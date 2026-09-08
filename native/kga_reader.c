/*
 * kga_reader.c — Verify KGA pages and stream graph rows into CBM.
 *
 * The reader opens a file identity captured by Python, bounds every positional
 * read to that immutable view, and validates frame CRC, SHA-256, and Merkle
 * references before handing leaf-sized batches to the generic CBM importer.
 */
#include "kga_reader.h"

#include "engine/graph_import.h"
#include "foundation/profile.h"
#include "foundation/sha256.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <zlib.h>

#include <yyjson/yyjson.h>

enum {
    KGA_PAGE = 1,
    KGA_FRAME_HEADER_SIZE = 53,
    KGA_FRAME_DIGEST_OFFSET = 21,
    KGA_MAX_TREE_DEPTH = 64,
};

static const unsigned char KGA_MAGIC[] = {'K', 'G', 'A', '5', '\r', '\n', 0x1a, '\n'};

typedef struct {
    char *raw;
    size_t raw_size;
    yyjson_doc *document;
} kga_page_t;

typedef struct {
    int descriptor;
    uint64_t limit;
    cbm_graph_import_t *import;
    char *error;
    size_t error_size;
} import_context_t;

typedef cbm_graph_import_node_t node_item_t;
typedef cbm_graph_import_edge_t edge_item_t;

static int fail(char *error, size_t error_size, const char *format, ...) {
    if (error && error_size > 0) {
        va_list arguments;
        va_start(arguments, format);
        (void)vsnprintf(error, error_size, format, arguments);
        va_end(arguments);
    }
    return -1;
}

static bool digest_valid(const char *digest) {
    if (!digest || strlen(digest) != GHP_KGA_SHA256_HEX_LEN) {
        return false;
    }
    for (size_t index = 0; index < GHP_KGA_SHA256_HEX_LEN; index++) {
        char byte = digest[index];
        if (!((byte >= '0' && byte <= '9') || (byte >= 'a' && byte <= 'f'))) {
            return false;
        }
    }
    return true;
}

static uint32_t read_u32_be(const unsigned char *bytes) {
    return ((uint32_t)bytes[0] << 24U) | ((uint32_t)bytes[1] << 16U) | ((uint32_t)bytes[2] << 8U) |
           (uint32_t)bytes[3];
}

static uint64_t read_u64_be(const unsigned char *bytes) {
    uint64_t value = 0;
    for (size_t index = 0; index < 8; index++) {
        value = (value << 8U) | bytes[index];
    }
    return value;
}

static bool read_exact(int descriptor, uint64_t offset, void *buffer, size_t size) {
    unsigned char *output = buffer;
    size_t consumed = 0;
    while (consumed < size) {
        uint64_t position = offset + consumed;
        if (position > (uint64_t)INT64_MAX) {
            return false;
        }
        size_t remaining = size - consumed;
        if (remaining > (size_t)SSIZE_MAX) {
            remaining = (size_t)SSIZE_MAX;
        }
        ssize_t count = pread(descriptor, output + consumed, remaining, (off_t)position);
        if (count < 0 && errno == EINTR) {
            continue;
        }
        if (count <= 0) {
            return false;
        }
        consumed += (size_t)count;
    }
    return true;
}

static uint32_t compressed_crc32(const unsigned char *bytes, size_t size) {
    uLong value = crc32(0L, Z_NULL, 0);
    size_t offset = 0;
    while (offset < size) {
        size_t remaining = size - offset;
        uInt chunk = remaining > UINT_MAX ? UINT_MAX : (uInt)remaining;
        value = crc32(value, bytes + offset, chunk);
        offset += chunk;
    }
    return (uint32_t)value;
}

static void page_free(kga_page_t *page) {
    if (!page) {
        return;
    }
    yyjson_doc_free(page->document);
    free(page->raw);
    memset(page, 0, sizeof(*page));
}

static int page_read(import_context_t *context, const ghp_kga_root_t *reference, kga_page_t *page) {
    unsigned char header[KGA_FRAME_HEADER_SIZE];
    memset(page, 0, sizeof(*page));
    if (!reference->present || reference->offset > context->limit ||
        context->limit - reference->offset < sizeof(header) ||
        !read_exact(context->descriptor, reference->offset, header, sizeof(header))) {
        return fail(context->error, context->error_size, "invalid page frame at %llu",
                    (unsigned long long)reference->offset);
    }
    uint64_t raw_size = read_u64_be(header + 1);
    uint64_t compressed_size = read_u64_be(header + 9);
    uint32_t expected_crc = read_u32_be(header + 17);
    uint64_t payload_offset = reference->offset + sizeof(header);
    if (header[0] != KGA_PAGE || raw_size == 0 || compressed_size == 0 || raw_size >= SIZE_MAX ||
        raw_size > ULONG_MAX || compressed_size > SIZE_MAX || payload_offset > context->limit ||
        compressed_size > context->limit - payload_offset) {
        return fail(context->error, context->error_size, "invalid page bounds at %llu",
                    (unsigned long long)reference->offset);
    }

    unsigned char *compressed = malloc((size_t)compressed_size);
    page->raw = malloc((size_t)raw_size + 1);
    if (!compressed || !page->raw ||
        !read_exact(context->descriptor, payload_offset, compressed, (size_t)compressed_size)) {
        free(compressed);
        page_free(page);
        return fail(context->error, context->error_size, "cannot read page at %llu",
                    (unsigned long long)reference->offset);
    }
    if (compressed_crc32(compressed, (size_t)compressed_size) != expected_crc) {
        free(compressed);
        page_free(page);
        return fail(context->error, context->error_size, "page CRC mismatch at %llu",
                    (unsigned long long)reference->offset);
    }
    uLongf output_size = (uLongf)raw_size;
    int decompressed =
        uncompress((Bytef *)page->raw, &output_size, compressed, (uLong)compressed_size);
    free(compressed);
    if (decompressed != Z_OK || output_size != raw_size) {
        page_free(page);
        return fail(context->error, context->error_size, "page decompression failed at %llu",
                    (unsigned long long)reference->offset);
    }
    page->raw[raw_size] = '\0';
    page->raw_size = (size_t)raw_size;

    unsigned char digest[CBM_SHA256_DIGEST_LEN];
    cbm_sha256_ctx sha256;
    cbm_sha256_init(&sha256);
    cbm_sha256_update(&sha256, page->raw, page->raw_size);
    cbm_sha256_final(&sha256, digest);
    if (memcmp(digest, header + KGA_FRAME_DIGEST_OFFSET, sizeof(digest)) != 0) {
        page_free(page);
        return fail(context->error, context->error_size, "page digest mismatch at %llu",
                    (unsigned long long)reference->offset);
    }

    page->document = yyjson_read_opts(page->raw, page->raw_size, YYJSON_READ_NOFLAG, NULL, NULL);
    if (!page->document || !yyjson_is_obj(yyjson_doc_get_root(page->document))) {
        page_free(page);
        return fail(context->error, context->error_size, "invalid page JSON at %llu",
                    (unsigned long long)reference->offset);
    }
    return 0;
}

static const char *json_string(yyjson_val *value) {
    if (!yyjson_is_str(value)) {
        return NULL;
    }
    const char *text = yyjson_get_str(value);
    return text && strlen(text) == yyjson_get_len(value) ? text : NULL;
}

static bool json_u64(yyjson_val *value, uint64_t *output) {
    if (!yyjson_is_uint(value)) {
        return false;
    }
    *output = yyjson_get_uint(value);
    return true;
}

static bool json_int32(yyjson_val *value, int *output) {
    if (!yyjson_is_int(value)) {
        return false;
    }
    if (yyjson_is_uint(value)) {
        uint64_t number = yyjson_get_uint(value);
        if (number > INT_MAX) {
            return false;
        }
        *output = (int)number;
        return true;
    }
    int64_t number = yyjson_get_sint(value);
    if (number < INT_MIN || number > INT_MAX) {
        return false;
    }
    *output = (int)number;
    return true;
}

static bool root_from_json(yyjson_val *object, ghp_kga_root_t *reference) {
    uint64_t offset = 0;
    uint64_t count = 0;
    const char *logical_hash = NULL;
    if (!yyjson_is_obj(object) || !json_u64(yyjson_obj_get(object, "offset"), &offset) ||
        !json_u64(yyjson_obj_get(object, "count"), &count) ||
        !(logical_hash = json_string(yyjson_obj_get(object, "logical_hash"))) ||
        !digest_valid(logical_hash)) {
        return false;
    }
    reference->present = true;
    reference->offset = offset;
    reference->count = count;
    (void)snprintf(reference->logical_hash, sizeof(reference->logical_hash), "%s", logical_hash);
    return true;
}

static int compare_nodes(const void *left, const void *right) {
    const node_item_t *a = left;
    const node_item_t *b = right;
    return strcmp(a->qualified_name, b->qualified_name);
}

static int compare_edges(const void *left, const void *right) {
    const edge_item_t *a = left;
    const edge_item_t *b = right;
    const char *a_parts[] = {a->source, a->target, a->type, a->local_name};
    const char *b_parts[] = {b->source, b->target, b->type, b->local_name};
    for (size_t index = 0; index < 4; index++) {
        int comparison = strcmp(a_parts[index], b_parts[index]);
        if (comparison != 0) {
            return comparison;
        }
    }
    return 0;
}

static bool shard_matches(const char *identity, const char *shard) {
    if (!identity || !shard) {
        return false;
    }
    const char *cursor = identity;
    unsigned dots = 0;
    while (*cursor && dots < 3) {
        if (*cursor == '.') {
            dots++;
        }
        cursor++;
    }
    size_t length = dots == 3 ? (size_t)(cursor - identity - 1) : (size_t)(cursor - identity);
    return strlen(shard) == length && memcmp(identity, shard, length) == 0;
}

static int import_node_leaf(import_context_t *context, yyjson_val *entries, uint64_t count,
                            const char *shard) {
    if (!yyjson_is_arr(entries) || yyjson_arr_size(entries) != count || count > SIZE_MAX) {
        return fail(context->error, context->error_size, "invalid node leaf entries");
    }
    node_item_t *items = calloc((size_t)count, sizeof(*items));
    if (!items && count > 0) {
        return fail(context->error, context->error_size, "cannot allocate node leaf");
    }
    int status = -1;
    size_t index, maximum;
    yyjson_val *entry;
    yyjson_arr_foreach(entries, index, maximum, entry) {
        yyjson_val *attributes = yyjson_arr_get(entry, 1);
        cbm_graph_import_node_t *row = &items[index];
        if (!yyjson_is_arr(entry) || yyjson_arr_size(entry) != 2 || !yyjson_is_obj(attributes) ||
            !(row->qualified_name = json_string(yyjson_arr_get(entry, 0))) ||
            !(row->label = json_string(yyjson_obj_get(attributes, "label"))) ||
            !(row->name = json_string(yyjson_obj_get(attributes, "name"))) ||
            !(row->file_path = json_string(yyjson_obj_get(attributes, "file_path"))) ||
            !json_int32(yyjson_obj_get(attributes, "start_line"), &row->start_line) ||
            !json_int32(yyjson_obj_get(attributes, "end_line"), &row->end_line) ||
            !shard_matches(row->qualified_name, shard)) {
            fail(context->error, context->error_size, "invalid node row in KGA leaf");
            goto cleanup;
        }
        yyjson_val *properties = yyjson_obj_get(attributes, "properties");
        if (!yyjson_is_obj(properties) ||
            !(row->properties_json = yyjson_val_write(properties, YYJSON_WRITE_NOFLAG, NULL))) {
            fail(context->error, context->error_size, "invalid node properties in KGA leaf");
            goto cleanup;
        }
    }
    qsort(items, (size_t)count, sizeof(*items), compare_nodes);
    for (size_t item = 1; item < (size_t)count; item++) {
        if (compare_nodes(&items[item - 1], &items[item]) == 0) {
            fail(context->error, context->error_size, "duplicate node identity in KGA leaf");
            goto cleanup;
        }
    }
    cbm_graph_import_status_t imported = cbm_graph_import_add_nodes(
        context->import, items, (size_t)count, context->error, context->error_size);
    status = imported == CBM_GRAPH_IMPORT_OK ? 0 : -1;

cleanup:
    for (size_t item = 0; item < (size_t)count; item++) {
        free((void *)items[item].properties_json);
    }
    free(items);
    return status;
}

static int import_edge_leaf(import_context_t *context, yyjson_val *entries, uint64_t count,
                            const char *shard) {
    if (!yyjson_is_arr(entries) || yyjson_arr_size(entries) != count || count > SIZE_MAX) {
        return fail(context->error, context->error_size, "invalid edge leaf entries");
    }
    edge_item_t *items = calloc((size_t)count, sizeof(*items));
    if (!items && count > 0) {
        return fail(context->error, context->error_size, "cannot allocate edge leaf");
    }
    int status = -1;
    size_t index, maximum;
    yyjson_val *entry;
    yyjson_arr_foreach(entries, index, maximum, entry) {
        yyjson_val *identity = yyjson_arr_get(entry, 0);
        yyjson_val *attributes = yyjson_arr_get(entry, 1);
        cbm_graph_import_edge_t *row = &items[index];
        if (!yyjson_is_arr(entry) || yyjson_arr_size(entry) != 2 || !yyjson_is_arr(identity) ||
            yyjson_arr_size(identity) != 4 || !yyjson_is_obj(attributes) ||
            !(row->source = json_string(yyjson_arr_get(identity, 0))) ||
            !(row->target = json_string(yyjson_arr_get(identity, 1))) ||
            !(row->type = json_string(yyjson_arr_get(identity, 2))) ||
            !(row->local_name = json_string(yyjson_arr_get(identity, 3))) ||
            !shard_matches(row->source, shard)) {
            fail(context->error, context->error_size, "invalid edge row in KGA leaf");
            goto cleanup;
        }
        yyjson_val *properties = yyjson_obj_get(attributes, "properties");
        if (!yyjson_is_obj(properties) ||
            !(row->properties_json = yyjson_val_write(properties, YYJSON_WRITE_NOFLAG, NULL))) {
            fail(context->error, context->error_size, "invalid edge properties in KGA leaf");
            goto cleanup;
        }
    }
    qsort(items, (size_t)count, sizeof(*items), compare_edges);
    for (size_t item = 1; item < (size_t)count; item++) {
        if (compare_edges(&items[item - 1], &items[item]) == 0) {
            fail(context->error, context->error_size, "duplicate edge identity in KGA leaf");
            goto cleanup;
        }
    }
    cbm_graph_import_status_t imported = cbm_graph_import_add_edges(
        context->import, items, (size_t)count, context->error, context->error_size);
    status = imported == CBM_GRAPH_IMPORT_OK ? 0 : -1;

cleanup:
    for (size_t item = 0; item < (size_t)count; item++) {
        free((void *)items[item].properties_json);
    }
    free(items);
    return status;
}

static int import_tree(import_context_t *context, const ghp_kga_root_t *reference, const char *tree,
                       const char *shard, unsigned depth) {
    if (depth >= KGA_MAX_TREE_DEPTH) {
        return fail(context->error, context->error_size, "KGA tree exceeds depth limit");
    }
    kga_page_t page;
    if (page_read(context, reference, &page) != 0) {
        return -1;
    }
    yyjson_val *root = yyjson_doc_get_root(page.document);
    const char *page_tree = json_string(yyjson_obj_get(root, "tree"));
    const char *kind = json_string(yyjson_obj_get(root, "kind"));
    const char *logical_hash = json_string(yyjson_obj_get(root, "logical_hash"));
    uint64_t count = 0;
    int page_depth = -1;
    if (!page_tree || strcmp(page_tree, tree) != 0 || !kind || !logical_hash ||
        strcmp(logical_hash, reference->logical_hash) != 0 ||
        !json_u64(yyjson_obj_get(root, "count"), &count) || count != reference->count ||
        !json_int32(yyjson_obj_get(root, "depth"), &page_depth) || page_depth != (int)depth) {
        page_free(&page);
        return fail(context->error, context->error_size, "KGA page reference mismatch at %llu",
                    (unsigned long long)reference->offset);
    }

    int status = -1;
    if (strcmp(kind, "leaf") == 0) {
        if (depth != 1 || !shard) {
            fail(context->error, context->error_size, "invalid KGA leaf depth");
            goto done;
        }
        yyjson_val *entries = yyjson_obj_get(root, "entries");
        status = strcmp(tree, "nodes") == 0 ? import_node_leaf(context, entries, count, shard)
                                            : import_edge_leaf(context, entries, count, shard);
    } else if (strcmp(kind, "branch") == 0) {
        if (depth != 0 || shard) {
            fail(context->error, context->error_size, "invalid KGA branch depth");
            goto done;
        }
        yyjson_val *children = yyjson_obj_get(root, "children");
        if (!yyjson_is_arr(children)) {
            fail(context->error, context->error_size, "invalid KGA branch children");
            goto done;
        }
        uint64_t total = 0;
        const char *previous_slot = NULL;
        size_t index, maximum;
        yyjson_val *child;
        yyjson_arr_foreach(children, index, maximum, child) {
            const char *slot = json_string(yyjson_arr_get(child, 0));
            ghp_kga_root_t child_reference = {0};
            if (!yyjson_is_arr(child) || yyjson_arr_size(child) != 2 || !slot ||
                (previous_slot && strcmp(previous_slot, slot) >= 0) ||
                !root_from_json(yyjson_arr_get(child, 1), &child_reference) ||
                UINT64_MAX - total < child_reference.count) {
                fail(context->error, context->error_size, "invalid KGA branch reference");
                goto done;
            }
            previous_slot = slot;
            total += child_reference.count;
            if (import_tree(context, &child_reference, tree, slot, depth + 1) != 0) {
                goto done;
            }
        }
        if (total != count) {
            fail(context->error, context->error_size, "KGA branch count mismatch");
            goto done;
        }
        status = 0;
    } else {
        fail(context->error, context->error_size, "invalid KGA page kind");
    }

done:
    page_free(&page);
    return status;
}

int ghp_kga_import_snapshot(const ghp_kga_snapshot_t *snapshot, ghp_kga_import_result_t *output,
                            char *error, size_t error_size) {
    if (output) {
        memset(output, 0, sizeof(*output));
    }
    if (error && error_size > 0) {
        error[0] = '\0';
    }
    if (!snapshot || !snapshot->archive_path || !snapshot->project || !snapshot->database_path ||
        !digest_valid(snapshot->graph_digest) || snapshot->captured_size < sizeof(KGA_MAGIC) ||
        snapshot->node_count < 1 || snapshot->edge_count < 0 || !snapshot->node_root.present ||
        snapshot->node_root.count != (uint64_t)snapshot->node_count ||
        snapshot->edge_root.present != (snapshot->edge_count > 0) ||
        (snapshot->edge_root.present &&
         snapshot->edge_root.count != (uint64_t)snapshot->edge_count)) {
        return fail(error, error_size, "invalid KGA import snapshot");
    }

    int descriptor = open(snapshot->archive_path, O_RDONLY | O_CLOEXEC);
    if (descriptor < 0) {
        return fail(error, error_size, "cannot open KGA archive: %s", strerror(errno));
    }
    struct stat status;
    unsigned char magic[sizeof(KGA_MAGIC)];
    if (fstat(descriptor, &status) != 0 || (uint64_t)status.st_dev != snapshot->archive_device ||
        (uint64_t)status.st_ino != snapshot->archive_inode || status.st_size < 0 ||
        (uint64_t)status.st_size < snapshot->captured_size ||
        !read_exact(descriptor, 0, magic, sizeof(magic)) ||
        memcmp(magic, KGA_MAGIC, sizeof(magic)) != 0) {
        (void)close(descriptor);
        return fail(error, error_size, "KGA archive identity changed before native load");
    }

    cbm_graph_import_options_t options = {
        .project = snapshot->project,
        .root_path = snapshot->archive_path,
        .source_digest = snapshot->graph_digest,
        .final_db_path = snapshot->database_path,
        .node_count = snapshot->node_count,
        .edge_count = snapshot->edge_count,
        .unordered_identities = true,
        .prevalidated_unique_identities = true,
        .drop_missing_edge_endpoints = snapshot->repair_legacy,
    };
    cbm_graph_import_t *import = NULL;
    cbm_graph_import_status_t begun = cbm_graph_import_begin(&options, &import, error, error_size);
    if (begun != CBM_GRAPH_IMPORT_OK) {
        (void)close(descriptor);
        return -1;
    }
    import_context_t context = {
        .descriptor = descriptor,
        .limit = snapshot->captured_size,
        .import = import,
        .error = error,
        .error_size = error_size,
    };
    CBM_PROF_START(nodes_started);
    int result = import_tree(&context, &snapshot->node_root, "nodes", NULL, 0);
    CBM_PROF_END_N("kga_import", "nodes", nodes_started, snapshot->node_count);
    if (result == 0 && snapshot->edge_root.present) {
        CBM_PROF_START(edges_started);
        result = import_tree(&context, &snapshot->edge_root, "edges", NULL, 0);
        CBM_PROF_END_N("kga_import", "edges", edges_started, snapshot->edge_count);
    }
    if (result == 0) {
        CBM_PROF_START(finish_started);
        cbm_graph_import_result_t imported = {0};
        if (cbm_graph_import_finish(import, &imported, error, error_size) != CBM_GRAPH_IMPORT_OK) {
            result = -1;
        } else if (output) {
            output->node_count = imported.node_count;
            output->edge_count = imported.edge_count;
            output->input_edge_count = imported.input_edge_count;
            output->dropped_edge_count = imported.dropped_edge_count;
        }
        CBM_PROF_END_N("kga_import", "finish", finish_started,
                       snapshot->node_count + snapshot->edge_count);
    }
    cbm_graph_import_free(import);
    (void)close(descriptor);
    return result;
}

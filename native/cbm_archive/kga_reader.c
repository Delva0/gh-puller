/*
 * kga_reader.c — Verify KGA pages and stream exact graph and coverage rows into CBM.
 *
 * The reader opens a file identity captured by Python, bounds every positional
 * read to that immutable view, and validates frame CRC, SHA-256, and Merkle
 * references before handing leaf-sized batches to the generic CBM importer.
 */
#include "kga_reader.h"
#include "kga_sha256.h"

#include "sdk/sdk.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <zlib.h>

#include <yyjson/yyjson.h>

enum {
    KGA_PAGE = 1,
    KGA_FRAME_HEADER_SIZE = 53,
    KGA_FRAME_DIGEST_OFFSET = 21,
    KGA_MAX_TREE_DEPTH = 64,
    KGA_IMPORT_WORKERS = 16,
};

static const unsigned char KGA_MAGIC[] = {'K', 'G', 'A', '5', '\r', '\n', 0x1a, '\n'};

typedef struct {
    char *raw;
    size_t raw_size;
    yyjson_doc *document;
} kga_page_t;

typedef enum {
    PROFILE_IO,
    PROFILE_CRC,
    PROFILE_INFLATE,
    PROFILE_DIGEST,
    PROFILE_JSON,
    PROFILE_ROWS,
    PROFILE_PROPERTIES,
    PROFILE_SORT,
    PROFILE_SDK_WAIT,
    PROFILE_SDK_IMPORT,
    PROFILE_RELEASE,
    PROFILE_STAGE_COUNT,
} profile_stage_t;

typedef struct {
    bool active;
    atomic_uint_fast64_t microseconds[PROFILE_STAGE_COUNT];
    atomic_uint_fast64_t calls[PROFILE_STAGE_COUNT];
} profile_metrics_t;

typedef struct {
    int descriptor;
    uint64_t limit;
    cbm_sdk_import_t *import;
    char *error;
    size_t error_size;
    pthread_mutex_t *sdk_lock;
    atomic_int *failed;
    profile_metrics_t *profile;
} import_context_t;

typedef cbm_sdk_node_t node_item_t;
typedef cbm_sdk_edge_t edge_item_t;
typedef cbm_sdk_coverage_row_t coverage_item_t;

static const char EMPTY_PROPERTIES_JSON[] = "{}";

typedef struct {
    struct timespec started;
    bool active;
} profile_span_t;

typedef struct {
    ghp_kga_root_t reference;
    const char *shard;
} tree_child_t;

typedef struct {
    import_context_t context;
    const tree_child_t *children;
    size_t child_count;
    const char *tree;
    atomic_size_t next;
    atomic_int failed;
    pthread_mutex_t sdk_lock;
    char error[1024];
} tree_import_work_t;

static bool profile_enabled(void) {
    const char *profile = getenv("CBM_PROFILE");
    return profile && profile[0] && profile[0] != '0';
}

static uint64_t profile_clock(const profile_metrics_t *metrics) {
    if (!metrics || !metrics->active) {
        return 0;
    }
    struct timespec now;
    (void)clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint64_t)now.tv_sec * 1000000U + (uint64_t)now.tv_nsec / 1000U;
}

static void profile_record(profile_metrics_t *metrics, profile_stage_t stage, uint64_t microseconds,
                           uint64_t calls) {
    if (!metrics || !metrics->active) {
        return;
    }
    atomic_fetch_add_explicit(&metrics->microseconds[stage], microseconds, memory_order_relaxed);
    atomic_fetch_add_explicit(&metrics->calls[stage], calls, memory_order_relaxed);
}

static void profile_add(profile_metrics_t *metrics, profile_stage_t stage, uint64_t started,
                        uint64_t calls) {
    if (started) {
        profile_record(metrics, stage, profile_clock(metrics) - started, calls);
    }
}

static void profile_metrics_init(profile_metrics_t *metrics) {
    memset(metrics, 0, sizeof(*metrics));
    metrics->active = profile_enabled();
    for (size_t stage = 0; stage < PROFILE_STAGE_COUNT; stage++) {
        atomic_init(&metrics->microseconds[stage], 0);
        atomic_init(&metrics->calls[stage], 0);
    }
}

static void profile_metrics_finish(const char *tree, const profile_metrics_t *metrics) {
    static const char *names[] = {
        "io",   "crc",        "inflate", "digest",     "json",    "rows",
        "properties", "sort_unique", "sdk_wait", "sdk_import", "release",
    };
    if (!metrics->active) {
        return;
    }
    for (size_t stage = 0; stage < PROFILE_STAGE_COUNT; stage++) {
        uint64_t microseconds =
            atomic_load_explicit(&metrics->microseconds[stage], memory_order_relaxed);
        uint64_t calls = atomic_load_explicit(&metrics->calls[stage], memory_order_relaxed);
        if (calls > 0) {
            (void)fprintf(stderr,
                          "level=info msg=prof phase=kga_decode tree=%s sub=%s ms=%llu us=%llu "
                          "calls=%llu\n",
                          tree, names[stage], (unsigned long long)(microseconds / 1000U),
                          (unsigned long long)microseconds, (unsigned long long)calls);
        }
    }
}

static profile_span_t profile_start(void) {
    profile_span_t span = {.active = profile_enabled()};
    if (span.active) {
        (void)clock_gettime(CLOCK_MONOTONIC, &span.started);
    }
    return span;
}

static void profile_finish(const char *subphase, profile_span_t span, long items) {
    if (!span.active) {
        return;
    }
    struct timespec finished;
    (void)clock_gettime(CLOCK_MONOTONIC, &finished);
    long microseconds = (finished.tv_sec - span.started.tv_sec) * 1000000L +
                        (finished.tv_nsec - span.started.tv_nsec) / 1000L;
    long rate = microseconds > 0 ? (long)((double)items * 1000000.0 / (double)microseconds) : 0;
    (void)fprintf(stderr,
                  "level=info msg=prof phase=kga_import sub=%s ms=%ld us=%ld items=%ld "
                  "rate_per_s=%ld\n",
                  subphase, microseconds / 1000L, microseconds, items, rate);
}

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

static bool coverage_meta_valid(const ghp_kga_coverage_meta_t *metadata) {
    return metadata->index_mode && metadata->recorded_at && metadata->recording_status &&
           metadata->ignored_files_stored >= 0 && metadata->ignored_files_total >= 0 &&
           metadata->coverage_version >= 1;
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
    uint64_t measured = profile_clock(context->profile);
    if (!reference->present || reference->offset > context->limit ||
        context->limit - reference->offset < sizeof(header) ||
        !read_exact(context->descriptor, reference->offset, header, sizeof(header))) {
        return fail(context->error, context->error_size, "invalid page frame at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_IO, measured, 1);
    uint64_t raw_size = read_u64_be(header + 1);
    uint64_t compressed_size = read_u64_be(header + 9);
    uint32_t expected_crc = read_u32_be(header + 17);
    uint64_t payload_offset = reference->offset + sizeof(header);
    if (header[0] != KGA_PAGE || raw_size == 0 || compressed_size == 0 ||
        raw_size > SIZE_MAX - YYJSON_PADDING_SIZE || raw_size > ULONG_MAX ||
        compressed_size > SIZE_MAX || payload_offset > context->limit ||
        compressed_size > context->limit - payload_offset) {
        return fail(context->error, context->error_size, "invalid page bounds at %llu",
                    (unsigned long long)reference->offset);
    }

    measured = profile_clock(context->profile);
    unsigned char *compressed = malloc((size_t)compressed_size);
    page->raw = malloc((size_t)raw_size + YYJSON_PADDING_SIZE);
    if (!compressed || !page->raw ||
        !read_exact(context->descriptor, payload_offset, compressed, (size_t)compressed_size)) {
        free(compressed);
        page_free(page);
        return fail(context->error, context->error_size, "cannot read page at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_IO, measured, 1);
    measured = profile_clock(context->profile);
    if (compressed_crc32(compressed, (size_t)compressed_size) != expected_crc) {
        free(compressed);
        page_free(page);
        return fail(context->error, context->error_size, "page CRC mismatch at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_CRC, measured, 1);
    measured = profile_clock(context->profile);
    uLongf output_size = (uLongf)raw_size;
    int decompressed =
        uncompress((Bytef *)page->raw, &output_size, compressed, (uLong)compressed_size);
    free(compressed);
    if (decompressed != Z_OK || output_size != raw_size) {
        page_free(page);
        return fail(context->error, context->error_size, "page decompression failed at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_INFLATE, measured, 1);
    memset(page->raw + raw_size, 0, YYJSON_PADDING_SIZE);
    page->raw_size = (size_t)raw_size;

    measured = profile_clock(context->profile);
    unsigned char digest[GHP_SHA256_DIGEST_SIZE];
    ghp_sha256(page->raw, page->raw_size, digest);
    if (memcmp(digest, header + KGA_FRAME_DIGEST_OFFSET, sizeof(digest)) != 0) {
        page_free(page);
        return fail(context->error, context->error_size, "page digest mismatch at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_DIGEST, measured, 1);

    measured = profile_clock(context->profile);
    page->document = yyjson_read_opts(page->raw, page->raw_size, YYJSON_READ_INSITU, NULL, NULL);
    if (!page->document || !yyjson_is_obj(yyjson_doc_get_root(page->document))) {
        page_free(page);
        return fail(context->error, context->error_size, "invalid page JSON at %llu",
                    (unsigned long long)reference->offset);
    }
    profile_add(context->profile, PROFILE_JSON, measured, 1);
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

static bool edge_properties_match_identity(const edge_item_t *row, yyjson_val *properties) {
    if (strcmp(row->type, "IMPORTS") != 0) {
        return row->local_name[0] == '\0';
    }
    yyjson_val *value = yyjson_obj_get(properties, "local_name");
    const char *stored = value ? json_string(value) : "";
    return stored && strcmp(stored, row->local_name) == 0;
}

static const char *serialize_properties(yyjson_val *properties) {
    if (!yyjson_is_obj(properties)) {
        return NULL;
    }
    return yyjson_obj_size(properties) == 0
               ? EMPTY_PROPERTIES_JSON
               : yyjson_val_write(properties, YYJSON_WRITE_NOFLAG, NULL);
}

static void free_properties(const char *properties) {
    if (properties != EMPTY_PROPERTIES_JSON) {
        free((void *)properties);
    }
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

static int compare_coverage(const void *left, const void *right) {
    const coverage_item_t *a = left;
    const coverage_item_t *b = right;
    int path_comparison = strcmp(a->rel_path, b->rel_path);
    return path_comparison != 0 ? path_comparison : strcmp(a->kind, b->kind);
}

static bool graph_shard_matches(const char *identity, const char *shard) {
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

static bool coverage_shard_matches(const char *rel_path, const char *shard) {
    if (!rel_path || !shard) {
        return false;
    }
    const char *slash = strchr(rel_path, '/');
    size_t length = slash ? (size_t)(slash - rel_path) : strlen(rel_path);
    if (length == 0) {
        return strcmp(shard, ".") == 0;
    }
    return strlen(shard) == length && memcmp(rel_path, shard, length) == 0;
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
    uint64_t rows_started = profile_clock(context->profile);
    uint64_t properties_microseconds = 0;
    size_t index, maximum;
    yyjson_val *entry;
    yyjson_arr_foreach(entries, index, maximum, entry) {
        yyjson_val *attributes = yyjson_arr_get(entry, 1);
        cbm_sdk_node_t *row = &items[index];
        if (!yyjson_is_arr(entry) || yyjson_arr_size(entry) != 2 || !yyjson_is_obj(attributes) ||
            !(row->qualified_name = json_string(yyjson_arr_get(entry, 0))) ||
            !(row->label = json_string(yyjson_obj_get(attributes, "label"))) ||
            !(row->name = json_string(yyjson_obj_get(attributes, "name"))) ||
            !(row->file_path = json_string(yyjson_obj_get(attributes, "file_path"))) ||
            !json_int32(yyjson_obj_get(attributes, "start_line"), &row->start_line) ||
            !json_int32(yyjson_obj_get(attributes, "end_line"), &row->end_line) ||
            !graph_shard_matches(row->qualified_name, shard)) {
            fail(context->error, context->error_size, "invalid node row in KGA leaf");
            goto cleanup;
        }
        yyjson_val *properties = yyjson_obj_get(attributes, "properties");
        uint64_t properties_started = profile_clock(context->profile);
        row->properties_json = serialize_properties(properties);
        if (properties_started) {
            properties_microseconds += profile_clock(context->profile) - properties_started;
        }
        if (!row->properties_json) {
            fail(context->error, context->error_size, "invalid node properties in KGA leaf");
            goto cleanup;
        }
    }
    profile_add(context->profile, PROFILE_ROWS, rows_started, count);
    profile_record(context->profile, PROFILE_PROPERTIES, properties_microseconds, count);
    uint64_t sort_started = profile_clock(context->profile);
    qsort(items, (size_t)count, sizeof(*items), compare_nodes);
    for (size_t item = 1; item < (size_t)count; item++) {
        if (compare_nodes(&items[item - 1], &items[item]) == 0) {
            fail(context->error, context->error_size, "duplicate node identity in KGA leaf");
            goto cleanup;
        }
    }
    profile_add(context->profile, PROFILE_SORT, sort_started, 1);
    uint64_t wait_started = profile_clock(context->profile);
    if (context->sdk_lock) {
        (void)pthread_mutex_lock(context->sdk_lock);
    }
    profile_add(context->profile, PROFILE_SDK_WAIT, wait_started, 1);
    uint64_t import_started = profile_clock(context->profile);
    cbm_sdk_status_t imported =
        context->failed && atomic_load(context->failed)
            ? CBM_SDK_CANCELLED
            : cbm_sdk_import_add_nodes(context->import, items, (size_t)count, context->error,
                                       context->error_size);
    profile_add(context->profile, PROFILE_SDK_IMPORT, import_started, 1);
    if (context->sdk_lock) {
        (void)pthread_mutex_unlock(context->sdk_lock);
    }
    status = imported == CBM_SDK_OK ? 0 : -1;

cleanup:
    uint64_t release_started = profile_clock(context->profile);
    for (size_t item = 0; item < (size_t)count; item++) {
        free_properties(items[item].properties_json);
    }
    free(items);
    profile_add(context->profile, PROFILE_RELEASE, release_started, count);
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
    uint64_t rows_started = profile_clock(context->profile);
    uint64_t properties_microseconds = 0;
    size_t index, maximum;
    yyjson_val *entry;
    yyjson_arr_foreach(entries, index, maximum, entry) {
        yyjson_val *identity = yyjson_arr_get(entry, 0);
        yyjson_val *attributes = yyjson_arr_get(entry, 1);
        cbm_sdk_edge_t *row = &items[index];
        if (!yyjson_is_arr(entry) || yyjson_arr_size(entry) != 2 || !yyjson_is_arr(identity) ||
            yyjson_arr_size(identity) != 4 || !yyjson_is_obj(attributes) ||
            !(row->source = json_string(yyjson_arr_get(identity, 0))) ||
            !(row->target = json_string(yyjson_arr_get(identity, 1))) ||
            !(row->type = json_string(yyjson_arr_get(identity, 2))) ||
            !(row->local_name = json_string(yyjson_arr_get(identity, 3))) ||
            !graph_shard_matches(row->source, shard)) {
            fail(context->error, context->error_size, "invalid edge row in KGA leaf");
            goto cleanup;
        }
        yyjson_val *properties = yyjson_obj_get(attributes, "properties");
        uint64_t properties_started = profile_clock(context->profile);
        row->properties_json =
            yyjson_is_obj(properties) && edge_properties_match_identity(row, properties)
                ? serialize_properties(properties)
                : NULL;
        if (properties_started) {
            properties_microseconds += profile_clock(context->profile) - properties_started;
        }
        if (!row->properties_json) {
            fail(context->error, context->error_size, "invalid edge properties in KGA leaf");
            goto cleanup;
        }
    }
    profile_add(context->profile, PROFILE_ROWS, rows_started, count);
    profile_record(context->profile, PROFILE_PROPERTIES, properties_microseconds, count);
    uint64_t sort_started = profile_clock(context->profile);
    qsort(items, (size_t)count, sizeof(*items), compare_edges);
    for (size_t item = 1; item < (size_t)count; item++) {
        if (compare_edges(&items[item - 1], &items[item]) == 0) {
            fail(context->error, context->error_size, "duplicate edge identity in KGA leaf");
            goto cleanup;
        }
    }
    profile_add(context->profile, PROFILE_SORT, sort_started, 1);
    uint64_t wait_started = profile_clock(context->profile);
    if (context->sdk_lock) {
        (void)pthread_mutex_lock(context->sdk_lock);
    }
    profile_add(context->profile, PROFILE_SDK_WAIT, wait_started, 1);
    uint64_t import_started = profile_clock(context->profile);
    cbm_sdk_status_t imported =
        context->failed && atomic_load(context->failed)
            ? CBM_SDK_CANCELLED
            : cbm_sdk_import_add_edges(context->import, items, (size_t)count, context->error,
                                       context->error_size);
    profile_add(context->profile, PROFILE_SDK_IMPORT, import_started, 1);
    if (context->sdk_lock) {
        (void)pthread_mutex_unlock(context->sdk_lock);
    }
    status = imported == CBM_SDK_OK ? 0 : -1;

cleanup:
    uint64_t release_started = profile_clock(context->profile);
    for (size_t item = 0; item < (size_t)count; item++) {
        free_properties(items[item].properties_json);
    }
    free(items);
    profile_add(context->profile, PROFILE_RELEASE, release_started, count);
    return status;
}

static int import_coverage_leaf(import_context_t *context, yyjson_val *entries, uint64_t count,
                                const char *shard) {
    if (!yyjson_is_arr(entries) || yyjson_arr_size(entries) != count || count > SIZE_MAX) {
        return fail(context->error, context->error_size, "invalid coverage leaf entries");
    }
    coverage_item_t *items = calloc((size_t)count, sizeof(*items));
    if (!items && count > 0) {
        return fail(context->error, context->error_size, "cannot allocate coverage leaf");
    }
    int status = -1;
    size_t index, maximum;
    yyjson_val *entry;
    yyjson_arr_foreach(entries, index, maximum, entry) {
        yyjson_val *identity = yyjson_arr_get(entry, 0);
        coverage_item_t *row = &items[index];
        if (!yyjson_is_arr(entry) || yyjson_arr_size(entry) != 2 || !yyjson_is_arr(identity) ||
            yyjson_arr_size(identity) != 2 ||
            !(row->rel_path = json_string(yyjson_arr_get(identity, 0))) ||
            !(row->kind = json_string(yyjson_arr_get(identity, 1))) ||
            !(row->detail = json_string(yyjson_arr_get(entry, 1))) ||
            !coverage_shard_matches(row->rel_path, shard)) {
            fail(context->error, context->error_size, "invalid coverage row in KGA leaf");
            goto cleanup;
        }
    }
    qsort(items, (size_t)count, sizeof(*items), compare_coverage);
    for (size_t item = 1; item < (size_t)count; item++) {
        if (compare_coverage(&items[item - 1], &items[item]) == 0) {
            fail(context->error, context->error_size, "duplicate coverage identity in KGA leaf");
            goto cleanup;
        }
    }
    cbm_sdk_status_t imported = cbm_sdk_import_add_coverage(context->import, items, (size_t)count,
                                                            context->error, context->error_size);
    status = imported == CBM_SDK_OK ? 0 : -1;

cleanup:
    free(items);
    return status;
}

static int import_tree(import_context_t *context, const ghp_kga_root_t *reference,
                       const char *tree, const char *shard, unsigned depth);

static void *import_tree_worker(void *opaque) {
    tree_import_work_t *work = opaque;
    while (!atomic_load(&work->failed)) {
        size_t index = atomic_fetch_add(&work->next, 1);
        if (index >= work->child_count) {
            break;
        }
        char error[sizeof(work->error)] = {0};
        import_context_t context = work->context;
        context.error = error;
        context.error_size = sizeof(error);
        context.sdk_lock = &work->sdk_lock;
        context.failed = &work->failed;
        const tree_child_t *child = &work->children[index];
        if (import_tree(&context, &child->reference, work->tree, child->shard, 1) != 0) {
            int expected = 0;
            if (atomic_compare_exchange_strong(&work->failed, &expected, 1)) {
                (void)snprintf(work->error, sizeof(work->error), "%s",
                               error[0] ? error : "parallel KGA import failed");
            }
            break;
        }
    }
    return NULL;
}

static int import_tree_children(import_context_t *context, const tree_child_t *children,
                                size_t child_count, const char *tree) {
    if (child_count < 2 || strcmp(tree, "coverage") == 0) {
        for (size_t index = 0; index < child_count; index++) {
            if (import_tree(context, &children[index].reference, tree, children[index].shard, 1) !=
                0) {
                return -1;
            }
        }
        return 0;
    }

    tree_import_work_t work = {
        .context = *context,
        .children = children,
        .child_count = child_count,
        .tree = tree,
    };
    atomic_init(&work.next, 0);
    atomic_init(&work.failed, 0);
    if (pthread_mutex_init(&work.sdk_lock, NULL) != 0) {
        return fail(context->error, context->error_size, "cannot initialize KGA import workers");
    }

    pthread_t workers[KGA_IMPORT_WORKERS - 1];
    size_t worker_count = 0;
    long online = sysconf(_SC_NPROCESSORS_ONLN);
    size_t worker_limit = online > 0 && online < KGA_IMPORT_WORKERS ? (size_t)online
                                                                  : KGA_IMPORT_WORKERS;
    while (worker_count + 1 < worker_limit && worker_count + 1 < child_count &&
           pthread_create(&workers[worker_count], NULL, import_tree_worker, &work) == 0) {
        worker_count++;
    }
    (void)import_tree_worker(&work);
    for (size_t index = 0; index < worker_count; index++) {
        (void)pthread_join(workers[index], NULL);
    }
    (void)pthread_mutex_destroy(&work.sdk_lock);
    if (atomic_load(&work.failed)) {
        return fail(context->error, context->error_size, "%s", work.error);
    }
    return 0;
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
        if (strcmp(tree, "nodes") == 0) {
            status = import_node_leaf(context, entries, count, shard);
        } else if (strcmp(tree, "edges") == 0) {
            status = import_edge_leaf(context, entries, count, shard);
        } else if (strcmp(tree, "coverage") == 0) {
            status = import_coverage_leaf(context, entries, count, shard);
        } else {
            fail(context->error, context->error_size, "unsupported KGA tree");
        }
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
        size_t child_count = yyjson_arr_size(children);
        tree_child_t *child_refs = calloc(child_count, sizeof(*child_refs));
        if (!child_refs && child_count > 0) {
            fail(context->error, context->error_size, "cannot allocate KGA branch references");
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
                free(child_refs);
                goto done;
            }
            previous_slot = slot;
            total += child_reference.count;
            child_refs[index] = (tree_child_t){.reference = child_reference, .shard = slot};
        }
        if (total != count) {
            fail(context->error, context->error_size, "KGA branch count mismatch");
            free(child_refs);
            goto done;
        }
        status = import_tree_children(context, child_refs, child_count, tree);
        free(child_refs);
    } else {
        fail(context->error, context->error_size, "invalid KGA page kind");
    }

done:
    page_free(&page);
    return status;
}

int ghp_kga_import_snapshot(const ghp_kga_snapshot_t *snapshot, char *error, size_t error_size) {
    if (error && error_size > 0) {
        error[0] = '\0';
    }
    if (!snapshot || !snapshot->archive_path || !snapshot->project || !snapshot->database_path ||
        !digest_valid(snapshot->graph_digest) || !digest_valid(snapshot->materialization_digest) ||
        snapshot->captured_size < sizeof(KGA_MAGIC) || snapshot->node_count < 1 ||
        snapshot->edge_count < 0 || !snapshot->node_root.present ||
        snapshot->node_root.count != (uint64_t)snapshot->node_count ||
        snapshot->edge_root.present != (snapshot->edge_count > 0) ||
        (snapshot->edge_root.present &&
         snapshot->edge_root.count != (uint64_t)snapshot->edge_count) ||
        snapshot->coverage_count < 0 ||
        snapshot->coverage_root.present !=
            (snapshot->coverage_present && snapshot->coverage_count > 0) ||
        (snapshot->coverage_root.present &&
         snapshot->coverage_root.count != (uint64_t)snapshot->coverage_count) ||
        (snapshot->coverage_present && !coverage_meta_valid(&snapshot->coverage_meta)) ||
        (!snapshot->coverage_present && snapshot->coverage_count != 0)) {
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

    cbm_sdk_import_options_t options = {
        .project = snapshot->project,
        .root_path = snapshot->archive_path,
        .source_digest = snapshot->graph_digest,
        .database_path = snapshot->database_path,
        .node_count = snapshot->node_count,
        .edge_count = snapshot->edge_count,
        .unordered_identities = true,
        .prevalidated_unique_identities = true,
        .prevalidated_rows = true,
    };
    cbm_sdk_import_t *import = NULL;
    cbm_sdk_status_t begun = cbm_sdk_import_begin(&options, &import, error, error_size);
    if (begun != CBM_SDK_OK) {
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
    profile_metrics_t nodes_profile;
    profile_metrics_init(&nodes_profile);
    context.profile = &nodes_profile;
    profile_span_t nodes_started = profile_start();
    int result = import_tree(&context, &snapshot->node_root, "nodes", NULL, 0);
    profile_finish("nodes", nodes_started, snapshot->node_count);
    profile_metrics_finish("nodes", &nodes_profile);
    if (result == 0 && snapshot->edge_root.present) {
        profile_metrics_t edges_profile;
        profile_metrics_init(&edges_profile);
        context.profile = &edges_profile;
        profile_span_t edges_started = profile_start();
        result = import_tree(&context, &snapshot->edge_root, "edges", NULL, 0);
        profile_finish("edges", edges_started, snapshot->edge_count);
        profile_metrics_finish("edges", &edges_profile);
    }
    if (result == 0 && snapshot->coverage_present) {
        cbm_sdk_coverage_meta_t metadata = {
            .index_mode = snapshot->coverage_meta.index_mode,
            .recorded_at = snapshot->coverage_meta.recorded_at,
            .recording_status = snapshot->coverage_meta.recording_status,
            .ignored_files_stored = snapshot->coverage_meta.ignored_files_stored,
            .ignored_files_total = snapshot->coverage_meta.ignored_files_total,
            .coverage_version = snapshot->coverage_meta.coverage_version,
            .hash_records_complete = snapshot->coverage_meta.hash_records_complete,
        };
        profile_metrics_t coverage_profile;
        profile_metrics_init(&coverage_profile);
        context.profile = &coverage_profile;
        profile_span_t coverage_started = profile_start();
        if (cbm_sdk_import_set_coverage(import, &metadata, snapshot->coverage_count, error,
                                        error_size) != CBM_SDK_OK ||
            (snapshot->coverage_root.present &&
             import_tree(&context, &snapshot->coverage_root, "coverage", NULL, 0) != 0)) {
            result = -1;
        }
        profile_finish("coverage", coverage_started, snapshot->coverage_count);
        profile_metrics_finish("coverage", &coverage_profile);
    }
    if (result == 0) {
        profile_span_t finish_started = profile_start();
        if (cbm_sdk_import_finish(import, NULL, error, error_size) != CBM_SDK_OK) {
            result = -1;
        }
        profile_finish("finish", finish_started, snapshot->node_count + snapshot->edge_count);
    }
    cbm_sdk_import_free(import);
    (void)close(descriptor);
    return result;
}

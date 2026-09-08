/*
 * cbm_archive_helper.c — Serve persistent KGA loads and native CBM tool calls.
 *
 * Control messages are length-prefixed JSON. Graph rows never cross the
 * protocol: the helper reads KGA pages directly and keeps the resulting
 * immutable SQLite store open across queries.
 */
#include "kga_reader.h"

#include "engine/tool_runtime.h"
#include "foundation/log.h"
#include "foundation/profile.h"
#include "store/store.h"

#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <yyjson/yyjson.h>

enum {
    NATIVE_PROTOCOL_VERSION = 4,
    KGA_FORMAT_VERSION = 5,
    KGA_FIDELITY_VERSION = 2,
    REQUEST_MAX_BYTES = 8 << 20,
};

typedef struct {
    cbm_store_t *store;
    char *project;
    char *database_path;
    char *graph_digest;
    int node_count;
    int edge_count;
} helper_session_t;

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

static bool json_int(yyjson_val *value, int *output) {
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

static char *document_json(yyjson_mut_doc *document) {
    char *json = yyjson_mut_write(document, YYJSON_WRITE_NOFLAG, NULL);
    yyjson_mut_doc_free(document);
    return json;
}

static yyjson_mut_doc *response_document(uint64_t id, bool ok, yyjson_mut_val **root_out) {
    yyjson_mut_doc *document = yyjson_mut_doc_new(NULL);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *root = yyjson_mut_obj(document);
    yyjson_mut_doc_set_root(document, root);
    yyjson_mut_obj_add_uint(document, root, "id", id);
    yyjson_mut_obj_add_bool(document, root, "ok", ok);
    *root_out = root;
    return document;
}

static char *error_response(uint64_t id, const char *code, const char *message) {
    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, false, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *error = yyjson_mut_obj(document);
    yyjson_mut_obj_add_str(document, error, "code", code);
    yyjson_mut_obj_add_str(document, error, "message", message);
    yyjson_mut_obj_add_val(document, root, "error", error);
    return document_json(document);
}

static char *hello_response(uint64_t id) {
    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, true, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_val *capabilities = yyjson_mut_arr(document);
    yyjson_mut_arr_add_str(document, capabilities, "archive-load");
    yyjson_mut_arr_add_str(document, capabilities, "tool-call");
    yyjson_mut_val *tools = yyjson_mut_arr(document);
    for (size_t index = 0; index < cbm_engine_tool_count(); index++) {
        yyjson_mut_arr_add_str(document, tools, cbm_engine_tool_name(index));
    }
    yyjson_mut_obj_add_int(document, result, "protocol", NATIVE_PROTOCOL_VERSION);
    yyjson_mut_obj_add_int(document, result, "kga_format", KGA_FORMAT_VERSION);
    yyjson_mut_obj_add_int(document, result, "graph_fidelity", KGA_FIDELITY_VERSION);
    yyjson_mut_obj_add_int(document, result, "store_format", CBM_INDEX_FORMAT_VERSION);
    yyjson_mut_obj_add_val(document, result, "capabilities", capabilities);
    yyjson_mut_obj_add_val(document, result, "tools", tools);
    yyjson_mut_obj_add_val(document, root, "result", result);
    return document_json(document);
}

static bool parse_root(yyjson_val *value, ghp_kga_root_t *root) {
    memset(root, 0, sizeof(*root));
    if (yyjson_is_null(value)) {
        return true;
    }
    uint64_t offset = 0;
    uint64_t count = 0;
    const char *logical_hash = NULL;
    if (!yyjson_is_obj(value) || !json_u64(yyjson_obj_get(value, "offset"), &offset) ||
        !json_u64(yyjson_obj_get(value, "count"), &count) ||
        !(logical_hash = json_string(yyjson_obj_get(value, "logical_hash"))) ||
        !digest_valid(logical_hash)) {
        return false;
    }
    root->present = true;
    root->offset = offset;
    root->count = count;
    (void)snprintf(root->logical_hash, sizeof(root->logical_hash), "%s", logical_hash);
    return true;
}

static bool parse_snapshot(yyjson_val *parameters, ghp_kga_snapshot_t *snapshot, bool *reuse) {
    memset(snapshot, 0, sizeof(*snapshot));
    uint64_t device = 0;
    uint64_t inode = 0;
    uint64_t captured_size = 0;
    int fidelity = 0;
    if (!yyjson_is_obj(parameters) ||
        !(snapshot->archive_path = json_string(yyjson_obj_get(parameters, "archive_path"))) ||
        !json_u64(yyjson_obj_get(parameters, "archive_device"), &device) ||
        !json_u64(yyjson_obj_get(parameters, "archive_inode"), &inode) ||
        !json_u64(yyjson_obj_get(parameters, "captured_size"), &captured_size) ||
        !(snapshot->project = json_string(yyjson_obj_get(parameters, "project"))) ||
        !(snapshot->graph_digest = json_string(yyjson_obj_get(parameters, "graph_digest"))) ||
        !digest_valid(snapshot->graph_digest) ||
        !(snapshot->database_path = json_string(yyjson_obj_get(parameters, "database_path"))) ||
        !json_int(yyjson_obj_get(parameters, "node_count"), &snapshot->node_count) ||
        !json_int(yyjson_obj_get(parameters, "edge_count"), &snapshot->edge_count) ||
        !json_int(yyjson_obj_get(parameters, "graph_fidelity"), &fidelity) ||
        fidelity != KGA_FIDELITY_VERSION ||
        !parse_root(yyjson_obj_get(parameters, "node_root"), &snapshot->node_root) ||
        !parse_root(yyjson_obj_get(parameters, "edge_root"), &snapshot->edge_root)) {
        return false;
    }
    yyjson_val *reuse_value = yyjson_obj_get(parameters, "reuse");
    if (reuse_value && !yyjson_is_bool(reuse_value)) {
        return false;
    }
    snapshot->archive_device = device;
    snapshot->archive_inode = inode;
    snapshot->captured_size = captured_size;
    *reuse = reuse_value && yyjson_get_bool(reuse_value);
    return true;
}

static void session_clear(helper_session_t *session) {
    cbm_store_close(session->store);
    free(session->project);
    free(session->database_path);
    free(session->graph_digest);
    memset(session, 0, sizeof(*session));
}

static bool session_install(helper_session_t *session, cbm_store_t *store, const char *project,
                            const char *database_path, const char *graph_digest, int node_count,
                            int edge_count) {
    char *saved_project = strdup(project);
    char *saved_database = strdup(database_path);
    char *saved_digest = strdup(graph_digest);
    if (!saved_project || !saved_database || !saved_digest) {
        free(saved_project);
        free(saved_database);
        free(saved_digest);
        cbm_store_close(store);
        return false;
    }
    session_clear(session);
    session->store = store;
    session->project = saved_project;
    session->database_path = saved_database;
    session->graph_digest = saved_digest;
    session->node_count = node_count;
    session->edge_count = edge_count;
    return true;
}

static cbm_store_t *open_expected_store(const ghp_kga_snapshot_t *snapshot) {
    cbm_store_t *store = cbm_store_open_path_query(snapshot->database_path);
    cbm_project_t project = {0};
    if (!store || cbm_store_get_project(store, snapshot->project, &project) != CBM_STORE_OK ||
        cbm_store_count_nodes(store, snapshot->project) != snapshot->node_count ||
        cbm_store_count_edges(store, snapshot->project) != snapshot->edge_count) {
        cbm_project_free_fields(&project);
        cbm_store_close(store);
        return NULL;
    }
    cbm_project_free_fields(&project);
    return store;
}

static char *load_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    ghp_kga_snapshot_t snapshot;
    bool reuse = false;
    if (!parse_snapshot(parameters, &snapshot, &reuse)) {
        return error_response(id, "invalid_request", "invalid archive load parameters");
    }
    bool already_loaded = session->store && strcmp(session->project, snapshot.project) == 0 &&
                          strcmp(session->database_path, snapshot.database_path) == 0 &&
                          strcmp(session->graph_digest, snapshot.graph_digest) == 0;
    bool materialized = false;
    if (!already_loaded) {
        cbm_store_t *candidate = reuse ? open_expected_store(&snapshot) : NULL;
        if (!candidate) {
            char error[1024];
            if (ghp_kga_import_snapshot(&snapshot, error, sizeof(error)) != 0) {
                return error_response(id, "archive_load_failed", error);
            }
            materialized = true;
            candidate = open_expected_store(&snapshot);
        }
        if (!candidate) {
            return error_response(id, "store_open_failed", "materialized CBM store is invalid");
        }
        if (!session_install(session, candidate, snapshot.project, snapshot.database_path,
                             snapshot.graph_digest, snapshot.node_count, snapshot.edge_count)) {
            return error_response(id, "allocation_failed", "cannot retain loaded graph state");
        }
    }

    yyjson_mut_val *root = NULL;
    yyjson_mut_doc *document = response_document(id, true, &root);
    if (!document) {
        return NULL;
    }
    yyjson_mut_val *result = yyjson_mut_obj(document);
    yyjson_mut_obj_add_str(document, result, "project", snapshot.project);
    yyjson_mut_obj_add_str(document, result, "graph_digest", snapshot.graph_digest);
    yyjson_mut_obj_add_int(document, result, "nodes", session->node_count);
    yyjson_mut_obj_add_int(document, result, "edges", session->edge_count);
    yyjson_mut_obj_add_int(document, result, "graph_fidelity", KGA_FIDELITY_VERSION);
    yyjson_mut_obj_add_bool(document, result, "materialized", materialized);
    yyjson_mut_obj_add_val(document, root, "result", result);
    return document_json(document);
}

static char *tool_response(uint64_t id, helper_session_t *session, yyjson_val *parameters) {
    const char *name = NULL;
    yyjson_val *arguments = NULL;
    if (!yyjson_is_obj(parameters) ||
        !(name = json_string(yyjson_obj_get(parameters, "name"))) || !name[0] ||
        !(arguments = yyjson_obj_get(parameters, "arguments")) || !yyjson_is_obj(arguments)) {
        return error_response(id, "invalid_request", "tool call requires name and arguments");
    }
    if (!session->store) {
        return error_response(id, "no_graph", "load an archive graph before calling a tool");
    }

    char *arguments_json = yyjson_val_write(arguments, YYJSON_WRITE_NOFLAG, NULL);
    if (!arguments_json) {
        return error_response(id, "allocation_failed", "cannot encode tool arguments");
    }
    cbm_engine_tool_result_t tool_result = {0};
    cbm_engine_tool_status_t status = cbm_engine_call_tool(
        session->store, session->project, name, arguments_json, &tool_result);
    free(arguments_json);
    if (status != CBM_ENGINE_TOOL_OK) {
        char *response = error_response(id, cbm_engine_tool_status_code(status),
                                        tool_result.error ? tool_result.error : "tool call failed");
        cbm_engine_tool_result_free(&tool_result);
        return response;
    }
    size_t result_size = strlen(tool_result.json);
    size_t capacity = result_size + 96;
    char *response = malloc(capacity);
    if (response) {
        int written = snprintf(response, capacity, "{\"id\":%llu,\"ok\":true,\"result\":%s}",
                               (unsigned long long)id, tool_result.json);
        if (written < 0 || (size_t)written >= capacity) {
            free(response);
            response = NULL;
        }
    }
    cbm_engine_tool_result_free(&tool_result);
    return response;
}

static char *dispatch_request(helper_session_t *session, yyjson_val *request, bool *shutdown) {
    uint64_t id = 0;
    const char *method = NULL;
    if (!yyjson_is_obj(request) || !json_u64(yyjson_obj_get(request, "id"), &id) ||
        !(method = json_string(yyjson_obj_get(request, "method")))) {
        return error_response(0, "invalid_request", "request requires uint id and method");
    }
    yyjson_val *parameters = yyjson_obj_get(request, "params");
    if (!parameters) {
        parameters = yyjson_obj_get(request, "parameters");
    }
    if (strcmp(method, "hello") == 0) {
        int protocol = 0;
        if (!yyjson_is_obj(parameters) ||
            !json_int(yyjson_obj_get(parameters, "protocol"), &protocol) ||
            protocol != NATIVE_PROTOCOL_VERSION) {
            return error_response(id, "protocol_mismatch", "unsupported native protocol");
        }
        return hello_response(id);
    }
    if (strcmp(method, "load") == 0) {
        return load_response(id, session, parameters);
    }
    if (strcmp(method, "call") == 0) {
        return tool_response(id, session, parameters);
    }
    if (strcmp(method, "shutdown") == 0) {
        *shutdown = true;
        yyjson_mut_val *root = NULL;
        yyjson_mut_doc *document = response_document(id, true, &root);
        if (!document) {
            return NULL;
        }
        yyjson_mut_obj_add_val(document, root, "result", yyjson_mut_obj(document));
        return document_json(document);
    }
    return error_response(id, "unsupported_method", "native method is not supported");
}

static int read_request(char **json, size_t *size) {
    unsigned char header[4];
    size_t prefix = fread(header, 1, sizeof(header), stdin);
    if (prefix == 0 && feof(stdin)) {
        return 0;
    }
    if (prefix != sizeof(header)) {
        return -1;
    }
    uint32_t length = ((uint32_t)header[0] << 24U) | ((uint32_t)header[1] << 16U) |
                      ((uint32_t)header[2] << 8U) | (uint32_t)header[3];
    if (length == 0 || length > REQUEST_MAX_BYTES) {
        return -1;
    }
    char *payload = malloc((size_t)length + 1);
    if (!payload || fread(payload, 1, length, stdin) != length) {
        free(payload);
        return -1;
    }
    payload[length] = '\0';
    *json = payload;
    *size = length;
    return 1;
}

static bool write_response(const char *json) {
    if (!json) {
        return false;
    }
    size_t length = strlen(json);
    if (length == 0 || length > UINT32_MAX) {
        return false;
    }
    unsigned char header[4] = {
        (unsigned char)(length >> 24U),
        (unsigned char)(length >> 16U),
        (unsigned char)(length >> 8U),
        (unsigned char)length,
    };
    return fwrite(header, 1, sizeof(header), stdout) == sizeof(header) &&
           fwrite(json, 1, length, stdout) == length && fflush(stdout) == 0;
}

int main(int argc, char **argv) {
    if (argc == 2 && strcmp(argv[1], "--version") == 0) {
        (void)printf("gh-puller-cbm-helper %d\n", NATIVE_PROTOCOL_VERSION);
        return 0;
    }
    if (argc != 1) {
        (void)fprintf(stderr, "usage: %s [--version]\n", argv[0]);
        return 2;
    }
    cbm_log_init_from_env();
    cbm_profile_init();
    (void)setvbuf(stdout, NULL, _IONBF, 0);

    helper_session_t session = {0};
    bool shutdown = false;
    int status = 0;
    while (!shutdown) {
        char *json = NULL;
        size_t size = 0;
        int read = read_request(&json, &size);
        if (read == 0) {
            break;
        }
        if (read < 0) {
            status = 2;
            break;
        }
        yyjson_doc *document = yyjson_read_opts(json, size, YYJSON_READ_NOFLAG, NULL, NULL);
        char *response = document
                             ? dispatch_request(&session, yyjson_doc_get_root(document), &shutdown)
                             : error_response(0, "invalid_request", "request is not valid JSON");
        yyjson_doc_free(document);
        free(json);
        if (!write_response(response)) {
            free(response);
            status = 2;
            break;
        }
        free(response);
    }
    session_clear(&session);
    return status;
}

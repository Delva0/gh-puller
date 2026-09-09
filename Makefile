# Build gh-puller's native adapters against the public CBM SDK.

CBM_ROOT ?= ../codebase-memory-mcp
NATIVE_BUILD_DIR ?= build/native
NATIVE_SOURCE_DIR := native
CBM_ARCHIVE_DIR := $(NATIVE_SOURCE_DIR)/cbm_archive
NATIVE_BIN_DIR := $(NATIVE_BUILD_DIR)/bin
NATIVE_OBJ_DIR := $(NATIVE_BUILD_DIR)/obj
NATIVE_LIB_DIR := $(NATIVE_BUILD_DIR)/lib

QUERY_TARGET := $(NATIVE_BIN_DIR)/gh-puller-cbm-helper
INDEX_TARGET := $(NATIVE_BIN_DIR)/gh-puller-cbm-index-helper
MATERIALIZER_TARGET := $(NATIVE_BIN_DIR)/gh-puller-kga-materializer
CBM_SDK := $(CBM_ROOT)/build/c/libcbm-sdk.a
CBM_SDK_FULL := $(CBM_ROOT)/build/c/libcbm-sdk-full.a
CBM_MIMALLOC := $(CBM_ROOT)/build/c/prod_mimalloc.o

CC ?= cc
CFLAGS := -std=c11 -D_DEFAULT_SOURCE -D_GNU_SOURCE -O2 -Wall -Wextra -Werror \
	-Wno-unused-parameter -Wno-sign-compare -Wdate-time \
	-I$(CBM_ROOT)/src -I$(CBM_ROOT)/vendored
LDFLAGS := -lm -lstdc++ -lpthread -lz -Wl,-z,noexecstack -Wl,-z,separate-code

ARCHIVE_OBJECT_DIR := $(NATIVE_OBJ_DIR)/cbm_archive
KGA_OBJECTS := \
	$(ARCHIVE_OBJECT_DIR)/kga_reader.o \
	$(ARCHIVE_OBJECT_DIR)/kga_sha256.o
QUERY_OBJECTS := $(ARCHIVE_OBJECT_DIR)/query_helper.o
INDEX_OBJECTS := $(ARCHIVE_OBJECT_DIR)/index_helper.o
MATERIALIZER_OBJECTS := $(ARCHIVE_OBJECT_DIR)/materializer.o $(KGA_OBJECTS)
DEPS := $(QUERY_OBJECTS:.o=.d) $(INDEX_OBJECTS:.o=.d) $(MATERIALIZER_OBJECTS:.o=.d)

.PHONY: native native-helper native-query-helper native-index-helper native-materializer native-test cbm-sdk

native: native-helper

native-helper: native-query-helper native-index-helper native-materializer

native-query-helper: $(QUERY_TARGET)
	$(QUERY_TARGET) --version

native-index-helper: $(INDEX_TARGET)
	$(INDEX_TARGET) --version

native-materializer: $(MATERIALIZER_TARGET)
	$(MATERIALIZER_TARGET) --version

native-test: native-helper
	GH_PULLER_TEST_CBM_NATIVE_HELPER=$(abspath $(QUERY_TARGET)) \
	GH_PULLER_TEST_CBM_NATIVE_INDEX_HELPER=$(abspath $(INDEX_TARGET)) \
	GH_PULLER_TEST_KGA_MATERIALIZER=$(abspath $(MATERIALIZER_TARGET)) \
	uv run pytest -q tests/codebase/test_cbm_native.py tests/codebase/test_build_pipeline.py \
		-m integration

cbm-sdk:
	$(MAKE) -C $(CBM_ROOT) -f Makefile.cbm cbm-sdk cbm-sdk-full build/c/prod_mimalloc.o

$(ARCHIVE_OBJECT_DIR)/kga_reader.o: $(CBM_ARCHIVE_DIR)/kga_reader.c Makefile
	@mkdir -p $(@D) $(NATIVE_LIB_DIR)
	$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

$(ARCHIVE_OBJECT_DIR)/kga_sha256.o: CFLAGS += -O3 -funroll-loops
$(ARCHIVE_OBJECT_DIR)/kga_sha256.o: $(CBM_ARCHIVE_DIR)/kga_sha256.c Makefile
	@mkdir -p $(@D) $(NATIVE_LIB_DIR)
	$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

$(ARCHIVE_OBJECT_DIR)/query_helper.o: $(CBM_ARCHIVE_DIR)/helper.c Makefile
	@mkdir -p $(@D) $(NATIVE_LIB_DIR)
	$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

$(ARCHIVE_OBJECT_DIR)/index_helper.o: $(CBM_ARCHIVE_DIR)/helper.c Makefile
	@mkdir -p $(@D) $(NATIVE_LIB_DIR)
	$(CC) $(CFLAGS) -DGHP_NATIVE_INDEXING -MMD -MP -c -o $@ $<

$(ARCHIVE_OBJECT_DIR)/materializer.o: $(CBM_ARCHIVE_DIR)/materializer.c Makefile
	@mkdir -p $(@D) $(NATIVE_LIB_DIR)
	$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

$(QUERY_TARGET): $(QUERY_OBJECTS) cbm-sdk
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) -o $@ $(QUERY_OBJECTS) $(CBM_SDK) $(CBM_MIMALLOC) $(LDFLAGS)
	@if nm $@ | grep -Eq 'ghp_kga_|tree_sitter_|cbm_mcp_server_|cbm_daemon_|cbm_pipeline_run'; then \
		echo "ERROR: native query helper links a forbidden CBM subsystem"; exit 1; fi

$(INDEX_TARGET): $(INDEX_OBJECTS) cbm-sdk
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) -o $@ $(INDEX_OBJECTS) $(CBM_SDK_FULL) $(CBM_MIMALLOC) $(LDFLAGS)
	@if nm $@ | grep -Eq 'ghp_kga_|cbm_mcp_server_|cbm_daemon_|cbm_cli_|cbm_watcher_'; then \
		echo "ERROR: native index helper links a frontend subsystem"; exit 1; fi

$(MATERIALIZER_TARGET): $(MATERIALIZER_OBJECTS) cbm-sdk
	@mkdir -p $(@D)
	$(CC) $(CFLAGS) -o $@ $(MATERIALIZER_OBJECTS) $(CBM_SDK) $(CBM_MIMALLOC) $(LDFLAGS)
	@if nm $@ | grep -Eq 'tree_sitter_|cbm_mcp_server_|cbm_daemon_|cbm_pipeline_run'; then \
		echo "ERROR: KGA materializer links a forbidden CBM subsystem"; exit 1; fi

-include $(DEPS)

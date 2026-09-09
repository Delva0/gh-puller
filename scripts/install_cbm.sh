#!/usr/bin/env bash
# Install an official CBM release or a source build of the Delva0 fork.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
OFFICIAL_INSTALLER_URL="https://raw.githubusercontent.com/DeusData/codebase-memory-mcp/main/install.sh"
FORK_REPOSITORY="https://github.com/Delva0/codebase-memory-mcp.git"
FORK_REF="${GH_PULLER_CBM_FORK_REF:-experiment/v5-native-change-journal}"
FORK_BUILD_DIR="build/gh-puller-install"
FORK_BUILDER_IMAGE="gh-puller-cbm-builder:local"
SOURCE="official"
TEMP_DIR=""
FORK_CHECKOUT=""
INSTALL_ARGS=()
NATIVE_COMPILER_ARGS=()

usage() {
    cat <<'EOF'
Usage: install_cbm.sh [--source official|fork] [INSTALL_OPTIONS...]

Sources:
  official  Download the latest DeusData release (default).
  fork      Build and install the Delva0 fork without using GitHub Releases.

The fork build uses CBM_ROOT when set, otherwise ../codebase-memory-mcp. If
neither checkout exists, it shallow-clones the Delva0 fork. Set
GH_PULLER_CBM_FORK_REF to select another remote branch or tag.
The build uses a local C/C++ toolchain when available. On Linux it falls back
to the fork's pinned Ubuntu build container and verifies the result locally.

INSTALL_OPTIONS are forwarded to the selected installer. Agent configuration
is always skipped. Common options include --dir PATH and --dir=PATH.
EOF
}

fail() {
    printf 'install_cbm: %s\n' "$*" >&2
    exit 2
}

cleanup() {
    [[ -z "$TEMP_DIR" ]] || rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

while (($#)); do
    case "$1" in
        --source=*)
            SOURCE="${1#--source=}"
            shift
            ;;
        --source)
            (($# >= 2)) || fail "--source requires official or fork"
            SOURCE="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        --)
            shift
            INSTALL_ARGS+=("$@")
            break
            ;;
        *)
            INSTALL_ARGS+=("$1")
            shift
            ;;
    esac
done

install_official() {
    TEMP_DIR="$(mktemp -d)"
    curl -fsSL "$OFFICIAL_INSTALLER_URL" -o "$TEMP_DIR/install.sh"
    bash "$TEMP_DIR/install.sh" --skip-config "${INSTALL_ARGS[@]}"
}

fork_checkout() {
    local default_checkout="$PROJECT_ROOT/../codebase-memory-mcp"
    if [[ -n "${CBM_ROOT:-}" ]]; then
        [[ -d "$CBM_ROOT" ]] || fail "CBM_ROOT is not a directory: $CBM_ROOT"
        FORK_CHECKOUT="$(cd -- "$CBM_ROOT" && pwd -P)"
    elif [[ -d "$default_checkout" ]]; then
        FORK_CHECKOUT="$(cd -- "$default_checkout" && pwd -P)"
    else
        command -v git >/dev/null || fail "git is required to clone the Delva0 fork"
        TEMP_DIR="$(mktemp -d)"
        git clone --depth 1 --single-branch --branch "$FORK_REF" \
            "$FORK_REPOSITORY" "$TEMP_DIR/codebase-memory-mcp" >&2
        FORK_CHECKOUT="$TEMP_DIR/codebase-memory-mcp"
    fi
}

select_native_compilers() {
    local cc
    if [[ -n "${CC:-}" || -n "${CXX:-}" ]]; then
        [[ -n "${CC:-}" && -n "${CXX:-}" ]] || fail "set both CC and CXX for the fork build"
        if ! command -v "$CC" >/dev/null || ! command -v "$CXX" >/dev/null; then
            fail "configured fork compilers were not found: CC=$CC CXX=$CXX"
        fi
        cc="$CC"
        NATIVE_COMPILER_ARGS=("CC=$CC" "CXX=$CXX")
    elif command -v gcc >/dev/null && command -v g++ >/dev/null; then
        cc="gcc"
        NATIVE_COMPILER_ARGS=(CC=gcc CXX=g++)
    elif command -v clang >/dev/null && command -v clang++ >/dev/null; then
        cc="clang"
        NATIVE_COMPILER_ARGS=(CC=clang CXX=clang++)
    elif command -v cc >/dev/null && command -v c++ >/dev/null; then
        cc="cc"
        NATIVE_COMPILER_ARGS=(CC=cc CXX=c++)
    else
        return 1
    fi
    command -v make >/dev/null || return 1
    printf '#include <zlib.h>\n' | "$cc" -E -x c - >/dev/null 2>&1
}

build_fork_with_docker() {
    local checkout="$1"
    local version="$2"
    local ccache_dir="$checkout/build/gh-puller-ccache"
    local dockerfile="$checkout/test-infrastructure/Dockerfile"
    [[ "$(uname -s)" == "Linux" ]] || \
        fail "fork builds outside Linux require a native C/C++ toolchain, make, and zlib"
    command -v docker >/dev/null || \
        fail "fork builds require a native toolchain or Docker"
    docker info >/dev/null 2>&1 || fail "Docker is installed but its daemon is unavailable"
    [[ -f "$dockerfile" ]] || fail "fork Docker builder not found: $dockerfile"

    printf 'Native build dependencies unavailable; using the CBM build container.\n'
    mkdir -p "$ccache_dir"
    docker build --tag "$FORK_BUILDER_IMAGE" --file "$dockerfile" \
        "$checkout/test-infrastructure"
    docker run --rm --user "$(id -u):$(id -g)" --env HOME=/tmp \
        --env CCACHE_DIR=/src/build/gh-puller-ccache --env CCACHE_MAXSIZE=1500M \
        --entrypoint bash --volume "$checkout:/src" --workdir /src "$FORK_BUILDER_IMAGE" \
        scripts/build.sh --version "$version" "BUILD_DIR=$FORK_BUILD_DIR" CC=gcc CXX=g++
}

build_fork() {
    local checkout="$1"
    local version="$2"
    if select_native_compilers; then
        "$checkout/scripts/build.sh" --version "$version" "BUILD_DIR=$FORK_BUILD_DIR" \
            "${NATIVE_COMPILER_ARGS[@]}"
    else
        build_fork_with_docker "$checkout" "$version"
    fi
}

install_fork() {
    local binary checkout revision version

    fork_checkout
    checkout="$FORK_CHECKOUT"
    [[ -x "$checkout/scripts/build.sh" ]] || fail "fork build script not found: $checkout/scripts/build.sh"

    revision="$(git -C "$checkout" rev-parse --short=12 HEAD 2>/dev/null || printf 'local')"
    if [[ -n "$(git -C "$checkout" status --short --untracked-files=no 2>/dev/null || true)" ]]; then
        revision="${revision}-dirty"
    fi
    version="delva-${revision}"

    build_fork "$checkout" "$version"
    binary="$checkout/$FORK_BUILD_DIR/codebase-memory-mcp"
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*) binary="${binary}.exe" ;;
    esac
    [[ -x "$binary" ]] || fail "fork build did not produce an executable: $binary"
    "$binary" --version >/dev/null || fail "fork build cannot run on this host"
    "$binary" install -y --force --skip-config "${INSTALL_ARGS[@]}"
}

case "$SOURCE" in
    official) install_official ;;
    fork) install_fork ;;
    *) fail "unknown source '$SOURCE'; expected official or fork" ;;
esac

#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(realpath "$SCRIPT_DIR/..")"
SYSTEMD_DIR="${GH_PULLER_SYSTEMD_DIR:-/etc/systemd/system}"
POLKIT_RULES_DIR="${GH_PULLER_POLKIT_RULES_DIR:-/etc/polkit-1/rules.d}"
SYSTEMCTL="${GH_PULLER_SYSTEMCTL:-systemctl}"
JOURNALCTL="${GH_PULLER_JOURNALCTL:-journalctl}"
SYSTEMD_ANALYZE="${GH_PULLER_SYSTEMD_ANALYZE:-systemd-analyze}"

usage() {
    cat <<'EOF'
Usage:
  github-puller-daemon.sh install OWNER/REPO DATABASE [PULLER_OPTIONS...]
  github-puller-daemon.sh uninstall DATABASE
  github-puller-daemon.sh start DATABASE
  github-puller-daemon.sh stop DATABASE
  github-puller-daemon.sh restart DATABASE
  github-puller-daemon.sh status [DATABASE]
  github-puller-daemon.sh logs DATABASE
  github-puller-daemon.sh render OWNER/REPO DATABASE [PULLER_OPTIONS...]

install registers a disabled, inactive system-level systemd service. The service
owner can then use start, stop, and restart without sudo. uninstall removes only
the service and its control policy; archives, environments, and source remain.
The schedule defaults to 1h; pass --interval DURATION after DATABASE to change it.
EOF
}

fail() {
    printf 'github-puller-daemon: %s\n' "$*" >&2
    exit 2
}

require_repository() {
    [[ "$1" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail "repository must be OWNER/REPO"
}

writer_digest() {
    printf '%s' "$1" | sha256sum | cut -d' ' -f1
}

writer_id() {
    writer_digest "$1" | cut -c1-12
}

unit_name() {
    printf 'gh-puller-%s.service\n' "$(writer_id "$1")"
}

policy_name() {
    printf '60-gh-puller-%s.rules\n' "$(writer_id "$1")"
}

require_system_access() {
    if [[ ("$SYSTEMD_DIR" == "/etc/systemd/system" || \
        "$POLKIT_RULES_DIR" == "/etc/polkit-1/rules.d") && EUID -ne 0 ]]; then
        fail "run this action with sudo"
    fi
}

service_user() {
    local user="${GH_PULLER_SERVICE_USER:-${SUDO_USER:-$(id -un)}}"
    [[ "$user" != "root" ]] || fail "set GH_PULLER_SERVICE_USER to a non-root account"
    id "$user" >/dev/null 2>&1 || fail "service user does not exist: $user"
    printf '%s\n' "$user"
}

uv_binary() {
    local user="$1"
    local home candidate
    if [[ -n "${GH_PULLER_UV_BIN:-}" ]]; then
        [[ -x "$GH_PULLER_UV_BIN" ]] || fail "GH_PULLER_UV_BIN is not executable"
        realpath "$GH_PULLER_UV_BIN"
        return
    fi
    home="$(getent passwd "$user" | cut -d: -f6)"
    for candidate in "$home/.local/bin/uv" /usr/local/bin/uv /usr/bin/uv; do
        if [[ -x "$candidate" ]]; then
            realpath "$candidate"
            return
        fi
    done
    fail "uv was not found for $user; set GH_PULLER_UV_BIN"
}

absolute_destination() {
    if [[ "$1" = /* ]]; then
        realpath -m "$1"
    else
        realpath -m "$PROJECT_ROOT/$1"
    fi
}

run_as_service_user() {
    local user="$1"
    shift
    if [[ EUID -eq 0 && "$(id -un)" != "$user" ]]; then
        runuser -u "$user" -- "$@"
    else
        "$@"
    fi
}

archive_repository() {
    local destination="$1"
    local user="$2"
    local uv="$3"
    [[ -s "$destination" ]] || return 0
    run_as_service_user "$user" "$uv" --directory "$PROJECT_ROOT" run --frozen python -c '
import sqlite3
import sys
from pathlib import Path

uri = f"{Path(sys.argv[1]).resolve().as_uri()}?mode=ro"
with sqlite3.connect(uri, uri=True) as db:
    metadata = dict(db.execute(
        "SELECT key, value FROM archive_meta WHERE key IN (?, ?, ?)",
        ("schema_version", "git_layout_version", "repository"),
    ))
if (
    metadata.get("schema_version") != "8"
    or metadata.get("git_layout_version") != "0"
    or "repository" not in metadata
):
    raise SystemExit(3)
print(metadata["repository"])
' "$destination"
}

configured_value() {
    local key="$1"
    local path="$2"
    sed -n "s/^# gh-puller-$key=//p" "$path" | head -n 1
}

configured_service_user() {
    local path="$1"
    local user
    user="$(configured_value user "$path")"
    if [[ -z "$user" ]]; then
        user="$(sed -n 's/^User=//p' "$path" | head -n 1)"
    fi
    printf '%s\n' "$user"
}

require_control_owner() {
    local path="$1"
    local owner
    owner="$(configured_service_user "$path")"
    [[ -n "$owner" ]] || fail "service has no owner; reinstall it with sudo"
    if [[ EUID -ne 0 && "$(id -un)" != "$owner" ]]; then
        fail "service belongs to $owner, not $(id -un)"
    fi
}

policy_path_for_unit() {
    local unit
    unit="$(basename "$1")"
    printf '%s/60-%s.rules\n' "$POLKIT_RULES_DIR" "${unit%.service}"
}

validate_unit_database() {
    local path="$1"
    local destination="$2"
    local configured_database
    [[ -e "$path" ]] || return 0
    configured_database="$(configured_value database "$path")"
    [[ -n "$configured_database" ]] || \
        fail "unit exists but is not managed by this installer: $(basename "$path")"
    [[ "$configured_database" == "$destination" ]] || fail "writer identity collision for $destination"
}

managed_unit_paths() {
    local destination="$1"
    local name path
    [[ -d "$SYSTEMD_DIR" ]] || return 0
    for path in "$SYSTEMD_DIR"/gh-puller-*.service; do
        [[ -e "$path" ]] || continue
        name="$(basename "$path")"
        [[ "$name" =~ ^gh-puller-([0-9a-f]{12}|[0-9a-f]{64})\.service$ ]] || continue
        if [[ "$(configured_value database "$path")" == "$destination" ]]; then
            printf '%s\n' "$path"
        fi
    done
}

resolve_managed_unit() {
    local action="$1"
    local raw="$2"
    local destination="$3"
    local canonical_path="$SYSTEMD_DIR/$(unit_name "$destination")"
    local -a paths
    mapfile -t paths < <(managed_unit_paths "$destination")
    if [[ ${#paths[@]} -eq 1 ]]; then
        validate_unit_database "${paths[0]}" "$destination"
        printf '%s\n' "${paths[0]}"
        return
    fi
    if [[ ${#paths[@]} -gt 1 ]]; then
        fail "multiple managed writers for database: $destination"
    fi
    if [[ ! -e "$canonical_path" ]]; then
        if [[ "$raw" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
            fail "$action expects DATABASE; '$raw' looks like OWNER/REPO"
        fi
        fail "no managed writer for database: $destination"
    fi
    validate_unit_database "$canonical_path" "$destination"
    fail "no managed writer for database: $destination"
}

unit_quote() {
    local value="$1"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "unit arguments cannot contain newlines"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//%/%%}"
    value="${value//\$/\$\$}"
    printf '"%s"' "$value"
}

unit_value() {
    local value="$1"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] || fail "unit values cannot contain newlines"
    value="${value//\\/\\x5c}"
    value="${value// /\\x20}"
    value="${value//$'\t'/\\x09}"
    value="${value//%/%%}"
    value="${value//\$/\$\$}"
    printf '%s' "$value"
}

render_unit() {
    local repository="$1"
    local destination="$2"
    local user="$3"
    local group="$4"
    local uv="$5"
    shift 5
    local identity argument
    identity="$(writer_id "$destination")"
    printf '# gh-puller-repository=%s\n' "$repository"
    printf '# gh-puller-database=%s\n' "$destination"
    printf '# gh-puller-user=%s\n' "$user"
    printf '[Unit]\n'
    printf 'Description=gh-puller writer for %s from %s\n' "$(unit_value "$destination")" "$repository"
    printf 'Wants=network-online.target\n'
    printf 'After=network-online.target\n'
    printf 'StartLimitIntervalSec=0\n\n'
    printf '[Service]\n'
    printf 'Type=simple\n'
    printf 'User=%s\n' "$user"
    printf 'Group=%s\n' "$group"
    printf 'WorkingDirectory=%s\n' "$(unit_value "$PROJECT_ROOT")"
    printf 'Environment=PYTHONUNBUFFERED=1\n'
    printf 'Environment=UV_NO_PROGRESS=1\n'
    printf 'UMask=0077\n'
    printf 'SyslogIdentifier=gh-puller-%s\n' "$identity"
    printf 'ExecStart=%s --directory %s run --frozen -m gh_puller.github schedule %s %s' \
        "$(unit_quote "$uv")" \
        "$(unit_quote "$PROJECT_ROOT")" \
        "$(unit_quote "$repository")" \
        "$(unit_quote "$destination")"
    for argument in "$@"; do
        printf ' %s' "$(unit_quote "$argument")"
    done
    printf '\n'
    printf 'Restart=always\n'
    printf 'RestartSec=60s\n'
    printf 'KillSignal=SIGTERM\n'
    printf 'TimeoutStopSec=2min\n'
    printf 'SuccessExitStatus=143\n\n'
    printf '[Install]\n'
    printf 'WantedBy=multi-user.target\n'
}

policy_string() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '%s' "$value"
}

render_policy() {
    local unit="$1"
    local user="$2"
    printf 'polkit.addRule(function(action, subject) {\n'
    printf '    if (action.id == "org.freedesktop.systemd1.manage-units" &&\n'
    printf '        subject.user == "%s" &&\n' "$(policy_string "$user")"
    printf '        action.lookup("unit") == "%s") {\n' "$(policy_string "$unit")"
    printf '        var verb = action.lookup("verb");\n'
    printf '        if (verb == "start" || verb == "stop" || verb == "restart") {\n'
    printf '            return polkit.Result.YES;\n'
    printf '        }\n'
    printf '    }\n'
    printf '});\n'
}

render_for() {
    local repository="$1"
    local destination="$2"
    shift 2
    local user group uv
    user="$(service_user)"
    group="$(id -gn "$user")"
    uv="$(uv_binary "$user")"
    render_unit "$repository" "$(absolute_destination "$destination")" "$user" "$group" "$uv" "$@"
}

monitor_status() {
    local user uv
    local -a arguments
    user="$(service_user)"
    uv="$(uv_binary "$user")"
    arguments=(
        --systemd-dir "$SYSTEMD_DIR"
        --systemctl "$SYSTEMCTL"
        --journalctl "$JOURNALCTL"
    )
    if [[ $# -eq 1 ]]; then
        arguments+=(--database "$1")
    fi
    exec "$uv" --directory "$PROJECT_ROOT" run --frozen -m gh_puller.github.monitor "${arguments[@]}"
}

action="${1:-}"
if [[ -z "$action" || "$action" == "-h" || "$action" == "--help" ]]; then
    usage
    exit 0
fi

case "$action" in
    render)
        [[ $# -ge 3 ]] || fail "database is required"
        repository="$2"
        require_repository "$repository"
        render_for "$repository" "$3" "${@:4}"
        ;;
    install)
        [[ $# -ge 3 ]] || fail "database is required"
        repository="$2"
        require_repository "$repository"
        require_system_access
        destination="$(absolute_destination "$3")"
        unit="$(unit_name "$destination")"
        unit_path="$SYSTEMD_DIR/$unit"
        policy_path="$POLKIT_RULES_DIR/$(policy_name "$destination")"
        user="$(service_user)"
        group="$(id -gn "$user")"
        uv="$(uv_binary "$user")"
        mapfile -t existing_paths < <(managed_unit_paths "$destination")
        if ! bound_repository="$(archive_repository "$destination" "$user" "$uv" 2>/dev/null)"; then
            fail "database is not a valid gh-puller archive: $destination"
        fi
        if [[ -n "$bound_repository" && "$bound_repository" != "$repository" ]]; then
            fail "database belongs to $bound_repository, not $repository"
        fi
        validate_unit_database "$unit_path" "$destination"
        for existing_path in "${existing_paths[@]}"; do
            configured_repository="$(configured_value repository "$existing_path")"
            configured_database="$(configured_value database "$existing_path")"
            [[ -n "$configured_repository" && -n "$configured_database" ]] || \
                fail "unit exists but is not managed by this installer: $(basename "$existing_path")"
            [[ "$configured_database" == "$destination" ]] || fail "writer identity collision for $destination"
            [[ "$configured_repository" == "$repository" ]] || \
                fail "database writer is configured for $configured_repository, not $repository"
            configured_user="$(configured_service_user "$existing_path")"
            [[ -z "$configured_user" || "$configured_user" == "$user" ]] || \
                fail "database writer belongs to $configured_user, not $user"
        done
        mkdir -p "$SYSTEMD_DIR"
        mkdir -p "$POLKIT_RULES_DIR"
        verify_dir="$(mktemp -d)"
        trap 'rm -r -- "$verify_dir"' EXIT
        render_unit "$repository" "$destination" "$user" "$group" "$uv" "${@:4}" >"$verify_dir/$unit"
        render_policy "$unit" "$user" >"$verify_dir/$(basename "$policy_path")"
        "$SYSTEMD_ANALYZE" verify "$verify_dir/$unit"
        for existing_path in "${existing_paths[@]}"; do
            [[ "$existing_path" == "$unit_path" ]] && continue
            existing_unit="$(basename "$existing_path")"
            "$SYSTEMCTL" disable --now "$existing_unit" >/dev/null
        done
        install -m 0644 "$verify_dir/$unit" "$unit_path"
        install -m 0644 "$verify_dir/$(basename "$policy_path")" "$policy_path"
        for existing_path in "${existing_paths[@]}"; do
            existing_policy="$(policy_path_for_unit "$existing_path")"
            if [[ "$existing_path" != "$unit_path" ]]; then
                rm -f -- "$existing_path"
            fi
            if [[ "$existing_policy" != "$policy_path" ]]; then
                rm -f -- "$existing_policy"
            fi
        done
        "$SYSTEMCTL" daemon-reload
        "$SYSTEMCTL" disable --now "$unit" >/dev/null
        "$SYSTEMCTL" reset-failed "$unit" >/dev/null 2>&1 || true
        printf 'Installed %s (disabled, inactive)\n' "$unit"
        printf 'Start:  %q start %q\n' "$0" "$destination"
        printf 'Status: %q status %q\n' "$0" "$destination"
        printf 'Logs:   %q logs %q\n' "$0" "$destination"
        ;;
    uninstall)
        [[ $# -eq 2 ]] || fail "uninstall accepts only DATABASE"
        require_system_access
        destination="$(absolute_destination "$2")"
        unit="$(unit_name "$destination")"
        unit_path="$SYSTEMD_DIR/$unit"
        mapfile -t existing_paths < <(managed_unit_paths "$destination")
        if [[ ${#existing_paths[@]} -eq 0 ]]; then
            validate_unit_database "$unit_path" "$destination"
            existing_paths=("$unit_path")
        fi
        for existing_path in "${existing_paths[@]}"; do
            existing_unit="$(basename "$existing_path")"
            "$SYSTEMCTL" disable --now "$existing_unit" >/dev/null 2>&1 || true
            rm -f -- "$existing_path"
            rm -f -- "$(policy_path_for_unit "$existing_path")"
        done
        "$SYSTEMCTL" daemon-reload
        for existing_path in "${existing_paths[@]}"; do
            "$SYSTEMCTL" reset-failed "$(basename "$existing_path")" >/dev/null 2>&1 || true
        done
        printf 'Uninstalled %s\n' "$unit"
        printf 'SQLite archives, Git object stores, .env, environments, and source files were preserved.\n'
        ;;
    start|stop|restart)
        [[ $# -eq 2 ]] || fail "$action accepts only DATABASE"
        destination="$(absolute_destination "$2")"
        unit_path="$(resolve_managed_unit "$action" "$2" "$destination")"
        require_control_owner "$unit_path"
        unit="$(basename "$unit_path")"
        "$SYSTEMCTL" "$action" "$unit"
        if [[ "$action" != "stop" ]]; then
            "$SYSTEMCTL" is-active --quiet "$unit"
        fi
        ;;
    status)
        [[ $# -le 2 ]] || fail "status accepts at most one DATABASE"
        if [[ $# -eq 1 ]]; then
            monitor_status
        fi
        destination="$(absolute_destination "$2")"
        resolve_managed_unit status "$2" "$destination" >/dev/null
        monitor_status "$destination"
        ;;
    logs)
        [[ $# -eq 2 ]] || fail "logs accepts only DATABASE"
        destination="$(absolute_destination "$2")"
        unit_path="$(resolve_managed_unit logs "$2" "$destination")"
        unit="$(basename "$unit_path")"
        exec "$JOURNALCTL" --unit "$unit" --output=short-full --lines=100 --follow
        ;;
    *)
        usage >&2
        fail "unknown action: $action"
        ;;
esac

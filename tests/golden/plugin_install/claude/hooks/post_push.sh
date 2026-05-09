#!/usr/bin/env bash
# Eä-managed Claude Code hook wrapper for post_push.
# Generator: eawf plugin install claude (managed file — re-render via
# `eawf plugin update claude`; hand-edits are detected by `eawf plugin doctor`).
set -euo pipefail

# Synthesise a JSON payload from positional args (Claude passes args, not stdin).
# We escape only the minimum set of characters required for a JSON string body
# (backslash + double-quote) so the payload is portable across bash versions.
_eawf_json_escape() {
    local s="${1-}"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

_eawf_arg1="$(_eawf_json_escape "${1-}")"
_eawf_arg2="$(_eawf_json_escape "${2-}")"
_eawf_arg3="$(_eawf_json_escape "${3-}")"
_eawf_arg4="$(_eawf_json_escape "${4-}")"

_eawf_payload=$(printf '{"hook_event_name":"%s","claude_event_name":"%s","args":["%s","%s","%s","%s"]}' \
    "PostToolUse" \
    "post_push" \
    "${_eawf_arg1}" \
    "${_eawf_arg2}" \
    "${_eawf_arg3}" \
    "${_eawf_arg4}")

printf '%s' "${_eawf_payload}" | exec uv run eawf hook run post_push --runtime claude

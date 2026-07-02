#!/usr/bin/env bash
# Eä-managed Claude Code hook wrapper for wave_open.
# Generator: eawf plugin install claude (managed file — re-render via
# `eawf plugin update claude`; hand-edits are detected by `eawf plugin doctor`).
set -euo pipefail

# Escape the minimum set of characters required for a JSON string body
# (backslash + double-quote) so the fallback payload is portable across
# bash versions. Only used for the positional-arg fallback below.
_eawf_json_escape() {
    local s="${1-}"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# Claude Code delivers hook input as a JSON document on stdin; SessionEnd,
# Stop, and SubagentStop carry the session cost + token usage totals that
# feed EU capture. Read that document verbatim. The read is fail-open: a
# hook that dies breaks the operator's session, so an interactive TTY or an
# empty pipe leaves the payload blank and the positional-arg fallback runs.
_eawf_stdin=""
if [ ! -t 0 ]; then
    _eawf_stdin="$(cat)"
fi

if [ -n "${_eawf_stdin}" ]; then
    _eawf_payload="${_eawf_stdin}"
else
    _eawf_arg1="$(_eawf_json_escape "${1-}")"
    _eawf_arg2="$(_eawf_json_escape "${2-}")"
    _eawf_arg3="$(_eawf_json_escape "${3-}")"
    _eawf_arg4="$(_eawf_json_escape "${4-}")"
    _eawf_payload=$(printf '{"hook_event_name":"%s","claude_event_name":"%s","args":["%s","%s","%s","%s"]}' \
        "wave_open" \
        "wave_open" \
        "${_eawf_arg1}" \
        "${_eawf_arg2}" \
        "${_eawf_arg3}" \
        "${_eawf_arg4}")
fi

printf '%s' "${_eawf_payload}" | exec uv run eawf hook run wave_open --runtime claude

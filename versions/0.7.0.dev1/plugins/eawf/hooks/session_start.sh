#!/usr/bin/env bash
# Eä-managed hook wrapper for session_start (codex runtime).
# Generator: eawf plugin install codex (managed file — re-render via
# `eawf plugin update codex`; hand-edits are detected by `eawf plugin doctor`).
set -euo pipefail

# PATH bootstrap: a hook fired from inside a git worktree (or a GUI-launched
# agent) can inherit a minimal PATH that omits the uv install dir, so a bare
# `uv` dies with exit 127 (command not found). Append the common single-user
# install locations so both `uv` and the `eawf` console script it runs resolve
# even from a stripped PATH. Appending (not prepending) leaves any `uv`
# already on PATH authoritative and only adds fallbacks when it is missing.
for _eawf_bin in "${HOME:-}/.local/bin" "${HOME:-}/.cargo/bin" /opt/homebrew/bin /usr/local/bin; do
    case ":${PATH}:" in
        *":${_eawf_bin}:"*) ;;
        *)
            if [ -d "${_eawf_bin}" ]; then
                PATH="${PATH}:${_eawf_bin}"
            fi
            ;;
    esac
done
export PATH

# Resolve uv to an absolute interpreter path so the exec below never depends
# on a further PATH lookup inside the worktree. Fall back to the bare name
# when nothing is found so the exec fails loudly rather than silently.
_eawf_uv="$(command -v uv 2>/dev/null || true)"
if [ -z "${_eawf_uv}" ]; then
    _eawf_uv="uv"
fi

# Escape the minimum set of characters required for a JSON string body
# (backslash + double-quote) so the fallback payload is portable across
# bash versions. Only used for the positional-arg fallback below.
_eawf_json_escape() {
    local s="${1-}"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    printf '%s' "$s"
}

# The provider delivers hook input as a JSON document on stdin. Claude points
# at transcripts carrying usage; Codex points at exact session/subagent rollout
# files. Read that document verbatim. The read is fail-open: a
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
        "SessionStart" \
        "session_start" \
        "${_eawf_arg1}" \
        "${_eawf_arg2}" \
        "${_eawf_arg3}" \
        "${_eawf_arg4}")
fi

printf '%s' "${_eawf_payload}" | exec "${_eawf_uv}" run eawf hook run session_start --runtime codex >/dev/null

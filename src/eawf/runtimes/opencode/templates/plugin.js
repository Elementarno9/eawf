// Eä-owned OpenCode plugin bridge.
//
// OpenCode plugins are loaded by ID via `opencode.json`. This file is
// the untyped JS surface the OpenCode CLI executes; every hook event
// is forwarded to `eawf hook` over stdio so the typed Python side does
// the actual work. No TypeScript / build step — the bridge is
// intentionally minimal.
//
// Region markers `__eawf_managed begin/end` exist so the install
// renderer can patch the body without disturbing user-authored content
// (none expected in this file, but the markers keep the contract
// uniform with the codex `config.toml`).

// ---- __eawf_managed begin ----
'use strict';

const { spawnSync } = require('node:child_process');

const EAWF_HOOK_TIMEOUT_MS = 30_000;

function dispatchHook(eventType, payload) {
  const stdinJson = JSON.stringify({
    event_type: eventType,
    payload: payload || {},
  });
  const result = spawnSync('eawf', ['hook', 'run', eventType, '--runtime', 'opencode'], {
    input: stdinJson,
    encoding: 'utf-8',
    timeout: EAWF_HOOK_TIMEOUT_MS,
  });
  if (result.status !== 0) {
    return { ok: false, stderr: result.stderr || '', stdout: result.stdout || '' };
  }
  return { ok: true, stdout: result.stdout || '' };
}

module.exports = {
  name: 'eawf',
  version: '__EAWF_PLUGIN_VERSION__',
  hooks: {
    onSessionStart: (ctx) => dispatchHook('session_start', ctx),
    onSessionEnd: (ctx) => dispatchHook('session_end', ctx),
    onPreCommit: (ctx) => dispatchHook('pre_commit', ctx),
    onPostCommit: (ctx) => dispatchHook('post_commit', ctx),
    onAgentEnd: (ctx) => dispatchHook('agent_end', ctx),
  },
};
// ---- __eawf_managed end ----

# Windows named-pipe transport — threat model

*Why the daemon's per-user named pipe is safe on Windows, and the one accepted residual: a single bounded pre-verify read.*

## Context

On POSIX the daemon listens on a Unix domain socket guarded by a peer-credential (SO_PEERCRED / LOCAL_PEERCRED) check; on Windows there are no Unix domain sockets, so the daemon transport becomes a per-user named pipe `\\.\pipe\eawfd-<username>`. The transport must give the same guarantee the UDS gives: only the owning operator's processes can drive the daemon's mutators.

Two gates enforce that on the pipe:

1. **Owner-only DACL (primary gate).** Every pipe instance is created with a `SECURITY_ATTRIBUTES` whose discretionary access-control list (DACL) grants full control to exactly one SID — the running user's — and grants nothing to `Everyone` or `Authenticated Users` (`build_user_only_security_attributes`). The Windows kernel enforces the DACL at `CreateFile` time: a process running as a different user is refused the open before any application code runs. This is the load-bearing control.

2. **Post-connect SID verify (defence in depth).** After a client connects, the listener impersonates the named-pipe client (`win32pipe.ImpersonateNamedPipeClient`), opens the resulting thread token (`win32security.OpenThreadToken`), reads the token user SID (`win32security.GetTokenInformation(..., win32security.TokenUser)`), and compares that SID to the daemon owner's SID (`verify_peer_sid`). A mismatch closes the connection with a JSON-RPC `-32000 unauthorized` envelope. This catches DACL-bypass scenarios such as a malicious second pipe instance racing the daemon's accept loop.

## The accepted residual: one bounded pre-verify read

`ImpersonateNamedPipeClient` has a precondition: the server must have **read from the pipe first** so the client's security context is established for the impersonation. The Windows documentation is explicit that impersonating before any read is unreliable. The listener therefore reads **one bounded chunk** of the request *before* the SID verify (`_read_first_chunk`), then verifies the SID, then — only if the SID passes — drains any `ERROR_MORE_DATA` tail of a larger frame.

The residual this introduces: an unauthenticated peer who has already passed the kernel DACL check can move **at most one `_PIPE_BUFFER_BYTES` (64 KB) chunk** of bytes into the daemon process before the SID verify rejects it. The bytes are read into a bounded buffer and discarded on rejection; they are never parsed, dispatched, or used to mutate state.

### API spelling reconciliation

The implementation intentionally uses the pywin32 spellings `win32pipe.ImpersonateNamedPipeClient`, `win32security.OpenThreadToken`, and `win32security.GetTokenInformation(..., win32security.TokenUser)`. Earlier notes used shorter prose names for the same calls and mentioned process-id-oriented helpers; those helpers are not the verifier. The verifier authenticates the impersonated client's token SID, not a process id, because the SID is the authorization principal enforced by the DACL.

### Why this residual is acceptable

- **The DACL already gates the open.** To reach the pre-verify read at all, a process must first pass the kernel-enforced owner-only DACL at `CreateFile`. A non-owner is refused before the listener thread ever calls `ReadFile`. The pre-verify read is reachable only by a process the kernel already attributes to the owning user (or by an exotic DACL-bypass that the second gate, the SID verify, then catches).
- **The read is bounded.** It is a single 64 KB chunk, not the unbounded reassembly loop. A malicious peer cannot stream arbitrary volume pre-verify; the worst case is one buffer that is read and dropped.
- **No dispatch pre-verify.** The bytes never reach `process_frame_bytes` until after the SID check passes. There is no parse, no mutation, no side effect on the pre-verify path.
- **It is required by the platform.** Reversing the order (verify-before-read) makes `ImpersonateNamedPipeClient` unreliable, which would *weaken* the second gate — the read-before-impersonate order is what makes the SID verify trustworthy.

### Remediation considered and deferred

A stricter design would cap the pre-verify read below 64 KB (e.g. read only the first few bytes needed to establish the context). This was considered and deferred: the chunk size is already bounded, the DACL is the real gate, and a sub-chunk read adds complexity (a second reassembly seam) for no change to the threat surface — the residual is a *bounded discarded buffer behind a kernel DACL*, not an unbounded ingress. If a future audit shows the DACL can be bypassed at scale, the remediation is to shrink the pre-verify read, not to remove the read (which the platform requires).

## Proposed Decision row

The orchestrator records the following typed `Decision` via the daemon (this note does not run `eawf decision`). Proposed content:

- **id:** `D-WINPIPE-01`
- **scope_id:** `EAWF`
- **title:** `Accept one bounded 64KB pre-verify pipe read; DACL is the gate`
- **status:** `active`
- **rationale:** The Windows named-pipe transport reads one bounded `_PIPE_BUFFER_BYTES` (64 KB) chunk before the post-connect SID verify because `ImpersonateNamedPipeClient` requires a prior read to establish the client security context. The owner-only pipe DACL is the primary gate — the kernel refuses a non-owner `CreateFile` before any read — so the pre-verify read is reachable only by a process the kernel already attributes to the owning user, the bytes are bounded and discarded on rejection, and nothing is parsed or dispatched pre-verify. Accept the residual rather than reverse the order (which would make the SID verify unreliable).
- **alternatives:** `["verify-before-read (rejected: makes ImpersonateNamedPipeClient unreliable, weakening the second gate)", "sub-chunk pre-verify read (deferred: adds a reassembly seam for no change to the bounded-discarded-buffer-behind-a-DACL threat surface)"]`
- **consequences:** `["An unauthenticated-but-DACL-passing peer can move at most one 64KB discarded chunk pre-verify; never parsed or dispatched.", "The read-before-impersonate order keeps the post-connect SID verify trustworthy.", "Remediation path if the DACL is ever bypassable at scale: shrink the pre-verify read, do not remove it."]`

## References

- `src/eawf/runtime/daemon/windows_security.py` — `build_user_only_security_attributes` (DACL), `verify_peer_sid` (post-connect SID check).
- `src/eawf/runtime/daemon/windows_pipe.py` — `_read_first_chunk` (bounded pre-verify read), `_listen_loop` (read-before-impersonate order), `_read_full_message` (post-verify MORE_DATA reassembly).
- Microsoft Learn: *ImpersonateNamedPipeClient* — the read-before-impersonate precondition.

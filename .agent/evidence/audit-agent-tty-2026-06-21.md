---
contract_id: audit-agent-tty
version: 1
type: evidence
date: 2026-06-21
repo: https://github.com/rangersui/agent-tty
commit: 803bd4ece7a9ffd328a6cfe720b08252d13363b8
commit_short: 803bd4e
commit_date: 2026-06-21T07:41:14+10:00
commit_message: add readme pun
scope_status:
  src/agent_tty/__init__.py: reviewed — no findings
  src/agent_tty/__main__.py: reviewed — no findings
  src/agent_tty/_shared.py: reviewed — no findings
  src/agent_tty/cli.py: reviewed — findings below
  src/agent_tty/monitor.py: reviewed — no findings
precedents_checked:
  - id: event-spoof-accepted
    matched: yes — suppressed security finding (local event spoofing via log write)
    cited: below
  - id: status-no-side-effects
    matched: yes — verified cmd_status uses idempotent pipe_start, no keystroke injection
    cited: below
---

# Audit Findings: agent-tty src/agent_tty/

## Summary

5 files reviewed, 1424+273+189 = 1886 lines.

0 P1 findings.
0 P2 findings.
5 P3 findings.

This is a well-hardened codebase. Security invariants (path
traversal prevention, cell_id validation, private file
permissions, atomic writes, symlink rejection, lock-file
mutex) are consistently enforced. The type-seal pattern in
_shared.py (single source of truth for event wire format)
eliminates the class of protocol-drift bugs it was designed
to prevent. Error handling is thorough — failures are caught,
warned, and surfaced rather than swallowed.

## P3 Findings

### P3-1. `last_clean` state leak across ECHOING→OUTPUT transition
File: cli.py:667,810
Evidence type: analytical

`last_clean` is updated on every line (line 810) including
during ECHOING state, but `repeat_count` only increments
during OUTPUT state. When transitioning from ECHOING to
OUTPUT, `last_clean` carries the last echoed line. If the
first real output line matches the last echo, `repeat_count`
starts at 1 instead of 0 — frame detection needs 3 more
matching lines instead of 4.

Practical impact: near-zero. Echo = typed command, output =
response. They almost never match, and even when they do,
you still need 4 total identical lines.

### P3-2. `T.send` appears unused
File: cli.py:105-106
Evidence type: analytical (grep-level — not deterministic, file is 1424 lines)

`T.send()` is defined but no call site found in cli.py or
monitor.py. `_send_code` uses paste-buffer, `_send_frame_enters`
uses subprocess.run directly. If confirmed unused, it is dead
code.

### P3-3. `cmd_notify` reads `/proc/{ppid}/comm` — Linux-only
File: cli.py:1163
Evidence type: analytical

Falls back to "?" on non-Linux POSIX systems. The `who` field
in notify events silently degrades. Not a correctness issue
(notification still works), but an implicit platform dependency
beyond the stated POSIX requirement.

### P3-4. `_looks_like_bash` only matches "bash"
File: cli.py:843-859
Evidence type: analytical

The source-bash optimization (write multiline code to a
per-cell script, then `source` it) only activates for sessions
whose command resolves to `bash`. Other POSIX shells (`sh`,
`zsh`) that support `source` don't get the optimization.
Multiline code in those sessions goes through the paste-buffer
path with the echo_count heuristic, which is a known limitation
documented in the README.

### P3-5. `cmd_kill` does not hold LockGuard
File: cli.py:1199-1208
Evidence type: analytical

`cmd_kill` reads lock metadata and kills the watcher without
holding the session lock. A concurrent `cmd_fire` in the
window between `_load_cell` and `T.kill` could spawn a
watcher whose pgid isn't in the metadata cmd_kill read.
That watcher becomes orphaned (its session dir is deleted
in step 5).

(inferred) This is intentional: cmd_kill is the nuclear
recovery option. Requiring the lock would make it fail
precisely when recovery is needed most (hung lock, corrupted
state). The orphaned watcher self-cleans on timeout.

## Precedent Applications

### event-spoof-accepted (status: accepted_risk)
Any local process can call `k notify` or write directly to
the session log, spoofing events. Not flagged per precedent.
Verified: the threat model explicitly excludes malicious
local processes.

### status-no-side-effects (status: active)
Verified: `cmd_status` (cli.py:1213-1238) calls
`T.pipe_start(session, logpath)` which is idempotent
(replaces dead/existing pipe). No send-keys, no keystroke
injection. The comment on line 1217 documents this explicitly.
Compliant.

## Unresolved Items

None. All files in scope fully reviewed.

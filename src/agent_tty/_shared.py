"""agent-tty shared infrastructure — single source of truth.

Type seal for the event wire protocol:
- cell_event() is the ONLY constructor for cell event strings
- CELL_END_RE is derived from TERMINAL — adding a new terminal status
  automatically updates both the format function and the parser regex
- No parallel raw API: cli.py and monitor.py import, never redefine

Anti-pattern this prevents:
  cli.py writes f"── cell:{cid} newstatus ──"
  monitor.py's END_RE doesn't match "newstatus"
  → km -1 hangs forever (silent protocol drift)
"""

import os, re, shlex, shutil, sys

if os.name != "posix":
    print("ERR agent-tty requires POSIX: tmux + tail + POSIX signals", file=sys.stderr)
    sys.exit(1)

# ═══════════════════════════════════════════
# TMUX
# ═══════════════════════════════════════════
TMUX = shutil.which("tmux") or "tmux"

# ═══════════════════════════════════════════
# FRAME DETECTION
# ═══════════════════════════════════════════
FRAME_ENTERS = 5  # consecutive identical lines to detect frame end

# ═══════════════════════════════════════════
# ANSI STRIPPING
# ═══════════════════════════════════════════
ANSI_RE = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[<[0-9;]*[mM]|\x1b\[\?[0-9;]*[hlsr]"
    r"|\x1b\][^\x07]*\x07|\x1b\][^\x1b]*\x1b\\|\x1b[()][0-9A-B]"
    r"|\x1b[>=]|\x1b\x50[^\x1b]*\x1b\\|\x08|\r"
)

# ═══════════════════════════════════════════
# STATUS CONSTANTS — sealed set
# Typo in a constant name → NameError (fail loud)
# Typo in a bare string   → silent mismatch
# ═══════════════════════════════════════════
FIRED = "fired"
DONE = "done"
TIMEOUT = "timeout"
INTERRUPTED = "interrupted"
RUNNING = "running"
ERROR = "error"
NOTIFY = "notify"
CLOSED = "closed"

# Terminal statuses: cell reached an end state
TERMINAL = frozenset({DONE, TIMEOUT, INTERRUPTED})

# ═══════════════════════════════════════════
# EVENT WIRE FORMAT — type seal
#
# cell_event() validates status against _CELL_STATUSES.
# CELL_END_RE is generated from TERMINAL.
# Adding a new terminal status to TERMINAL automatically
# updates both the format function and the parser regex.
# ═══════════════════════════════════════════
_CELL_STATUSES = frozenset({FIRED}) | TERMINAL

def cell_event(cell_id: str, status: str) -> str:
    """Format a cell event line. Validates status — typo → ValueError."""
    cell_id = validate_cell_id(cell_id)
    if status not in _CELL_STATUSES:
        raise ValueError(f"invalid cell event status: {status!r}")
    return f"── cell:{cell_id} {status} ──"

def notify_event(who: str, message: str) -> str:
    """Format a notify event line."""
    return f"── notify [{who}] {message} ──"

# Parsing regexes — derived from the same status constants
CELL_EVENT_RE = re.compile(
    r"^── cell:([0-9a-f]{12}) (" + "|".join(sorted(_CELL_STATUSES)) + r") ──$"
)
CELL_START_RE = re.compile(r"^── cell:([0-9a-f]{12}) " + FIRED + r" ──$")
CELL_END_RE = re.compile(
    r"^── cell:([0-9a-f]{12}) (" + "|".join(sorted(TERMINAL)) + r") ──$"
)
NOTIFY_EVENT_RE = re.compile(r"^── notify \[(.+?)\] (.+) ──$")

# ═══════════════════════════════════════════
# SESSION NAME VALIDATION
# ═══════════════════════════════════════════
_SAFE_NAME = re.compile(r'^[A-Za-z0-9_.-]+$')
CELL_ID_RE = re.compile(r'^[0-9a-f]{12}$')

def validate_name(name: str, prefix: str = "ERR"):
    """Reject path traversal / injection. Exits on invalid — protocol-level rejection."""
    if not name or not _SAFE_NAME.match(name) or '..' in name:
        print(f"{prefix} invalid session name: {name!r}", file=sys.stderr)
        sys.exit(1)

def validate_cell_id(cell_id: str) -> str:
    """Return a path-safe cell id or raise ValueError."""
    if not cell_id or not CELL_ID_RE.match(cell_id):
        raise ValueError(f"invalid cell_id: {cell_id!r}")
    return cell_id

def ensure_private_dir(path: str) -> str:
    """Create/verify a private 0700 directory owned by the current user."""
    if not os.path.isdir(path):
        os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if os.path.islink(path) or st.st_uid != os.getuid():
        print(f"ERR: {path} not owned by current user or is a symlink", file=sys.stderr)
        sys.exit(1)
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path

# ═══════════════════════════════════════════
# PER-USER STATE DIRECTORY
# ═══════════════════════════════════════════
def _cell_dir() -> str:
    """Per-user 0700 state directory. Rejects symlinks and wrong ownership."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        base = os.path.join(runtime, "k_cells")
    else:
        base = f"/tmp/k_cells_{os.getuid()}"
    return ensure_private_dir(base)

CELL_DIR = _cell_dir()

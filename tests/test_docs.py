#!/usr/bin/env python3
"""Documentation drift tests for README.md and SKILL.md."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
SKILL = (ROOT / "SKILL.md").read_text(encoding="utf-8")
K_HELP = (ROOT / "src" / "agent_tty" / "cli.py").read_text(encoding="utf-8")
TEST_SH = (ROOT / "tests" / "test.sh").read_text(encoding="utf-8")
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8") if (ROOT / ".github" / "workflows" / "ci.yml").exists() else ""
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{name}: {detail}".rstrip(": "))


def max_runtime_checks() -> int:
    check_calls = len(re.findall(r'^\s*check\s+"', TEST_SH, flags=re.M))
    manual_checks = len(re.findall(r'&&\s*PASS=\$\(\(PASS\+1\)\)', TEST_SH))
    return check_calls + manual_checks


expected_checks = max_runtime_checks()

# ── command coverage ──
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL)):
    check(f"{doc_name}: runtime check count", f"{expected_checks} tests" in text, f"expected '{expected_checks} tests'")
    for command in ("new", "fire", "poll", "run", "await", "notify", "int", "kill", "status", "watch", "history"):
        check(f"{doc_name}: command {command}", f"k {command}" in text)
    for script in (
        "tests/test_contracts.py",
        "tests/test_docs.py",
        "tests/test.sh",
        "tests/test_regressions.py",
        "tests/run_all.py",
    ):
        check(f"{doc_name}: mentions {script}", script in text)

check("README.md: no stale line counts", not re.search(r"scripts/(?:k|km)\s+\d+\s+lines", README))
check("README.md: no stale test.sh line count", not re.search(r"test\.sh\s+\d+\s+lines", README))
check("repo: runtime test lives under tests", (ROOT / "tests" / "test.sh").exists())
check("repo: no root test.sh", not (ROOT / "test.sh").exists())

# ── CI coverage ──
check("CI: workflow exists", bool(CI))
for os_name in ("ubuntu-latest", "macos-latest", "windows-latest"):
    check(f"CI: covers {os_name}", os_name in CI)
for action in ("actions/checkout@v6", "actions/setup-python@v6"):
    check(f"CI: uses {action}", action in CI)
for needle in (
    "python tests/test_contracts.py",
    "python tests/test_docs.py",
    "python -m build",
    "python -m twine check dist/*",
    "python tests/run_all.py k",
    "bash",
    "tmux",
):
    check(f"CI: includes {needle}", needle in CI)

# ── option order: flags before positional args ──
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL), ("k help", K_HELP)):
    # fire: -t must come before [session] <code>
    check(f"{doc_name}: fire option order", bool(re.search(r"k fire\s+\[-t", text)),
          "should be 'k fire [-t N] [session] <code>'")
    # history: -n must come before [session]
    check(f"{doc_name}: history option order", bool(re.search(r"k history\s+\[-n", text)),
          "should be 'k history [-n N] [session]'")

# ── removed/outdated patterns ──
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL)):
    check(f"{doc_name}: no old frame-only claim", "Frame delimiter = repeated prompt lines" not in text)
    # k JSON uses "status":"error","output":"interrupted" — NOT "status":"interrupted"
    # but km events legitimately emit "status":"interrupted" (terminal status)
    # so only check the k JSON schema section, not the km events section
    check(f"{doc_name}: k schema uses error+interrupted not status=interrupted",
          '"status": "error", "output": "interrupted"' in text)
    check(f"{doc_name}: no pipe-pane -o", "pipe-pane -o" not in text)
    check(f"{doc_name}: no /proc orphan docs", "/proc" not in text)
    check(f"{doc_name}: no bash-wrapped Python tests", "test_contracts.sh" not in text and "test_docs.sh" not in text)
    check(f"{doc_name}: hook uses path separator wording", "path separator" in text)
    check(f"{doc_name}: timeout recovery documented", "use k int or k kill" in text)
    check(f"{doc_name}: interrupted is error schema", '"status": "error", "output": "interrupted"' in text)

# ── km event monitor ──
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL)):
    check(f"{doc_name}: km CLI documented", "km <session>" in text)
    check(f"{doc_name}: km -1 flag documented", "-1" in text and "one-shot" in text.lower())
    for km_status in ("fired", "done", "timeout", "interrupted", "notify", "closed", "error"):
        check(f"{doc_name}: km event '{km_status}'", f'"status": "{km_status}"' in text)

# ── install + safety ──
check("README.md: install section", "## Install" in README)
check("README.md: requires python", "python" in README.lower() and "tmux" in README.lower())
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL)):
    check(f"{doc_name}: atomic result writes documented", "os.replace" in text)

# ── k help text consistency ──
for err in ("lock update failed", "interrupt failed"):
    check(f"k help: error '{err}'", err in K_HELP)

# ── k JSON error outputs ──
for doc_name, text in (("README.md", README), ("SKILL.md", SKILL)):
    for err in ("unknown cell", "watcher died", "active cell", "pipe failed", "send failed", "no active cell",
                 "lock update failed", "lock release failed", "interrupt failed"):
        check(f"{doc_name}: error output '{err}'", err in text)
    # cell errors (with cell_id) vs errors (without) are distinguished
    check(f"{doc_name}: cell error schema", "cell error" in text)
    without = re.search(r"Errors without `cell_id`: ([^\n]+)", text)
    with_cell = re.search(r"Errors with `cell_id`: ([^\n]+)", text)
    check(f"{doc_name}: errors without cell_id line exists", without is not None)
    check(f"{doc_name}: errors with cell_id line exists", with_cell is not None)
    if without and with_cell:
        check(f"{doc_name}: no-active-cell is without cell_id", "no active cell" in without.group(1))
        check(f"{doc_name}: no-active-cell not with cell_id", "no active cell" not in with_cell.group(1))
    check(f"{doc_name}: no duplicate default 5", "(default 5) (default 5)" not in text)

if FAILURES:
    print("documentation drift failures:")
    for failure in FAILURES:
        print(f"  - {failure}")
    raise SystemExit(1)

print("documentation drift tests passed")

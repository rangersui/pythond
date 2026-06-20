#!/usr/bin/env python3
"""Run all local test suites.

Pass a k executable/path to test an installed CLI, e.g. `python tests/run_all.py k`.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
K_ARG = sys.argv[1] if len(sys.argv) > 1 else "scripts/k"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    run([sys.executable, str(ROOT / "tests" / "test_contracts.py")])
    run([sys.executable, str(ROOT / "tests" / "test_bridge_contracts.py")])
    run([sys.executable, str(ROOT / "tests" / "test_docs.py")])
    run(["bash", str(ROOT / "tests" / "test.sh"), K_ARG])
    run([sys.executable, str(ROOT / "tests" / "test_regressions.py"), K_ARG])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

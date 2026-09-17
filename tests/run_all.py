"""Run every suite: ``python tests/run_all.py``.

Nothing here needs hardware. The vision suite replays synthetic video through
the detector, the relay suite stubs the serial transport, and the GUI suite runs
Qt on its offscreen platform.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUITES = (
    "test_vision.py",
    "test_relay_modbus.py",
    "test_flow_steps.py",
    "test_config_schema.py",
    "test_gui_smoke.py",
)


def main() -> int:
    failed: list[str] = []
    for suite in SUITES:
        print(f"\n=== {suite} ===")
        result = subprocess.run(
            [sys.executable, str(ROOT / "tests" / suite)],
            cwd=str(ROOT),
        )
        if result.returncode != 0:
            failed.append(suite)

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"all {len(SUITES)} suites passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the complete D1 + D2 validation workflow sequentially.

Run from this folder in Ubuntu:
    sudo python3 run_all.py
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
STAGES = [
    ("D1 three-player multiplayer demonstration", ["multiplayer_demo.py"]),
    ("D2 player-disconnection robustness", ["multiplayer_demo.py", "--robustness"]),
    ("D2 baseline performance", ["d2_performance_test.py", "--mode", "baseline"]),
    ("D2 static QoS performance", ["d2_performance_test.py", "--mode", "static"]),
    ("D2 dynamic QoS performance and recovery", ["d2_dynamic_person2_test.py"]),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-robustness", action="store_true",
        help="skip the simulated Player 2 outage/recovery test",
    )
    parser.add_argument(
        "--skip-d2", action="store_true",
        help="run only the D1 three-player demonstration",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Run this workflow in Ubuntu with sudo: sudo python3 run_all.py", file=sys.stderr)
        return 2

    stages = [STAGES[0]]
    if not args.skip_d2:
        if not args.skip_robustness:
            stages.append(STAGES[1])
        stages.extend(STAGES[2:])

    print("D1 + D2 MASTER WORKFLOW", flush=True)
    print("Each stage starts and cleans up its own Mininet network.", flush=True)
    for index, (label, command) in enumerate(stages, start=1):
        print(f"\n[{index}/{len(stages)}] {label}", flush=True)
        result = subprocess.run([sys.executable, *command], cwd=ROOT, check=False)
        if result.returncode != 0:
            print(
                f"\nSTOPPED: {label} failed with exit code {result.returncode}. "
                "Earlier stage results are preserved. Fix the reported issue, then rerun.",
                file=sys.stderr,
                flush=True,
            )
            return result.returncode or 1
    print("\nALL SELECTED STAGES PASSED. Review each generated summary and evidence folder.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

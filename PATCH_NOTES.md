# Repair notes

This package is based on the supplied `CN_PROJECT_zip.zip`. Original archived experiment outputs and the approved D2 controller source were preserved.

## Corrected

1. **Receiver dependency:** `launch_project.py` now resolves `receiver_new.py`, which is the receiver filename present in the archive.
2. **Dynamic thresholds:** `d2_dynamic_person2_test.py` explicitly supplies 50% congestion and 30% recovery thresholds, 2/3 confirmation samples, a 2-second monitor interval and a 6-second cooldown to the Ryu process. The chosen settings are saved in the run provenance and result summaries.
3. **Robustness verification:** the test now requires disconnection/reconnection evidence, pre-outage and post-recovery acknowledgements from Player 2, timeouts toward Player 2 during the outage, and continued Player 1/Player 3 communication during and after the outage. A failed check produces a non-zero exit status.
4. **Time-aligned CSV evidence:** `multiplayer_peer.py` records `elapsed_s` for each send result so the robustness test can evaluate the configured outage window.
5. **D1 controller consistency:** the protocol-aware learned-forwarding match is now in `game_sdn_controller.py` itself. The launcher copies this corrected source unchanged, so direct and launcher-based paths use the same logic.
6. **Prerequisites:** the common launcher checks that Mininet is importable; D2 performance scripts also check for `iperf3` before creating a network.
7. **Master workflow and documentation:** added `run_all.py`, `README.md`, `requirements-ryu.txt`, this file, a D2 report and local integrity tests.
8. **Evidence organization:** known failed/incomplete dynamic attempts are retained separately under `d2_performance_runs/failed_or_incomplete/`.

## Intentionally preserved

- `game_sdn_controller_person3_D2.py` was not modified. Its SHA-256 remains `4e67cd687b3a43954b2efa3b8392687d64da23718ef89081a54cf495e28e2db1`, matching the approved hash embedded in the dynamic test.
- Existing successful D1 and D2 evidence folders were not rewritten.
- Existing source files and historical measurements were retained unless a correction above required a source change.

## Validation performed during packaging

- Python `compileall` completed successfully.
- All five local project integrity tests passed.
- Approved D2 controller SHA-256 check passed.
- Live Mininet/OVS/Ryu execution was not possible in the packaging environment. Run `sudo python3 run_all.py` in the team's Ubuntu VM for final end-to-end validation.

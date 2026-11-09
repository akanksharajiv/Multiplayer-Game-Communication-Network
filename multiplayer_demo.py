#!/usr/bin/env python3
"""Unified three-player Mininet demo with optional D2 robustness testing.

Default mode preserves the D1 multiplayer demonstration.
Pass --robustness to run the D2 simulated Player 2 outage/recovery scenario.
Requires launch_project.py and multiplayer_peer.py in the project root.
"""

import argparse
import csv
import os
import statistics
import subprocess
import sys
from datetime import datetime

from launch_project import (
    ROOT,
    announce,
    check_prerequisites,
    prepare_integrated_controller,
    start_controller,
    create_network,
    configure_qos,
    clean_created_qos,
    stop_process,
    owner_files,
    command,
)

PEER = ROOT / "multiplayer_peer.py"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the three-player UDP multiplayer demonstration."
    )
    parser.add_argument(
        "--robustness",
        action="store_true",
        help="Enable D2 robustness test: simulate Player 2 going offline and returning.",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=15,
        help="Player runtime in seconds (default: 15).",
    )
    parser.add_argument(
        "--offline-start",
        type=float,
        default=5.0,
        help="Seconds after player start when Player 2 goes offline (default: 5).",
    )
    parser.add_argument(
        "--offline-duration",
        type=float,
        default=5.0,
        help="Duration of simulated Player 2 outage in seconds (default: 5).",
    )
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration must be greater than 0.")
    if args.offline_start < 0:
        parser.error("--offline-start cannot be negative.")
    if args.offline_duration <= 0:
        parser.error("--offline-duration must be greater than 0.")
    if args.robustness and args.offline_start + args.offline_duration >= args.duration:
        parser.error(
            "For --robustness, --offline-start + --offline-duration "
            "must be less than --duration so reconnection can be observed."
        )
    return args


def read_player_rows(folder, player):
    csv_path = folder / f"player{player}.csv"
    with csv_path.open(newline="") as file:
        return list(csv.DictReader(file))


def count_game_queue_flows(flow_output):
    """Count installed game-classification rules that explicitly select Queue 1."""
    return sum(
        "priority=200" in line and "set_queue:1" in line
        for line in flow_output.splitlines()
    )


def evaluate_robustness_records(rows, *, player, other_player, offline_start, offline_duration):
    """Evaluate outage/recovery evidence for one player's CSV records."""
    def elapsed(row):
        try:
            return float(row.get("elapsed_s", ""))
        except (TypeError, ValueError):
            return -1.0

    toward_p2 = [row for row in rows if row.get("target") == "2"]
    before = [row for row in toward_p2 if elapsed(row) < offline_start]
    during = [row for row in toward_p2 if offline_start <= elapsed(row) < offline_start + offline_duration]
    after = [row for row in toward_p2 if elapsed(row) >= offline_start + offline_duration]
    other_during = [
        row for row in rows
        if row.get("target") == str(other_player)
        and offline_start <= elapsed(row) < offline_start + offline_duration
    ]
    other_after = [
        row for row in rows
        if row.get("target") == str(other_player)
        and elapsed(row) >= offline_start + offline_duration
    ]
    return {
        "ack_before_outage": any(row.get("reply_received") == "1" for row in before),
        "timeout_during_outage": any(row.get("reply_received") == "0" for row in during),
        "ack_after_recovery": any(row.get("reply_received") == "1" for row in after),
        "other_peer_communicates_during_outage": any(row.get("reply_received") == "1" for row in other_during),
        "other_peer_communicates_after_outage": any(row.get("reply_received") == "1" for row in other_after),
    }


def main():
    args = parse_args()

    if os.geteuid() != 0:
        suffix = " [--robustness]" if not args.robustness else ""
        print(f"Run: sudo python3 multiplayer_demo.py{suffix}")
        return 1

    check_prerequisites()
    if not PEER.is_file():
        print(f"Missing {PEER.name} in the project folder: {ROOT}")
        return 1

    run_type = "robustness_runs" if args.robustness else "multiplayer_runs"
    folder = ROOT / run_type / datetime.now().strftime("%Y%m%d_%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)

    controller = None
    net = None
    qos = []
    processes = []
    logs = []

    try:
        announce("Starting Ryu SDN controller and 3-host Mininet network")
        controller_file = prepare_integrated_controller(folder)
        controller = start_controller(controller_file, folder)
        net = create_network()
        qos = configure_qos()

        announce("Starting three simultaneous UDP game players")
        for player in (1, 2, 3):
            logfile = (folder / f"player{player}.log").open("w")
            logs.append(logfile)
            host = net.get(f"h{player}")

            peer_args = [
                "python3",
                "-u",
                str(PEER),
                "--player",
                str(player),
                "--duration",
                str(args.duration),
                "--output",
                str(folder / f"player{player}.csv"),
            ]

            if args.robustness and player == 2:
                peer_args += [
                    "--offline-start",
                    str(args.offline_start),
                    "--offline-duration",
                    str(args.offline_duration),
                ]

            process = host.popen(
                peer_args,
                cwd=str(ROOT),
                stdout=logfile,
                stderr=subprocess.STDOUT,
            )
            processes.append(process)

        for process in processes:
            process.wait(timeout=args.duration + 16)
            if process.returncode != 0:
                raise RuntimeError(
                    f"Player process exited with code {process.returncode}; "
                    "inspect the player logs."
                )

        all_rtts = []
        total_sent = 0
        total_replies = 0
        report = [
            "THREE-PLAYER UDP MULTIPLAYER "
            + ("ROBUSTNESS TEST" if args.robustness else "DEMONSTRATION"),
            "Each player sends state updates to the other two players concurrently.",
            "",
        ]

        for player in (1, 2, 3):
            rows = read_player_rows(folder, player)
            rtts = [
                float(row["rtt_ms"])
                for row in rows
                if row.get("reply_received") == "1" and row.get("rtt_ms")
            ]

            sent = len(rows)
            replies = len(rtts)
            total_sent += sent
            total_replies += replies
            all_rtts.extend(rtts)

            if rtts:
                report.append(
                    f"Player {player}: updates sent={sent}, "
                    f"acknowledged={replies}, unanswered={sent - replies}, "
                    f"average RTT={statistics.mean(rtts):.3f} ms"
                )
            else:
                report.append(
                    f"Player {player}: updates sent={sent}, acknowledged=0"
                )

            if args.robustness:
                log_lines = (folder / f"player{player}.log").read_text().splitlines()
                timeout_count = sum("TIMEOUT waiting" in line for line in log_lines)
                gap_count = sum("SEQUENCE GAP" in line for line in log_lines)
                report.append(
                    f"  Player {player} timeout events: {timeout_count}, "
                    f"sequence gap events: {gap_count}"
                )

        flow = command(
            ["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"],
            check=False,
        )
        (folder / "flows.txt").write_text(flow.stdout + flow.stderr)
        game_flow_count = count_game_queue_flows(flow.stdout)

        unanswered_rate = (
            (total_sent - total_replies) * 100 / total_sent
            if total_sent
            else None
        )
        report += [
            "",
            f"Total updates sent: {total_sent}",
            f"Total replies: {total_replies}",
            (
                f"Unanswered update rate: {unanswered_rate:.1f}%"
                if unanswered_rate is not None
                else "Unanswered update rate: unavailable"
            ),
            (
                f"Overall average RTT: {statistics.mean(all_rtts):.3f} ms"
                if all_rtts
                else "Overall average RTT: unavailable"
            ),
            f"Game flows using Queue 1: {game_flow_count}",
            "Unanswered updates are not a definitive measure of one-way packet loss.",
        ]

        robustness_passed = True
        if args.robustness:
            player2_log = (folder / "player2.log").read_text(encoding="utf-8", errors="replace")
            disconnected = "DISCONNECTED (simulated)" in player2_log
            reconnected = "RECONNECTED" in player2_log
            peer_checks = {}
            for player in (1, 3):
                rows = read_player_rows(folder, player)
                other_player = 3 if player == 1 else 1
                checks = evaluate_robustness_records(
                    rows,
                    player=player,
                    other_player=other_player,
                    offline_start=args.offline_start,
                    offline_duration=args.offline_duration,
                )
                peer_checks[player] = tuple(checks.values())
                report.append(
                    f"Player {player} -> Player 2: acknowledged before outage={checks['ack_before_outage']}; "
                    f"timeout during outage={checks['timeout_during_outage']}; "
                    f"acknowledged after recovery={checks['ack_after_recovery']}"
                )
                report.append(
                    f"Player {player} -> Player {other_player}: communication during outage="
                    f"{checks['other_peer_communicates_during_outage']}; "
                    f"communication after outage={checks['other_peer_communicates_after_outage']}"
                )
            robustness_passed = disconnected and reconnected and all(all(v) for v in peer_checks.values())
            report += [
                "",
                (
                    f"Scenario: Player 2 simulates {args.offline_duration:g} seconds "
                    f"offline, starting at {args.offline_start:g} seconds."
                ),
                f"Player 2 disconnection logged: {disconnected}",
                f"Player 2 reconnection logged: {reconnected}",
                f"Robustness verification: {'PASS' if robustness_passed else 'FAIL'}",
            ]

        result = "\n".join(report) + "\n"
        (folder / "summary.txt").write_text(result, encoding="utf-8")
        print("\n" + result, flush=True)
        if game_flow_count == 0:
            raise RuntimeError(
                "No priority-200 game flows using Queue 1 were found. "
                "D1 QoS integration was not verified; inspect controller.log and flows.txt."
            )
        if args.robustness and not robustness_passed:
            raise RuntimeError("Robustness verification failed. See summary.txt and player CSV/log files.")
        return 0

    except Exception as error:
        print("ERROR:", error, flush=True)
        return 1

    finally:
        for process in processes:
            stop_process(process)
        for logfile in logs:
            logfile.close()
        if qos:
            clean_created_qos(qos)
        if net is not None:
            net.stop()
        stop_process(controller)
        owner_files(folder)
        print("Results saved to:", folder, flush=True)


if __name__ == "__main__":
    sys.exit(main())

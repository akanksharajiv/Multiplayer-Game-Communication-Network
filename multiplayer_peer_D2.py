#!/usr/bin/env python3
"""Unified three-player UDP game peer (D1 communication + D2 robustness features).

Default invocation preserves the normal D1 multiplayer behaviour. Optional
--offline-start/--offline-duration flags enable D2's simulated outage test, and
--ack-timeout controls how long an acknowledgement may remain outstanding.
"""

import argparse
import csv
import json
import socket
import threading
import time
from pathlib import Path

CSV_FIELDS = ["target", "sequence", "rtt_ms", "reply_received"]
PLAYER_IDS = (1, 2, 3)
UDP_PORT = 5000
SEND_INTERVAL_SECONDS = 0.5
RECEIVE_TIMEOUT_SECONDS = 0.2
STARTUP_GRACE_SECONDS = 1.0


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--player", type=int, required=True, choices=PLAYER_IDS)
    parser.add_argument("--duration", type=float, default=15,
                        help="duration of the update loop in seconds (default: 15)")
    parser.add_argument("--output", required=True, help="path for this player's CSV results")
    parser.add_argument("--offline-start", type=float, default=-1,
                        help="seconds after startup before simulated outage; disabled by default")
    parser.add_argument("--offline-duration", type=float, default=0,
                        help="length of simulated outage in seconds (default: 0)")
    parser.add_argument("--ack-timeout", type=float, default=1.5,
                        help="seconds before an unacknowledged update is recorded as timed out")
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration must be greater than 0")
    if args.ack_timeout <= 0:
        parser.error("--ack-timeout must be greater than 0")
    if args.offline_start < -1:
        parser.error("--offline-start must be -1 (disabled) or non-negative")
    if args.offline_duration < 0:
        parser.error("--offline-duration cannot be negative")
    if args.offline_start >= 0 and args.offline_duration <= 0:
        parser.error("--offline-duration must be greater than 0 when --offline-start is enabled")
    return args


def main():
    args = parse_args()
    addresses = {player: f"10.0.0.{player}" for player in PLAYER_IDS}
    peers = [player for player in PLAYER_IDS if player != args.player]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((addresses[args.player], UDP_PORT))
    except OSError:
        sock.close()
        raise
    sock.settimeout(RECEIVE_TIMEOUT_SECONDS)

    lock = threading.Lock()
    pending = {}  # (target_player, sequence) -> perf_counter send timestamp
    records = []
    last_sequence = {}
    stats = {"received_updates": 0, "missing_sequences": 0}
    running = threading.Event()
    running.set()
    started = time.monotonic()
    was_offline = False

    def is_offline():
        elapsed = time.monotonic() - started
        return (
            args.offline_start >= 0
            and args.offline_start <= elapsed < args.offline_start + args.offline_duration
        )

    def expire_pending():
        now = time.perf_counter()
        with lock:
            expired = [key for key, sent_at in pending.items()
                       if now - sent_at >= args.ack_timeout]
            for target, sequence in expired:
                pending.pop((target, sequence), None)
                records.append({
                    "target": target,
                    "sequence": sequence,
                    "rtt_ms": "",
                    "reply_received": 0,
                })
                print(
                    f"PLAYER {args.player}: TIMEOUT waiting for P{target} seq={sequence}",
                    flush=True,
                )

    def listen():
        while running.is_set():
            try:
                data, address = sock.recvfrom(4096)
                # During a simulated outage, this peer intentionally ignores all
                # inbound traffic, including updates and acknowledgements.
                if is_offline():
                    continue

                message = json.loads(data.decode("utf-8"))
                kind = message.get("type")
                sender = message.get("player")
                sequence = message.get("sequence")

                if sender not in peers or not isinstance(sequence, int):
                    continue

                if kind == "update":
                    with lock:
                        stats["received_updates"] += 1
                        previous = last_sequence.get(sender)
                        if previous is not None and sequence > previous + 1:
                            gap = sequence - previous - 1
                            stats["missing_sequences"] += gap
                            print(
                                f"PLAYER {args.player}: SEQUENCE GAP from P{sender}: "
                                f"{previous} -> {sequence}",
                                flush=True,
                            )
                        if previous is None or sequence > previous:
                            last_sequence[sender] = sequence

                    ack = {"type": "ack", "player": args.player, "sequence": sequence}
                    sock.sendto(json.dumps(ack).encode("utf-8"), address)
                    print(
                        f"Player {args.player} received P{sender} seq={sequence} "
                        f"x={message.get('x')} y={message.get('y')}",
                        flush=True,
                    )

                elif kind == "ack":
                    with lock:
                        sent_at = pending.pop((sender, sequence), None)
                        if sent_at is not None:
                            records.append({
                                "target": sender,
                                "sequence": sequence,
                                "rtt_ms": round((time.perf_counter() - sent_at) * 1000, 3),
                                "reply_received": 1,
                            })

            except socket.timeout:
                continue
            except (OSError, UnicodeError, ValueError, TypeError) as error:
                if not running.is_set():
                    break
                print(f"PLAYER {args.player}: receive error: {error}", flush=True)

    listener = threading.Thread(target=listen, daemon=True)
    listener.start()
    sequence = 0
    end = started + STARTUP_GRACE_SECONDS + args.duration

    try:
        # Keep the original one-second startup grace period used by both versions.
        time.sleep(STARTUP_GRACE_SECONDS)
        while time.monotonic() < end:
            offline_now = is_offline()
            if offline_now != was_offline:
                state = "DISCONNECTED (simulated)" if offline_now else "RECONNECTED"
                print(f"PLAYER {args.player}: {state}", flush=True)
                was_offline = offline_now

            if not offline_now:
                sequence += 1
                message = {
                    "type": "update",
                    "player": args.player,
                    "sequence": sequence,
                    "x": args.player * 10 + sequence,
                    "y": args.player * 5 + sequence,
                }
                payload = json.dumps(message).encode("utf-8")

                for target in peers:
                    key = (target, sequence)
                    with lock:
                        pending[key] = time.perf_counter()
                    try:
                        sock.sendto(payload, (addresses[target], UDP_PORT))
                    except OSError as error:
                        with lock:
                            pending.pop(key, None)
                            records.append({
                                "target": target,
                                "sequence": sequence,
                                "rtt_ms": "",
                                "reply_received": 0,
                            })
                        print(
                            f"PLAYER {args.player}: send error to P{target}: {error}",
                            flush=True,
                        )

            expire_pending()
            time.sleep(SEND_INTERVAL_SECONDS)

        # Allow final acknowledgements to arrive, then record any remaining timeouts.
        time.sleep(args.ack_timeout + 0.3)
        expire_pending()

    finally:
        running.clear()
        listener.join(timeout=1)
        sock.close()

        with lock:
            for target, sequence in list(pending):
                records.append({
                    "target": target,
                    "sequence": sequence,
                    "rtt_ms": "",
                    "reply_received": 0,
                })
            pending.clear()
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", newline="", encoding="utf-8") as csv_file:
                writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(sorted(records, key=lambda row: (row["sequence"], row["target"])))

        answered = sum(row["reply_received"] for row in records)
        print(
            f"PLAYER {args.player}: sent={len(records)}, replies={answered}, "
            f"unanswered={len(records) - answered}, "
            f"received_updates={stats['received_updates']}, "
            f"sequence_gaps_observed={stats['missing_sequences']}",
            flush=True,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

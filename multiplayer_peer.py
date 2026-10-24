import argparse
import csv
import json
import socket
import threading
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--player', type=int, required=True, choices=(1, 2, 3))
parser.add_argument('--duration', type=float, default=15)
parser.add_argument('--output', required=True)
args = parser.parse_args()

addresses = {i: f'10.0.0.{i}' for i in (1, 2, 3)}
peers = [i for i in addresses if i != args.player]
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((addresses[args.player], 5000))
sock.settimeout(0.2)
lock = threading.Lock()
pending = {}
records = []
last_sequence = {}
received_updates = 0
missing_sequences = 0
running = True

def listen():
    global received_updates, missing_sequences
    while running:
        try:
            data, address = sock.recvfrom(4096)
            message = json.loads(data.decode())
            kind = message.get('type')
            sender = message.get('player')
            seq = message.get('sequence')
            if kind == 'update' and sender in peers and isinstance(seq, int):
                with lock:
                    received_updates += 1
                    previous = last_sequence.get(sender)
                    if previous is not None and seq > previous + 1:
                        missing_sequences += seq - previous - 1
                    if previous is None or seq > previous:
                        last_sequence[sender] = seq
                ack = {'type': 'ack', 'player': args.player, 'sequence': seq}
                sock.sendto(json.dumps(ack).encode(), address)
                print(f'Player {args.player} received P{sender} seq={seq} x={message.get("x")} y={message.get("y")}', flush=True)
            elif kind == 'ack' and sender in peers and isinstance(seq, int):
                with lock:
                    key = (sender, seq)
                    start = pending.pop(key, None)
                    if start is not None:
                        records.append({'target': sender, 'sequence': seq, 'rtt_ms': round((time.perf_counter() - start)*1000, 3), 'reply_received': 1})
        except socket.timeout:
            pass
        except (OSError, UnicodeError, ValueError, TypeError):
            if not running:
                break

thread = threading.Thread(target=listen, daemon=True)
thread.start()
time.sleep(1.0)
sequence = 0
end = time.monotonic() + args.duration
while time.monotonic() < end:
    sequence += 1
    message = {'type': 'update', 'player': args.player, 'sequence': sequence,
               'x': args.player * 10 + sequence, 'y': args.player * 5 + sequence}
    payload = json.dumps(message).encode()
    for target in peers:
        with lock:
            pending[(target, sequence)] = time.perf_counter()
        try:
            sock.sendto(payload, (addresses[target], 5000))
        except OSError:
            with lock:
                pending.pop((target, sequence), None)
                records.append({'target': target, 'sequence': sequence, 'rtt_ms': '', 'reply_received': 0})
    time.sleep(0.5)
time.sleep(2.0)
running = False
thread.join(timeout=1)
sock.close()
with lock:
    for target, seq in pending:
        records.append({'target': target, 'sequence': seq, 'rtt_ms': '', 'reply_received': 0})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=['target','sequence','rtt_ms','reply_received'])
        writer.writeheader()
        writer.writerows(sorted(records, key=lambda r: (r['sequence'], r['target'])))
    answered = sum(r['reply_received'] for r in records)
    print(f'PLAYER {args.player}: sent={len(records)}, replies={answered}, unanswered={len(records)-answered}, received_updates={received_updates}, sequence_gaps_observed={missing_sequences}', flush=True)

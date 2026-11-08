#!/usr/bin/env python3
"""D2 reproducible baseline/static QoS benchmark for the existing CN project.

Run on the Ubuntu VM from ~/multiplayer-sdn-game:
  sudo python3 d2_performance_test.py --mode baseline
  sudo python3 d2_performance_test.py --mode static

The original topology, controller, launch_project.py, D1 measurements and
multiplayer peer are never modified. A separate run-local controller is used.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import tempfile
import time

from launch_project import (
    ROOT, check_prerequisites, prepare_integrated_controller, start_controller,
    create_network, configure_qos, clean_created_qos, stop_process, command,
)

PEER = ROOT / 'multiplayer_peer_D2.py'
BASE = ROOT / 'd2_performance_runs'
BOTTLENECK = 's1-eth2'
IPERF_SECONDS = 30
GAME_SECONDS = 17
OFFERED_MBIT_PER_STREAM = 15


def log(message):
    print(message, flush=True)


def launch(host, args, path, processes):
    out = path.open('w', encoding='utf-8')
    try:
        process = host.popen(args, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT)
    finally:
        out.close()
    processes.append(process)
    return process


def wait_ok(proc, label, timeout):
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{label} timed out; inspect the saved logs') from None
    if rc != 0:
        raise RuntimeError(f'{label} exited with code {rc}; inspect the saved logs')


def port_bytes():
    return int(Path(f'/sys/class/net/{BOTTLENECK}/statistics/tx_bytes').read_text())


def average(values):
    return round(statistics.mean(values), 3) if values else None


def summarize_games(folder):
    all_rtts = []
    to_h2_rtts = []
    changes = []
    sent = acknowledged = 0
    per_player = []
    for player in (1, 2, 3):
        with (folder / f'player{player}.csv').open(newline='', encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        valid = [r for r in rows if r['reply_received'] == '1' and r['rtt_ms']]
        rtts = [float(r['rtt_ms']) for r in valid]
        sent += len(rows)
        acknowledged += len(valid)
        all_rtts.extend(rtts)
        # The only saturated egress is toward h2, so report RTT for game
        # messages h1->h2 and h3->h2 separately from the aggregate RTT.
        if player in (1, 3):
            to_h2_rtts.extend(float(r['rtt_ms']) for r in valid if int(r['target']) == 2)
        for target in (1, 2, 3):
            seq_rtt = sorted(
                ((int(r['sequence']), float(r['rtt_ms'])) for r in valid if int(r['target']) == target),
                key=lambda p: p[0],
            )
            changes.extend(abs(b[1] - a[1]) for a, b in zip(seq_rtt, seq_rtt[1:]))
        per_player.append({
            'player': player,
            'sent': len(rows),
            'acknowledged': len(valid),
            'mean_rtt_ms': average(rtts),
        })
    return {
        'game_updates_sent': sent,
        'game_updates_acknowledged': acknowledged,
        'unanswered_update_pct': round((sent - acknowledged) * 100 / sent, 2) if sent else None,
        'mean_rtt_ms': average(all_rtts),
        'mean_rtt_to_h2_ms': average(to_h2_rtts),
        'mean_absolute_consecutive_rtt_change_ms': average(changes),
        'per_player': per_player,
    }


def parse_iperf_client(path):
    """Report throughput from the receiver's last summary line if present."""
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
    summaries = [s.strip() for s in lines if re.search(r'\s(receiver|sender)\s*$', s)]
    receiver = [s for s in summaries if s.endswith('receiver')]
    return {'final_receiver_summary': receiver[-1] if receiver else None,
            'final_sender_summary': next((s for s in reversed(summaries) if s.endswith('sender')), None)}


def make_readable_files(folder, mode, data):
    (folder / 'summary.json').write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    keys = [
        'mode', 'game_updates_sent', 'game_updates_acknowledged', 'unanswered_update_pct',
        'mean_rtt_ms', 'mean_rtt_to_h2_ms', 'mean_absolute_consecutive_rtt_change_ms',
        'shared_egress_mbps', 'game_flows_queue1',
    ]
    with (folder / 'summary.csv').open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerow({k: data.get(k) for k in keys})
    lines = [
        f'D2 {mode.upper()} PERFORMANCE TEST',
        'h1 and h3 each request 15 Mbps UDP to h2 (iperf3).',
        'All three game peers exchange UDP updates simultaneously.',
        'Same 10 Mbps HTB shaper and QoS queues on switch output s1-eth2.',
        'Baseline: game uses default Queue 0. Static: game directed to Queue 1.',
        '',
        f'Game updates: {data["game_updates_acknowledged"]}/{data["game_updates_sent"]} acknowledged',
        f'Unanswered updates: {data["unanswered_update_pct"]}%',
        f'Overall mean RTT: {data["mean_rtt_ms"]} ms',
        f'Mean RTT for traffic toward h2: {data["mean_rtt_to_h2_ms"]} ms',
        f'Average |successive RTT difference| (per player/target): '
        f'{data["mean_absolute_consecutive_rtt_change_ms"]} ms',
        f'Measured s1-eth2 throughput (8-second sample): {data["shared_egress_mbps"]} Mbps',
        f'Game rules using Queue 1: {data["game_flows_queue1"]}',
        'Unanswered update percentage is not equivalent to one-way packet loss.',
        '',
        'iperf3 h1 -> h2: ' + str(data['iperf_h1']['final_receiver_summary']),
        'iperf3 h3 -> h2: ' + str(data['iperf_h3']['final_receiver_summary']),
        '',
        f'Results folder: {folder}',
    ]
    summary = '\n'.join(lines) + '\n'
    (folder / 'summary.txt').write_text(summary, encoding='utf-8')
    log('\n' + summary)


def give_user_ownership(folder):
    uid = int(os.environ.get('SUDO_UID', str(os.getuid())))
    gid = int(os.environ.get('SUDO_GID', str(os.getgid())))
    for p in (BASE, folder, *folder.rglob('*')):
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('baseline', 'static'), required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run in Ubuntu with sudo')
    if not PEER.is_file():
        parser.error(f'missing {PEER}')
    check_prerequisites()  # Includes check that no s1 bridge or controller is running
    BASE.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=f'{args.mode}_', dir=BASE))
    # launch_project.start_controller drops privileges to the Ubuntu user;
    # it must be able to read its run-local controller from this folder.
    folder.chmod(0o755)
    processes = []
    ryu = net = None
    qos = []
    try:
        controller_path = prepare_integrated_controller(folder)
        if args.mode == 'baseline':
            original = controller_path.read_text(encoding='utf-8')
            needle = 'parser.OFPActionSetQueue(1),'
            if original.count(needle) != 1:
                raise RuntimeError('Expected exactly one Queue 1 action; no change applied')
            controller_path.write_text(original.replace(needle, '# baseline: no Queue 1 action', 1), encoding='utf-8')
        log(f'>>> Starting {args.mode.upper()} test (original controller untouched)')
        ryu = start_controller(controller_path, folder)
        net = create_network()
        qos = configure_qos()
        # Start separate iperf3 listeners in the h2 network namespace.
        servers = []
        for port in (5204, 5205):
            server = launch(net.get('h2'), ['iperf3', '-s', '-1', '-p', str(port)],
                            folder / f'server_{port}.log', processes)
            servers.append(server)
        time.sleep(0.7)
        if any(p.poll() is not None for p in servers):
            raise RuntimeError('An iperf3 server failed to start. See server logs.')
        clients = []
        for host, port in (('h1', 5204), ('h3', 5205)):
            proc = launch(net.get(host), ['iperf3', '-c', '10.0.0.2', '-p', str(port), '-u',
                                          '-b', f'{OFFERED_MBIT_PER_STREAM}M', '-t', str(IPERF_SECONDS)],
                          folder / f'{host}_iperf.log', processes)
            clients.append((host, proc))
        log('>>> Both background UDP flows started toward h2 (15 Mbps requested each)')
        time.sleep(1.0)
        players = []
        for player in (1, 2, 3):
            proc = launch(net.get(f'h{player}'), ['python3', '-u', str(PEER),
                      '--player', str(player), '--duration', str(GAME_SECONDS),
                      '--output', str(folder / f'player{player}.csv')],
                      folder / f'player{player}.log', processes)
            players.append((player, proc))
        log('>>> All three game peers started during background congestion')
        time.sleep(2.0)
        before = port_bytes()
        start = time.monotonic()
        time.sleep(8.0)
        elapsed = time.monotonic() - start
        after = port_bytes()
        shared_egress = round((after - before) * 8 / elapsed / 1000000, 3)
        log(f'>>> Measured shared s1-eth2 egress: {shared_egress} Mbps')
        for player, process in players:
            wait_ok(process, f'Player {player}', GAME_SECONDS + 15)
        for host, process in clients:
            wait_ok(process, f'Background {host}', IPERF_SECONDS + 20)
        for i, process in enumerate(servers):
            wait_ok(process, f'iperf3 server {i + 1}', 10)
        stats = command(['tc', '-s', 'class', 'show', 'dev', BOTTLENECK], check=False)
        (folder / 'tc_classes.txt').write_text(stats.stdout + stats.stderr, encoding='utf-8')
        qdisc = command(['tc', '-s', 'qdisc', 'show', 'dev', BOTTLENECK], check=False)
        (folder / 'tc_qdisc.txt').write_text(qdisc.stdout + qdisc.stderr, encoding='utf-8')
        flows = command(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', 's1'], check=False)
        (folder / 'flows.txt').write_text(flows.stdout + flows.stderr, encoding='utf-8')
        if flows.returncode != 0:
            raise RuntimeError('Could not dump OpenFlow rules; inspect flows.txt')
        queue1_flows = sum('priority=200' in line and 'set_queue:1' in line
                           for line in flows.stdout.splitlines())
        if args.mode == 'baseline' and queue1_flows != 0:
            raise RuntimeError('Baseline unexpectedly installed Queue 1 flows')
        if args.mode == 'static' and queue1_flows == 0:
            raise RuntimeError('Static test had no Queue 1 game flow')
        data = summarize_games(folder)
        data.update({
            'mode': args.mode,
            'shared_egress_mbps': shared_egress,
            'game_flows_queue1': queue1_flows,
            'iperf_h1': parse_iperf_client(folder / 'h1_iperf.log'),
            'iperf_h3': parse_iperf_client(folder / 'h3_iperf.log'),
        })
        make_readable_files(folder, args.mode, data)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        log(f'ERROR: {exc}')
        log(f'Inspect files in: {folder}')
        return 1
    finally:
        for proc in processes:
            stop_process(proc)
        if qos:
            clean_created_qos(qos)
        if net is not None:
            net.stop()
        stop_process(ryu)
        give_user_ownership(folder)
        log(f'Logs preserved at: {folder}')


if __name__ == '__main__':
    sys.exit(main())

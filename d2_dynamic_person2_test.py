#!/usr/bin/env python3
"""Person 2: test Person 3's APPROVED integrated D2 controller directly.

Preconditions (Ubuntu, ~/multiplayer-sdn-game):
  - game_sdn_controller_person3_D2.py (APPROVED corrected D2 source)
  - d2_performance_test.py (existing baseline/static benchmark)
  - launch_project.py, multiplayer_peer_D2.py and existing D1 files

Run:
  sudo python3 d2_dynamic_person2_test.py

This uses the same 3 players, 10-Mbps shaped egress, 2 x 15-Mbps iperf3 UDP
background streams, and 17-second game duration as baseline/static.
There are NO source patches and NO second D2 controller generated. The
approved D2 controller is launched directly; existing D1 files stay intact.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from launch_project import (
    ROOT, check_prerequisites, start_controller, create_network,
    configure_qos, clean_created_qos, stop_process, command,
)
from d2_performance_test import (
    BASE, PEER, IPERF_SECONDS, GAME_SECONDS, BOTTLENECK,
    OFFERED_MBIT_PER_STREAM, launch, wait_ok, port_bytes,
    summarize_games, parse_iperf_client, give_user_ownership,
)

# Person 3 has approved the integration-corrected D2 controller. It is now
# the ONLY D2 controller source needed for this benchmark. The D1 controller
# (game_sdn_controller.py) is still required by launch_project.py prerequisites.
APPROVED_CONTROLLER = ROOT / 'game_sdn_controller_person3_D2.py'
APPROVED_CONTROLLER_SHA256 = '4e67cd687b3a43954b2efa3b8392687d64da23718ef89081a54cf495e28e2db1'


def log(msg: str) -> None:
    print(msg, flush=True)


def verify_approved_controller() -> str:
    """Fail closed if an obsolete or unreviewed controller is substituted."""
    if not APPROVED_CONTROLLER.is_file():
        raise RuntimeError('Approved D2 controller missing: ' + str(APPROVED_CONTROLLER))
    data = APPROVED_CONTROLLER.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != APPROVED_CONTROLLER_SHA256:
        raise RuntimeError(
            'D2 controller does not match Person 3\'s approved, integration-corrected '
            'version. Do not run an old/unreviewed controller.\n'
            f'Expected SHA-256: {APPROVED_CONTROLLER_SHA256}\n'
            f'Found SHA-256:    {digest}\n'
            f'File: {APPROVED_CONTROLLER}'
        )
    compile(data.decode('utf-8'), str(APPROVED_CONTROLLER), 'exec')
    return digest


def record_controller_provenance(folder: Path, digest: str) -> None:
    """Retain exact build evidence without generating a second controller file."""
    (folder / 'integration_changes.txt').write_text(
        'Controller launched directly: game_sdn_controller_person3_D2.py\n'
        'Approved integrated code: yes (Person 3 approved)\n'
        'Protocol-aware learned forwarding matches: already integrated\n'
        'Destination-aware dynamic flows: already integrated\n'
        'Run-local code patches: none\n'
        'Run-local controller copies: none\n'
        f'Controller SHA-256: {digest}\n',
        encoding='utf-8',
    )


def dump_flows(folder: Path, name: str) -> str:
    result = command(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', 's1'], check=False)
    (folder / name).write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode != 0:
        raise RuntimeError(f'dump-flows failed ({name}); inspect saved output')
    return result.stdout


def count_dynamic_rules(contents: str, priority: int) -> int:
    return sum(('priority=' + str(priority)) in line and 'set_queue:' in line
               for line in contents.splitlines())


def save_results(folder: Path, data: dict) -> None:
    (folder / 'summary.json').write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')
    import csv
    with (folder / 'summary.csv').open('w', newline='', encoding='utf-8') as handle:
        columns = [
            'mode', 'game_updates_sent', 'game_updates_acknowledged',
            'unanswered_update_pct', 'mean_rtt_ms', 'mean_rtt_to_h2_ms',
            'mean_absolute_consecutive_rtt_change_ms', 'shared_egress_mbps',
            'game_flows_queue1', 'active_dynamic_game_flows',
            'active_dynamic_background_flows', 'policy_activation_logged',
            'policy_recovery_logged', 'dynamic_flows_removed_after_recovery',
        ]
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow({k: data.get(k) for k in columns})
    report = '\n'.join([
        'D2 DYNAMIC PERFORMANCE TEST (Person 3-approved integrated controller)',
        'Same 3 UDP peers, 2 x 15 Mbps background traffic, 10 Mbps bottleneck as baseline/static.',
        f'Game updates: {data["game_updates_acknowledged"]}/{data["game_updates_sent"]} acknowledged',
        f'Unanswered update rate: {data["unanswered_update_pct"]}%',
        f'Overall mean RTT: {data["mean_rtt_ms"]} ms',
        f'Mean RTT for traffic toward h2: {data["mean_rtt_to_h2_ms"]} ms',
        'Average |successive RTT difference|: '
        f'{data["mean_absolute_consecutive_rtt_change_ms"]} ms',
        f'Measured shared egress (8-second sample): {data["shared_egress_mbps"]} Mbps',
        f'During congestion: priority-250 game flows={data["active_dynamic_game_flows"]}',
        f'During congestion: priority-100 background flows={data["active_dynamic_background_flows"]}',
        f'Controller logged policy activation: {data["policy_activation_logged"]}',
        f'Controller logged policy recovery: {data["policy_recovery_logged"]}',
        f'Temporary policy flows removed on recovery: {data["dynamic_flows_removed_after_recovery"]}',
        'Unanswered updates are NOT a direct measure of one-way packet loss.',
        'iperf h1->h2: ' + str(data['iperf_h1']['final_receiver_summary']),
        'iperf h3->h2: ' + str(data['iperf_h3']['final_receiver_summary']),
        'See integration_changes.txt for approved controller identity and SHA-256.',
        f'Results folder: {folder}',
    ]) + '\n'
    (folder / 'summary.txt').write_text(report, encoding='utf-8')
    log('\n' + report)


def main() -> int:
    if os.geteuid() != 0:
        log('ERROR: Run in Ubuntu with sudo, not in the Mac terminal.')
        return 1
    if not PEER.is_file():
        log('ERROR: Missing peer application: ' + str(PEER))
        return 1
    try:
        controller_digest = verify_approved_controller()
        check_prerequisites()
    except (OSError, RuntimeError, UnicodeError, subprocess.CalledProcessError) as error:
        log('ERROR: ' + str(error))
        return 1
    BASE.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='dynamic_', dir=BASE))
    folder.chmod(0o755)
    processes = []
    network = ryu = None
    qos = []
    try:
        record_controller_provenance(folder, controller_digest)
        log('>>> Running DYNAMIC QoS test with Person 3-approved D2 controller')
        log('>>> Using ' + str(APPROVED_CONTROLLER) + ' directly (no code patches/copies)')
        ryu = start_controller(APPROVED_CONTROLLER, folder)
        network = create_network()
        qos = configure_qos()
        for port in (5204, 5205):
            launch(network.get('h2'), ['iperf3', '-s', '-1', '-p', str(port)],
                   folder / f'server_{port}.log', processes)
        time.sleep(0.7)
        if any(p.poll() is not None for p in processes):
            raise RuntimeError('iperf3 server startup failed; inspect logs')
        clients = []
        for host, port in (('h1', 5204), ('h3', 5205)):
            proc = launch(network.get(host),
                          ['iperf3', '-c', '10.0.0.2', '-p', str(port), '-u',
                           '-b', f'{OFFERED_MBIT_PER_STREAM}M', '-t', str(IPERF_SECONDS)],
                          folder / f'{host}_iperf.log', processes)
            clients.append((host, proc))
        log('>>> 2 x 15 Mbps UDP background streams running toward h2')
        time.sleep(1.0)
        players = []
        for player in (1, 2, 3):
            proc = launch(network.get(f'h{player}'),
                          ['python3', '-u', str(PEER), '--player', str(player),
                           '--duration', str(GAME_SECONDS),
                           '--output', str(folder / f'player{player}.csv')],
                          folder / f'player{player}.log', processes)
            players.append((player, proc))
        log('>>> All 3 players exchanging game updates under congestion')
        time.sleep(2.0)
        before = port_bytes()
        start = time.monotonic()
        time.sleep(8.0)
        shared = round((port_bytes() - before) * 8 / (time.monotonic() - start) / 1000000, 3)
        log(f'>>> Measured shared s1-eth2 egress: {shared} Mbps')
        active_flows = dump_flows(folder, 'flows_during_congestion.txt')
        game_flows = count_dynamic_rules(active_flows, 250)
        ordinary_flows = count_dynamic_rules(active_flows, 100)
        log(f'>>> Active adaptive rules: {game_flows} game, {ordinary_flows} background')
        for player, proc in players:
            wait_ok(proc, f'Player {player}', GAME_SECONDS + 15)
        for host, proc in clients:
            wait_ok(proc, f'Background sender {host}', IPERF_SECONDS + 20)
        for idx, proc in enumerate(processes[:2], start=1):
            wait_ok(proc, f'iperf3 server {idx}', 10)
        # One 2-second monitor interval per recovery sample, plus margin.
        log('>>> Congestion stopped; verifying automatic policy recovery')
        time.sleep(9.0)
        recovered_flows = dump_flows(folder, 'flows_after_recovery.txt')
        tc = command(['tc', '-s', 'class', 'show', 'dev', BOTTLENECK], check=False)
        (folder / 'tc_classes.txt').write_text(tc.stdout + tc.stderr, encoding='utf-8')
        qd = command(['tc', '-s', 'qdisc', 'show', 'dev', BOTTLENECK], check=False)
        (folder / 'tc_qdisc.txt').write_text(qd.stdout + qd.stderr, encoding='utf-8')
        controller_log = (folder / 'controller.log').read_text(encoding='utf-8', errors='replace')
        policy_lines = [line for line in controller_log.splitlines() if any(
            label in line for label in (
                'CONGESTION CANDIDATE', 'POLICY CHANGE: CONGESTION ACTIVE',
                'RECOVERY CANDIDATE', 'POLICY RECOVERY: NORMAL POLICY RESTORED',
            )
        )]
        (folder / 'policy_events.txt').write_text('\n'.join(policy_lines) + '\n', encoding='utf-8')
        activated = 'POLICY CHANGE: CONGESTION ACTIVE' in controller_log
        recovered = 'POLICY RECOVERY: NORMAL POLICY RESTORED' in controller_log
        removed = count_dynamic_rules(recovered_flows, 250) == 0 and count_dynamic_rules(recovered_flows, 100) == 0
        stats = summarize_games(folder)
        stats.update({
            'mode': 'dynamic',
            'shared_egress_mbps': shared,
            'game_flows_queue1': game_flows,
            'active_dynamic_game_flows': game_flows,
            'active_dynamic_background_flows': ordinary_flows,
            'policy_activation_logged': activated,
            'policy_recovery_logged': recovered,
            'dynamic_flows_removed_after_recovery': removed,
            'iperf_h1': parse_iperf_client(folder / 'h1_iperf.log'),
            'iperf_h3': parse_iperf_client(folder / 'h3_iperf.log'),
        })
        save_results(folder, stats)
        if not (activated and recovered and removed and game_flows and ordinary_flows):
            raise RuntimeError('Adaptive verification incomplete. Inspect controller.log and saved flow tables.')
        log('>>> PASS: Dynamic congestion detection, destination-aware policy and recovery verified')
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        log('ERROR: ' + str(error))
        log('Inspect saved logs in ' + str(folder))
        return 1
    finally:
        for proc in processes:
            stop_process(proc)
        if qos:
            clean_created_qos(qos)
        if network is not None:
            network.stop()
        stop_process(ryu)
        give_user_ownership(folder)
        log(f'Logs preserved in: {folder}')


if __name__ == '__main__':
    sys.exit(main())

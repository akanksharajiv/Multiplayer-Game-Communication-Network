#!/usr/bin/env python3
"""Three simultaneous UDP players on a Mininet network with Ryu QoS."""
import csv
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time
from datetime import datetime

from launch_project import (ROOT, announce, check_prerequisites,
                            prepare_integrated_controller, start_controller,
                            create_network, configure_qos, clean_created_qos,
                            stop_process, owner_files, command)

PEER = ROOT / 'multiplayer_peer.py'
DURATION = 15

def main():
    if os.geteuid() != 0:
        print('Run: sudo python3 multiplayer_demo.py')
        return 1
    check_prerequisites()
    if not PEER.is_file():
        print('Missing multiplayer_peer.py in the project folder')
        return 1
    folder = ROOT / 'multiplayer_runs' / datetime.now().strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True, exist_ok=True)
    controller = None
    net = None
    qos = []
    processes = []
    logs = []
    try:
        announce('Starting Ryu SDN controller and 3-host Mininet network')
        controller_file = prepare_integrated_controller(folder)
        controller = start_controller(controller_file, folder)
        net = create_network()
        qos = configure_qos()
        announce('Starting three SIMULTANEOUS UDP game players')
        for i in (1, 2, 3):
            logfile = (folder / f'player{i}.log').open('w')
            logs.append(logfile)
            host = net.get(f'h{i}')
            process = host.popen(['python3', '-u', str(PEER), '--player', str(i),
                                  '--duration', str(DURATION), '--output', str(folder / f'player{i}.csv')],
                                 cwd=str(ROOT), stdout=logfile, stderr=subprocess.STDOUT)
            processes.append(process)
        for process in processes:
            process.wait(timeout=DURATION + 12)
            if process.returncode != 0:
                raise RuntimeError(f'Player process exited with code {process.returncode}; inspect player logs')
        all_rtts = []
        total_sent = total_replies = 0
        report = ['THREE-PLAYER UDP MULTIPLAYER DEMONSTRATION', 'Each player sends to the other two players concurrently.', '']
        for i in (1, 2, 3):
            with (folder / f'player{i}.csv').open(newline='') as file:
                rows = list(csv.DictReader(file))
            rtts = [float(r['rtt_ms']) for r in rows if r['reply_received'] == '1']
            total_sent += len(rows)
            total_replies += len(rtts)
            all_rtts.extend(rtts)
            report.append(f'Player {i}: updates sent={len(rows)}, acknowledged={len(rtts)}, average RTT={statistics.mean(rtts):.3f} ms' if rtts else f'Player {i}: updates sent={len(rows)}, acknowledged=0')
        flow = command(['ovs-ofctl', '-O', 'OpenFlow13', 'dump-flows', 's1'], check=False)
        (folder / 'flows.txt').write_text(flow.stdout + flow.stderr)
        game_flows = [line for line in flow.stdout.splitlines() if 'priority=200' in line and 'set_queue:1' in line]
        report.extend(['', f'Total updates sent: {total_sent}', f'Total replies: {total_replies}',
                       f'Unanswered update rate: {(total_sent-total_replies)*100/total_sent:.1f}%' if total_sent else 'No updates sent',
                       f'Overall average RTT: {statistics.mean(all_rtts):.3f} ms' if all_rtts else 'Overall average RTT: unavailable',
                       f'Game flows using Queue 1: {len(game_flows)}',
                       'Unanswered updates are not a definitive measure of one-way packet loss.'])
        result = '\n'.join(report) + '\n'
        (folder / 'summary.txt').write_text(result)
        print('\n' + result, flush=True)
        return 0
    except Exception as error:
        print('ERROR:', error, flush=True)
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
        print('Results saved to:', folder, flush=True)

if __name__ == '__main__':
    sys.exit(main())

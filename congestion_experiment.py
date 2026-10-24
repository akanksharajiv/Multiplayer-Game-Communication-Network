#!/usr/bin/env python3
"""Two-phase Mininet SDN congestion experiment using existing launch_project.py."""
import csv
from datetime import datetime
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time

import launch_project as base

ROOT = Path(__file__).resolve().parent
DURATION = 15

BACKGROUND = '''import socket,time,sys
mode=sys.argv[1]
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
if mode=='receive':
 s.bind(('0.0.0.0',6000))
 while True: s.recvfrom(65535)
else:
 payload=b'B'*1200
 next_send=time.monotonic()
 while True:
  s.sendto(payload,('10.0.0.2',6000))
  next_send+=0.0004
  delay=next_send-time.monotonic()
  if delay>0: time.sleep(delay)
  elif delay < -0.5: next_send=time.monotonic()
'''


def stop(process):
    base.stop_process(process)


def run_phase(folder, controller_file, label, enable_qos):
    phase = folder / label
    phase.mkdir()
    net = None
    controller = None
    queues = []
    processes = []
    handles = []
    try:
        print(f'\n=== {label.upper()} ===', flush=True)
        controller = base.start_controller(controller_file, phase)
        net = base.create_network()
        # Keep the same 10 Mbps switch cap in both scenarios.
        queues = base.configure_qos()
        h1, h2, h3 = (net.get(x) for x in ('h1','h2','h3'))
        for filename, host, command in (
            ('receiver.log',h2,['python3','-u',str(base.RECEIVER_FILE)]),
            ('background_receiver.log',h2,['python3','-u',str(folder/'background.py'),'receive']),
        ):
            handle=(phase/filename).open('w')
            handles.append(handle)
            processes.append(host.popen(command,stdout=handle,stderr=subprocess.STDOUT))
        time.sleep(1)
        if any(p.poll() is not None for p in processes):
            raise RuntimeError(f'{label}: receiver process exited; inspect logs')
        # Warm up ARP/learning before congestion, for a fair comparison.
        h1.cmd('ping -c 1 -W 1 10.0.0.2 >/dev/null 2>&1')
        h3.cmd('ping -c 1 -W 1 10.0.0.2 >/dev/null 2>&1')
        handle=(phase/'background_sender.log').open('w')
        handles.append(handle)
        bg=h3.popen(['python3','-u',str(folder/'background.py'),'send'],stdout=handle,stderr=subprocess.STDOUT)
        processes.append(bg)
        time.sleep(0.5)
        if bg.poll() is not None:
            raise RuntimeError(f'{label}: background sender exited')
        handle=(phase/'sender.log').open('w')
        handles.append(handle)
        sender=h1.popen(['timeout','--signal=INT','--kill-after=3s',f'{DURATION}s','python3','-u',str(base.SENDER_FILE)],cwd=str(phase),stdout=handle,stderr=subprocess.STDOUT)
        processes.append(sender)
        sender.wait(timeout=DURATION+8)
        if not (phase/'measurements.csv').exists():
            raise RuntimeError(f'{label}: measurements.csv not produced; inspect sender.log')
        flow = base.command(['ovs-ofctl','-O','OpenFlow13','dump-flows','s1'],check=False)
        (phase/'flows.txt').write_text(flow.stdout+flow.stderr)
        queue = base.command(['ovs-ofctl','-O','OpenFlow13','queue-stats','s1'],check=False)
        (phase/'queue_stats.txt').write_text(queue.stdout+queue.stderr)
        game_flows=sum('priority=200' in x and 'set_queue:1' in x for x in flow.stdout.splitlines())
        with (phase/'measurements.csv').open(newline='') as f:
            rows=list(csv.DictReader(f))
        replies=[r for r in rows if r['reply_received']=='1']
        rtts=[float(r['rtt_ms']) for r in replies if r['rtt_ms']]
        variations=[float(r['rtt_variation_ms']) for r in rows if r['rtt_variation_ms']]
        result={
            'scenario':label,
            'sent':len(rows),
            'replies':len(replies),
            'unanswered_percent':round(100*(len(rows)-len(replies))/len(rows),2) if rows else None,
            'avg_rtt_ms':round(statistics.mean(rtts),3) if rtts else None,
            'avg_rtt_variation_ms':round(statistics.mean(variations),3) if variations else None,
            'game_queue_flows':game_flows,
        }
        print(result,flush=True)
        return result
    finally:
        for p in reversed(processes):
            stop(p)
        for handle in handles:
            handle.close()
        if queues:
            base.clean_created_qos(queues)
        if net is not None:
            net.stop()
        stop(controller)
        time.sleep(1)


def main():
    base.check_prerequisites()
    folder=ROOT/'experiment_runs'/datetime.now().strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True)
    (folder/'background.py').write_text(BACKGROUND)
    controller_file=base.prepare_integrated_controller(folder)
    baseline_controller=folder/'game_sdn_controller_baseline.py'
    baseline_controller.write_text(controller_file.read_text().replace('parser.OFPActionSetQueue(1)', 'parser.OFPActionSetQueue(0)'))
    try:
        from mininet.log import setLogLevel
        setLogLevel('warning')
        results=[]
        results.append(run_phase(folder,baseline_controller,'without_qos',False))
        results.append(run_phase(folder,controller_file,'with_qos',True))
        with (folder/'comparison.csv').open('w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=list(results[0]))
            writer.writeheader()
            writer.writerows(results)
        a,b=results
        report=[
            'SDN GAME TRAFFIC UNDER CONGESTION',
            'Background load: ~24 Mbps UDP offered from h3 to h2, port 6000',
            'Switch egress QoS cap in both scenarios: 10 Mbps',
            'Game UDP: h1 to h2, port 5000',
            '',
            f"Without QoS: RTT={a['avg_rtt_ms']} ms, variation={a['avg_rtt_variation_ms']} ms, unanswered={a['unanswered_percent']}%",
            f"With QoS:    RTT={b['avg_rtt_ms']} ms, variation={b['avg_rtt_variation_ms']} ms, unanswered={b['unanswered_percent']}%",
            f"Queue-1 game flows: {b['game_queue_flows']}",
            '',
            'Interpretation: compare measured values; improvement is not guaranteed.',
            'This experiment uses offered UDP load, not measured throughput.',
            'Baseline sends game and background traffic to Queue 0; prioritized sends game to Queue 1.',
            'Unanswered replies are a proxy, not proof of one-way packet loss.',
        ]
        (folder/'report.txt').write_text('\n'.join(report)+'\n')
        print('\n'+'\n'.join(report))
        print(f'\nSaved all results in: {folder}')
        return 0
    finally:
        base.owner_files(folder)


if __name__=='__main__':
    try:
        sys.exit(main())
    except (RuntimeError, subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired) as e:
        print('ERROR:',e,file=sys.stderr)
        sys.exit(1)

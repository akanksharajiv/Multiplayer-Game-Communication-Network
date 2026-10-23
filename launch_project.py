#!/usr/bin/env python3
"""Single-command SDN game demonstrator for an existing Mininet project folder.

Usage (in Ubuntu): sudo python3 launch_project.py
Requires: topology's original project files, system Mininet/OVS, and .venv-ryu.
Does not edit existing project source files or historical measurements.
"""

from __future__ import annotations

import csv
from datetime import datetime
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
CONTROLLER_FILE = ROOT / "game_sdn_controller.py"
SENDER_FILE = ROOT / "sender_mininet.py"
RECEIVER_FILE = ROOT / "receiver.py"
RYU = ROOT / ".venv-ryu" / "bin" / "ryu-manager"
CONTROLLER_ADDRESS = ("127.0.0.1", 6653)
GAME_SECONDS = 12
PORTS = ("s1-eth1", "s1-eth2", "s1-eth3")


def announce(message: str) -> None:
    print(f"\n>>> {message}", flush=True)


def command(args, *, check=True):
    return subprocess.run(args, text=True, capture_output=True, check=check)


def check_prerequisites() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Launch with sudo: sudo python3 launch_project.py")
    for f in (CONTROLLER_FILE, SENDER_FILE, RECEIVER_FILE, RYU):
        if not f.is_file():
            raise RuntimeError(f"Required file missing: {f}")
    for exe in ("ovs-vsctl", "ovs-ofctl", "timeout"):
        if shutil.which(exe) is None:
            raise RuntimeError(f"Missing system command: {exe}")
    bridge = command(["ovs-vsctl", "br-exists", "s1"], check=False)
    if bridge.returncode == 0:
        raise RuntimeError(
            "An existing Mininet network (s1) is running. Go to its mininet> prompt "
            "and type exit before using this launcher. Do not launch two topologies."
        )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        if sock.connect_ex(CONTROLLER_ADDRESS) == 0:
            raise RuntimeError(
                "Port 6653 already has a controller. Stop the existing Ryu terminal "
                "with Ctrl+C first; this launcher will start its own controller."
            )


def fresh_run_folder() -> Path:
    base = ROOT / "demo_runs" / datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = base
    suffix = 2
    while folder.exists():
        folder = Path(str(base) + f"_{suffix}")
        suffix += 1
    folder.mkdir(parents=True)
    return folder


def prepare_integrated_controller(folder: Path) -> Path:
    """Make a run-local copy correcting the broad priority-10 MAC catch-all.

    The teammate's original file is NEVER edited. Without this narrowing,
    ordinary IP forwarding flows may swallow later UDP game packets before
    the controller can classify them at priority 200.
    """
    original = CONTROLLER_FILE.read_text(encoding="utf-8")
    old = '''                match = parser.OFPMatch(
                    in_port=in_port,
                    eth_dst=eth.dst,
                )
                actions = [parser.OFPActionOutput(out_port)]'''
    new = '''                # Scope ordinary forwarding to its protocol. A broad
                # (in_port, eth_dst) rule can hide subsequent game UDP flows.
                fields = dict(
                    in_port=in_port,
                    eth_dst=eth.dst,
                    eth_type=eth.ethertype,
                )
                if ip_pkt is not None:
                    fields["ip_proto"] = ip_pkt.proto
                    if udp_pkt is not None:
                        fields["udp_src"] = udp_pkt.src_port
                        fields["udp_dst"] = udp_pkt.dst_port
                match = parser.OFPMatch(**fields)
                actions = [parser.OFPActionOutput(out_port)]'''
    if original.count(old) != 1:
        raise RuntimeError(
            "Your controller code differs from the reviewed version. No patch applied. "
            "Please inspect game_sdn_controller.py before using this launcher."
        )
    result = folder / "game_sdn_controller_integrated.py"
    result.write_text(original.replace(old, new, 1), encoding="utf-8")
    return result


def start_controller(controller_file: Path, folder: Path):
    log = (folder / "controller.log").open("w", encoding="utf-8")

    def drop_privileges():
        uid = int(os.environ.get("SUDO_UID", "0"))
        gid = int(os.environ.get("SUDO_GID", "0"))
        if uid != 0:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    # Controller uses a separate Python 3.11 venv, while Mininet uses the
    # distribution's Python 3.14. Both were already installed by the user.
    process = subprocess.Popen(
        [str(RYU), "--ofp-tcp-listen-port", "6653", str(controller_file)],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        preexec_fn=drop_privileges,
    )
    log.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            text = (folder / "controller.log").read_text(errors="replace")
            raise RuntimeError("Ryu exited early. Controller log:\n" + text[-2400:])
        try:
            with socket.create_connection(CONTROLLER_ADDRESS, timeout=0.4):
                return process
        except OSError:
            time.sleep(0.25)
    raise RuntimeError("Controller did not listen on port 6653 within 15 seconds. See controller.log")


def create_network():
    from mininet.net import Mininet
    from mininet.node import OVSSwitch, RemoteController

    net = Mininet(controller=None, switch=OVSSwitch, autoSetMacs=True, build=False)
    net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6653)
    switch = net.addSwitch("s1", protocols="OpenFlow13", failMode="secure")
    for index in (1, 2, 3):
        host = net.addHost(f"h{index}", ip=f"10.0.0.{index}/24")
        net.addLink(host, switch)
    try:
        net.build()
        net.start()
    except Exception:
        net.stop()
        raise
    return net


def configure_qos():
    created = []
    try:
        for port in PORTS:
            attached = command(["ovs-vsctl", "get", "Port", port, "qos"]).stdout.strip()
            if attached != "[]":
                raise RuntimeError(f"Port {port} already has QoS {attached}; not overwriting it")
            response = command([
                "ovs-vsctl", "--", "set", "Port", port, "qos=@qos",
                "--", "--id=@qos", "create", "QoS", "type=linux-htb",
                "other-config:max-rate=10000000", "queues:0=@q0", "queues:1=@q1",
                "--", "--id=@q0", "create", "Queue",
                "other-config:min-rate=2000000", "other-config:priority=5",
                "--", "--id=@q1", "create", "Queue",
                "other-config:min-rate=8000000", "other-config:priority=0",
            ])
            uuids = re.findall(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", response.stdout)
            created.append((port, uuids))
            print(f"    {port}: Queue 0 ordinary / Queue 1 game, 10 Mbps cap", flush=True)
    except Exception:
        clean_created_qos(created)
        raise
    return created


def clean_created_qos(created):
    """Detach only our own QoS records, then remove their OVSDB rows."""
    for port, uuids in reversed(created):
        command(["ovs-vsctl", "--if-exists", "clear", "Port", port, "qos"], check=False)
        if len(uuids) == 3:
            for table, uid in zip(("QoS", "Queue", "Queue"), uuids):
                command(["ovs-vsctl", "--if-exists", "destroy", table, uid], check=False)


def stop_process(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def collect_summary(folder: Path):
    file = folder / "measurements.csv"
    if not file.is_file():
        print("Game sender did not write measurements.csv. See sender.log.")
        return
    with file.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    successes = [r for r in rows if r["reply_received"] == "1"]
    rtts = [float(r["rtt_ms"]) for r in successes if r["rtt_ms"]]
    variations = [float(r["rtt_variation_ms"]) for r in rows if r["rtt_variation_ms"]]
    result = [
        "SDN UDP Game Demo Results",
        f"Updates: {len(rows)}",
        f"Replies: {len(successes)} / {len(rows)}",
        f"Average RTT: {statistics.mean(rtts):.3f} ms" if rtts else "Average RTT: unavailable",
        f"Average absolute RTT variation: {statistics.mean(variations):.3f} ms" if variations else "Average variation: unavailable",
    ]
    flow = command(["ovs-ofctl", "-O", "OpenFlow13", "dump-flows", "s1"], check=False)
    (folder / "flows.txt").write_text(flow.stdout + flow.stderr, encoding="utf-8")
    priority = [line for line in flow.stdout.splitlines() if "priority=200" in line and "set_queue:1" in line]
    result.append(f"Controller-installed game flows using Queue 1: {len(priority)}")
    queues = command(["ovs-ofctl", "-O", "OpenFlow13", "queue-stats", "s1"], check=False)
    (folder / "queue_stats.txt").write_text(queues.stdout + queues.stderr, encoding="utf-8")
    if not priority:
        result.append("WARNING: game flow not found; inspect controller.log and flows.txt")
    report = "\n".join(result) + "\n"
    (folder / "summary.txt").write_text(report, encoding="utf-8")
    print("\n" + report, flush=True)


def owner_files(folder: Path):
    """Return created logs to the user even though Mininet runs under sudo."""
    uid = int(os.environ.get("SUDO_UID", "0"))
    gid = int(os.environ.get("SUDO_GID", "0"))
    if uid == 0:
        return
    for p in [ROOT / "demo_runs", folder, *folder.rglob("*")]:
        try:
            os.chown(p, uid, gid)
        except OSError:
            pass


def main() -> int:
    net = None
    qos_created = []
    ryu = None
    receiver = None
    receiver_log = None
    folder = None
    try:
        check_prerequisites()
        folder = fresh_run_folder()
        announce("Preparing a run-local controller copy (your original source is untouched)")
        integrated_controller = prepare_integrated_controller(folder)
        announce("Starting Ryu controller automatically")
        ryu = start_controller(integrated_controller, folder)
        announce("Creating your 3-host OpenFlow 1.3 Mininet network")
        from mininet.log import setLogLevel
        from mininet.cli import CLI
        setLogLevel("info")
        net = create_network()
        announce("Configuring Queue 0 and Queue 1 on all 3 switch egress ports")
        qos_created = configure_qos()
        h1 = net.get("h1")
        h2 = net.get("h2")
        announce("Starting receiver on h2 and sending a short automatic game demo from h1")
        receiver_log = (folder / "receiver.log").open("w", encoding="utf-8")
        receiver = h2.popen(["python3", "-u", str(RECEIVER_FILE)],
                            cwd=str(ROOT), stdout=receiver_log,
                            stderr=subprocess.STDOUT)
        time.sleep(0.8)
        if receiver.poll() is not None:
            raise RuntimeError("Receiver failed to start. Read receiver.log (port 5000 may already be in use).")
        sender_log = (folder / "sender.log").open("w", encoding="utf-8")
        try:
            send_cmd = ["timeout", "--signal=INT", "--kill-after=3s", f"{GAME_SECONDS}s",
                        "python3", "-u", str(SENDER_FILE)]
            sender = h1.popen(send_cmd, cwd=str(folder), stdout=sender_log,
                              stderr=subprocess.STDOUT)
            sender.wait(timeout=GAME_SECONDS + 7)
        finally:
            sender_log.close()
        collect_summary(folder)
        announce("Testing host connectivity through controller-installed forwarding rules")
        loss = net.pingAll()
        with (folder / "ping_summary.txt").open("w", encoding="utf-8") as f:
            f.write(f"Mininet pingall packet loss: {loss}%\n")
        announce(f"All-in-one demo launched! Results saved in {folder}")
        print("    At mininet> you may type: nodes, net, pingall, or exit", flush=True)
        print("    Type exit when done; this launcher will cleanly stop the network and controller.\n", flush=True)
        CLI(net)
        return 0
    except KeyboardInterrupt:
        print("\nInterrupted. Cleaning up...", flush=True)
        return 130
    except (RuntimeError, subprocess.CalledProcessError, OSError) as error:
        print("\nERROR:", error, flush=True)
        return 1
    finally:
        stop_process(receiver)
        if receiver_log is not None:
            receiver_log.close()
        if qos_created:
            clean_created_qos(qos_created)
        if net is not None:
            announce("Stopping Mininet and removing its virtual network")
            try:
                net.stop()
            except Exception as exc:
                print(f"WARNING during Mininet shutdown: {exc}", flush=True)
        stop_process(ryu)
        if folder is not None:
            owner_files(folder)
            print(f"Saved this run's logs and measurements to: {folder}", flush=True)


if __name__ == "__main__":
    sys.exit(main())

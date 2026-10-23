# Multiplayer Game Communication Network

A **three-player UDP networking prototype** implemented with **Python, Mininet, Open vSwitch, OpenFlow 1.3, and the Ryu SDN controller**. Three virtual players exchange position updates and acknowledgments while the controller identifies UDP game traffic on **port 5000** and assigns it to QoS **Queue 1**. The project records RTT and delivery results and includes a congestion comparison.

> **Platform:** Run this project **inside an Ubuntu virtual machine**, not directly in macOS or Windows. It is a network communication prototype, not a graphical game. Architecture, design decisions, and detailed results belong in the separate D1 design document.

## 1. Mac UTM and Windows VM compatibility

| Host computer | Guest to use | Where to run commands |
| --- | --- | --- |
| **MacBook with UTM** (including Apple Silicon) | Ubuntu Linux VM (ARM64 on Apple Silicon) | **Ubuntu Terminal inside UTM** |
| **Windows PC** with VirtualBox, VMware, or Hyper-V | Ubuntu Linux VM (typically x86_64) | **Ubuntu Terminal inside the VM** |

**The Ubuntu installation and execution commands below are the same on both hosts.** No separate macOS/Windows command sets are needed. Give the VM internet access, `sudo` permission, and support for Linux network namespaces and Open vSwitch. All three virtual hosts and the controller operate **inside that one VM**, so no separate VMs or host port forwarding are required.

## 2. One-time setup inside Ubuntu

Open a **normal Ubuntu Terminal** (not `mininet>`):

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 mininet openvswitch-switch \
  iproute2 iputils-ping build-essential libffi-dev libssl-dev \
  libxml2-dev libxslt1-dev zlib1g-dev
sudo systemctl start openvswitch-switch

git clone https://github.com/akanksharajiv/Multiplayer-Game-Communication-Network.git
cd Multiplayer-Game-Communication-Network
```

The project launchers use **Ubuntu's system `python3`** for Mininet and a **separate Python 3.11 environment** for the older Ryu controller. Create that environment in the repository root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.11
uv venv --python 3.11 .venv-ryu
uv pip install --python .venv-ryu/bin/python 'setuptools==65.5.0' 'pbr==5.11.1' wheel
uv pip install --python .venv-ryu/bin/python --no-build-isolation 'ryu==4.34'
.venv-ryu/bin/ryu-manager --help
```

**If** the last command fails with `ImportError: cannot import name 'ALREADY_HANDLED' from 'eventlet.wsgi'`, Ryu 4.34 is encountering a newer Eventlet API. Apply this narrowly scoped fix to the **installed Ryu environment only**, then verify again (do not edit `game_sdn_controller.py`):

```bash
.venv-ryu/bin/python - <<'PY'
from pathlib import Path
import sysconfig
p = Path(sysconfig.get_paths()['purelib']) / 'ryu/app/wsgi.py'
old = '    from eventlet.wsgi import ALREADY_HANDLED\n    _ALREADY_HANDLED = ALREADY_HANDLED'
new = "    import eventlet.wsgi\n    _ALREADY_HANDLED = getattr(eventlet.wsgi, 'ALREADY_HANDLED', None)"
s = p.read_text()
if old in s:
    p.with_suffix('.py.bak').write_text(s)
    p.write_text(s.replace(old, new, 1))
    print('Applied Ryu compatibility fix')
elif new in s:
    print('Fix already applied')
else:
    raise SystemExit('Unexpected Ryu version/layout; inspect ryu/app/wsgi.py')
PY
.venv-ryu/bin/ryu-manager --help
```

**Check before running:** Confirm that `game_sdn_controller.py`, `launch_project.py`, `multiplayer_demo.py`, and `multiplayer_peer.py` are all present in the cloned repository. The controller and application files must be committed by all team members. Don't upload the `.venv-ryu/` directory to GitHub.

## 3. Run the complete integrated three-player project

From the repository directory **inside Ubuntu**, run:

```bash
sudo python3 multiplayer_demo.py
```

**This is the main D1 execution command.** It automatically:

1. Starts **Person 3's Ryu controller** on port 6653.
2. Creates **Person 2's three-host Mininet/OpenFlow 1.3 network** and configures the **10 Mbps QoS queues** through `launch_project.py` helpers.
3. Runs **Person 1's `multiplayer_peer.py`** on all three hosts simultaneously; each player sends UDP updates to both others and receives acknowledgments.
4. Prints per-player and overall RTT/reply results, records controller-installed game flows, and stops the network/controller on completion.

The output is stored under `multiplayer_runs/<timestamp>/` (`player1.csv`, `player2.csv`, `player3.csv`, logs, `flows.txt`, `summary.txt`). A successful run shows acknowledgments from all three players and `set_queue:1` game flows. **No separate `topology.py`, `setup_qos.sh`, or `ryu-manager` invocation is required** for this integrated mode; equivalent network/QoS setup is called by the launcher.

## 4. Additional verification modes (run separately)

| Command | What it verifies | Output |
| --- | --- | --- |
| `sudo python3 launch_project.py` | Quick **h1 ↔ h2** UDP test, game Queue 1 rules, and `pingall`; type `exit` at `mininet>` to stop | `demo_runs/<timestamp>/` |
| `sudo python3 congestion_experiment.py` | Two successive trials using the same 10 Mbps queue cap, with background UDP traffic and **game Queue 0 vs Queue 1** | `experiment_runs/<timestamp>/` |

**Run only one mode at a time.** All launchers create and clean up their own Mininet network and Ryu controller. These additional commands are *not* necessary to start the complete three-player application, but support the connectivity and prioritisation tests for D1.

## 5. Source files and contributions

| Team member | Files |
| --- | --- |
| **Person 1 – Game communication and evaluation** | `multiplayer_peer.py`, `multiplayer_demo.py`, `sender_mininet.py`, `receiver.py`, `congestion_experiment.py` |
| **Person 2 – Mininet and integration** | `topology.py`, `setup_qos.sh`, `launch_project.py` |
| **Person 3 – SDN control** | `game_sdn_controller.py` |

`topology.py` and `setup_qos.sh` are standalone implementation files. The integrated demos reuse the corresponding topology and QoS functions from `launch_project.py` rather than executing those standalone files. **Do not upload** test-run copies of `game_sdn_controller_integrated.py`; the launcher creates a temporary corrected version from Person 3's original controller in each results folder.

## 6. Troubleshooting

- **`ryu-manager` missing / `No module named 'ryu'`:** Run the Python 3.11 Ryu setup in Section 2, then check `.venv-ryu/bin/ryu-manager --help`.
- **`s1` already exists / port 6653 busy:** Exit any previous `mininet>` session and stop manually running Ryu. After a **crashed** session, `sudo mn -c` clears stale Mininet state. Do not run two experiments at once.
- **Mininet import or OVS service error:** Confirm `sudo python3 -c 'import mininet.net'` succeeds and `sudo systemctl start openvswitch-switch` has completed.
- **A test fails:** Review its timestamped `controller.log`, `player*.log`, `sender.log`, or `summary.txt` output before retrying.

**Recorded evidence (one three-player run):** 180/180 updates acknowledged, mean RTT **2.529 ms**, six controller-installed game flows using Queue 1. These are observed test results, not guaranteed figures. The separate **D1 design document (maximum 2 pages)** covers the architecture, design choices, and experimental interpretation.

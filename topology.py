
from mininet.net import Mininet
from mininet.node import OVSSwitch, RemoteController
from mininet.cli import CLI
from mininet.log import setLogLevel, info

def create_topology():
    net = Mininet(
        controller=None,
        switch=OVSSwitch,
        autoSetMacs=True,
        build=False
    )

    info("*** Adding controller\n")
    net.addController(
        "c0",
        controller=RemoteController,
        ip="127.0.0.1",
        port=6653
    )

    info("*** Adding switch\n")
    s1 = net.addSwitch(
        "s1",
        protocols="OpenFlow13"
    )

    info("*** Adding players\n")
    h1 = net.addHost("h1", ip="10.0.0.1/24")
    h2 = net.addHost("h2", ip="10.0.0.2/24")
    h3 = net.addHost("h3", ip="10.0.0.3/24")

    info("*** Connecting players to switch\n")
    net.addLink(h1, s1)
    net.addLink(h2, s1)
    net.addLink(h3, s1)

    info("*** Building network\n")
    net.build()

    info("*** Starting switch and controller\n")
    net.start()

    info("*** Starting Mininet CLI\n")
    CLI(net)

    info("*** Stopping network\n")
    net.stop()

if __name__ == "__main__":
    setLogLevel("info")
    create_topology()

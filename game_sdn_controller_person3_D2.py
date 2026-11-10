
#!/usr/bin/env python3
"""Dynamic SDN Controller for GameNet.

D1:
- OpenFlow 1.3
- MAC learning and forwarding
- UDP game traffic identification
- Queue 1 prioritisation

D2:
- Periodic port and flow statistics
- Bandwidth utilisation calculation
- Congestion detection
- Dynamic QoS/forwarding policy
- Hysteresis and cooldown
- Congestion recovery
- Controller decision logging
"""

import logging
import os
import time

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    DEAD_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.lib import hub
from ryu.lib.packet import packet, ethernet, ipv4, udp
from ryu.lib.packet import ether_types
from ryu.ofproto import ofproto_v1_3


class GameSDNController(app_manager.RyuApp):

    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    GAME_UDP_PORT = int(os.getenv("GAME_UDP_PORT", "5000"))

    # D2 monitoring configuration
    MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "2"))

    LINK_CAPACITY_BPS = int(
        os.getenv("LINK_CAPACITY_BPS", "10000000")
    )

    CONGESTION_THRESHOLD = float(
        os.getenv("CONGESTION_THRESHOLD", "80")
    )

    RECOVERY_THRESHOLD = float(
        os.getenv("RECOVERY_THRESHOLD", "60")
    )

    CONGESTION_CONFIRM_SAMPLES = int(
        os.getenv("CONGESTION_CONFIRM_SAMPLES", "2")
    )

    RECOVERY_CONFIRM_SAMPLES = int(
        os.getenv("RECOVERY_CONFIRM_SAMPLES", "3")
    )

    POLICY_COOLDOWN_SECONDS = float(
        os.getenv("POLICY_COOLDOWN_SECONDS", "6")
    )

    # Unique cookie for temporary D2 flows
    DYNAMIC_POLICY_COOKIE = 0x47414D4553444E01

    def __init__(self, *args, **kwargs):

        super(GameSDNController, self).__init__(*args, **kwargs)

        self.mac_to_port = {}
        self.datapaths = {}

        # Previous port statistics
        self.previous_port_stats = {}

        # Congestion state
        self.high_util_samples = {}
        self.recovery_samples = {}
        self.policy_active = {}
        self.last_policy_change = {}

        self.logger.setLevel(logging.INFO)

        self.logger.info(
            "D2 CONTROLLER STARTED: interval=%.1fs "
            "capacity=%d bps congestion=%.1f%% recovery=%.1f%%",
            self.MONITOR_INTERVAL,
            self.LINK_CAPACITY_BPS,
            self.CONGESTION_THRESHOLD,
            self.RECOVERY_THRESHOLD,
        )

        # Start background monitoring
        self.monitor_thread = hub.spawn(self._monitor)

    # --------------------------------------------------
    # FLOW INSTALLATION
    # --------------------------------------------------

    def add_flow(
        self,
        datapath,
        priority,
        match,
        actions,
        idle_timeout=0,
        hard_timeout=0,
        cookie=0
    ):

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS,
                actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            cookie=cookie,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )

        datapath.send_msg(flow_mod)

    # --------------------------------------------------
    # SWITCH REGISTRATION
    # --------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPStateChange,
        [MAIN_DISPATCHER, DEAD_DISPATCHER]
    )
    def state_change_handler(self, ev):

        datapath = ev.datapath

        if ev.state == MAIN_DISPATCHER:

            if datapath.id not in self.datapaths:

                self.datapaths[datapath.id] = datapath

                self.logger.info(
                    "MONITOR: Registered switch dpid=%s",
                    datapath.id
                )

        elif ev.state == DEAD_DISPATCHER:

            self.datapaths.pop(datapath.id, None)

            self.logger.info(
                "MONITOR: Unregistered switch dpid=%s",
                datapath.id
            )

    # --------------------------------------------------
    # SWITCH INITIALIZATION
    # --------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPSwitchFeatures,
        CONFIG_DISPATCHER
    )
    def switch_features_handler(self, ev):

        datapath = ev.msg.datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        match = parser.OFPMatch()

        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER
            )
        ]

        self.add_flow(
            datapath,
            0,
            match,
            actions
        )

        self.logger.info(
            "Switch %s connected; OpenFlow 1.3 initialized",
            datapath.id
        )

    # --------------------------------------------------
    # PACKET PROCESSING AND MAC LEARNING
    # --------------------------------------------------

    @set_ev_cls(
        ofp_event.EventOFPPacketIn,
        MAIN_DISPATCHER
    )
    def packet_in_handler(self, ev):

        msg = ev.msg

        datapath = msg.datapath

        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)

        eth = pkt.get_protocol(
            ethernet.ethernet
        )

        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = datapath.id

        self.mac_to_port.setdefault(dpid, {})

        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(
            eth.dst,
            ofproto.OFPP_FLOOD
        )

        ip_pkt = pkt.get_protocol(ipv4.ipv4)

        udp_pkt = pkt.get_protocol(udp.udp)

        is_game = (
            ip_pkt is not None
            and udp_pkt is not None
            and (
                udp_pkt.src_port == self.GAME_UDP_PORT
                or udp_pkt.dst_port == self.GAME_UDP_PORT
            )
        )

        if is_game:

            self.logger.info(
                "GAME UDP: %s:%s -> %s:%s",
                ip_pkt.src,
                udp_pkt.src_port,
                ip_pkt.dst,
                udp_pkt.dst_port
            )

        if out_port != ofproto.OFPP_FLOOD:

            if is_game and ip_pkt is not None and udp_pkt is not None:

                match = parser.OFPMatch(
                    in_port=in_port,
                    eth_type=0x0800,
                    ip_proto=17,
                    ipv4_src=ip_pkt.src,
                    ipv4_dst=ip_pkt.dst,
                    udp_src=udp_pkt.src_port,
                    udp_dst=udp_pkt.dst_port,
                )

                actions = [
                    parser.OFPActionSetQueue(1),
                    parser.OFPActionOutput(out_port)
                ]

                self.add_flow(
                    datapath,
                    200,
                    match,
                    actions,
                    idle_timeout=60
                )

            else:

                # Run-local integration fix: include the packet protocol
                # and UDP ports, so a previous background flow cannot hide
                # subsequent game UDP packets from the controller.
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
                actions = [ parser.OFPActionOutput(out_port)]
                self.add_flow(
                    datapath,10,match,actions,
                    idle_timeout=60
                )

        else:

            actions = [
                parser.OFPActionOutput(out_port)
            ]

        data = (
            None
            if msg.buffer_id != ofproto.OFP_NO_BUFFER
            else msg.data
        )

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions,
            data=data
        )

        datapath.send_msg(out)

    # ==================================================
    # D2 FEATURE 1: PERIODIC STATISTICS MONITORING
    # ==================================================

    def _monitor(self):

        while True:

            for datapath in list(self.datapaths.values()):

                try:

                    self._request_stats(datapath)

                except Exception:

                    self.logger.exception(
                        "MONITOR: Statistics request failed for dpid=%s",
                        datapath.id
                    )

            hub.sleep(self.MONITOR_INTERVAL)

    def _request_stats(self, datapath):

        parser = datapath.ofproto_parser

        ofproto = datapath.ofproto

        # Request port statistics
        port_request = parser.OFPPortStatsRequest(
            datapath,
            0,
            ofproto.OFPP_ANY
        )

        datapath.send_msg(port_request)

        # Request flow statistics
        flow_request = parser.OFPFlowStatsRequest(
            datapath,
            0,
            ofproto.OFPTT_ALL,
            ofproto.OFPP_ANY,
            ofproto.OFPG_ANY,
            0,
            0,
            parser.OFPMatch()
        )

        datapath.send_msg(flow_request)

        self.logger.debug(
            "MONITOR: Requested port and flow statistics from dpid=%s",
            datapath.id
        )

    # ==================================================
    # D2 FEATURE 2: FLOW STATISTICS
    # ==================================================

    @set_ev_cls(
        ofp_event.EventOFPFlowStatsReply,
        MAIN_DISPATCHER
    )
    def flow_stats_reply_handler(self, ev):

        for flow in ev.msg.body:

            if flow.priority > 0:

                self.logger.info(
                    "FLOW dpid=%s priority=%s packets=%s "
                    "bytes=%s match=%s",
                    ev.msg.datapath.id,
                    flow.priority,
                    flow.packet_count,
                    flow.byte_count,
                    flow.match
                )

    # ==================================================
    # D2 FEATURE 3: PORT UTILISATION
    # ==================================================

    @set_ev_cls(
        ofp_event.EventOFPPortStatsReply,
        MAIN_DISPATCHER
    )
    def port_stats_reply_handler(self, ev):

        datapath = ev.msg.datapath

        dpid = datapath.id

        now = time.monotonic()

        utilization_by_port = {}

        for port in ev.msg.body:

            if port.port_no >= datapath.ofproto.OFPP_MAX:
                continue

            key = (dpid, port.port_no)

            previous = self.previous_port_stats.get(key)

            self.logger.info(
                "PORT dpid=%s port=%s rx_packets=%s tx_packets=%s "
                "rx_bytes=%s tx_bytes=%s rx_errors=%s tx_errors=%s",
                dpid,
                port.port_no,
                port.rx_packets,
                port.tx_packets,
                port.rx_bytes,
                port.tx_bytes,
                port.rx_errors,
                port.tx_errors
            )

            if previous is not None:

                old_bytes, old_time = previous

                elapsed = now - old_time

                delta_bytes = port.tx_bytes - old_bytes

                # Ignore invalid counter resets
                if elapsed > 0 and delta_bytes >= 0:

                    tx_bps = (
                        delta_bytes * 8.0
                    ) / elapsed

                    utilization = min(
                        100.0,
                        (
                            tx_bps / self.LINK_CAPACITY_BPS
                        ) * 100.0
                    )

                    utilization_by_port[port.port_no] = utilization

                    self.logger.info(
                        "UTILIZATION dpid=%s port=%s "
                        "tx_mbps=%.3f utilization=%.2f%%",
                        dpid,
                        port.port_no,
                        tx_bps / 1_000_000.0,
                        utilization
                    )

            self.previous_port_stats[key] = (
                port.tx_bytes,
                now
            )

        if utilization_by_port:

            self._update_congestion_state(
                datapath,
                utilization_by_port
            )

    # ==================================================
    # D2 FEATURE 4: CONGESTION DETECTION
    # ==================================================

    def _update_congestion_state(
        self,
        datapath,
        utilization_by_port
    ):

        dpid = datapath.id

        now = time.monotonic()

        high = any(
            value >= self.CONGESTION_THRESHOLD
            for value in utilization_by_port.values()
        )

        low = all(
            value <= self.RECOVERY_THRESHOLD
            for value in utilization_by_port.values()
        )

        self.high_util_samples.setdefault(dpid, 0)

        self.recovery_samples.setdefault(dpid, 0)

        self.policy_active.setdefault(dpid, False)

        self.last_policy_change.setdefault(dpid, 0.0)

        cooldown_done = (
            now - self.last_policy_change[dpid]
            >= self.POLICY_COOLDOWN_SECONDS
        )

        if not self.policy_active[dpid]:

            self.recovery_samples[dpid] = 0

            if high:

                self.high_util_samples[dpid] += 1

                self.logger.warning(
                    "CONGESTION CANDIDATE dpid=%s sample=%s/%s",
                    dpid,
                    self.high_util_samples[dpid],
                    self.CONGESTION_CONFIRM_SAMPLES
                )

            else:

                self.high_util_samples[dpid] = 0

            if (
                self.high_util_samples[dpid]
                >= self.CONGESTION_CONFIRM_SAMPLES
                and cooldown_done
            ):

                self._activate_congestion_policy(datapath)

                self.high_util_samples[dpid] = 0

        else:

            self.high_util_samples[dpid] = 0

            if low:

                self.recovery_samples[dpid] += 1

                self.logger.info(
                    "RECOVERY CANDIDATE dpid=%s sample=%s/%s",
                    dpid,
                    self.recovery_samples[dpid],
                    self.RECOVERY_CONFIRM_SAMPLES
                )

            else:

                self.recovery_samples[dpid] = 0

            if (
                self.recovery_samples[dpid]
                >= self.RECOVERY_CONFIRM_SAMPLES
                and cooldown_done
            ):

                self._restore_normal_policy(datapath)

                self.recovery_samples[dpid] = 0

    # ==================================================
    # D2 FEATURE 5: DYNAMIC POLICY ACTIVATION
    # ==================================================

    def _switch_data_ports(self, datapath):

        limit = datapath.ofproto.OFPP_MAX

        return sorted(
            port_no
            for port_no in datapath.ports
            if 0 < port_no < limit
        )

    def _activate_congestion_policy(self, datapath):
        dpid = datapath.id
        parser = datapath.ofproto_parser
        learned = self.mac_to_port.get(dpid, {})
        ports = self._switch_data_ports(datapath)
        installed = 0

        for in_port in ports:
            for destination_mac, out_port in sorted(learned.items()):
                if out_port == in_port or out_port not in ports:
                    continue

                # Destination-aware game packets receive Queue 1.
                for field in ("udp_src", "udp_dst"):
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_dst=destination_mac,
                        eth_type=0x0800,
                        ip_proto=17,
                        **{field: self.GAME_UDP_PORT}
                    )
                    self.add_flow(
                        datapath, 250, match,
                        [parser.OFPActionSetQueue(1),
                         parser.OFPActionOutput(out_port)],
                        cookie=self.DYNAMIC_POLICY_COOKIE,
                    )
                    installed += 1

                # Ordinary TCP and non-game UDP receive Queue 0.
                # Game UDP above has a higher matching priority.
                for protocol in (6, 17):
                    match = parser.OFPMatch(
                        in_port=in_port,
                        eth_dst=destination_mac,
                        eth_type=0x0800,
                        ip_proto=protocol,
                    )
                    self.add_flow(
                        datapath, 100, match,
                        [parser.OFPActionSetQueue(0),
                         parser.OFPActionOutput(out_port)],
                        cookie=self.DYNAMIC_POLICY_COOKIE,
                    )
                    installed += 1

        self.policy_active[dpid] = True
        self.last_policy_change[dpid] = time.monotonic()
        self.logger.warning(
            "POLICY CHANGE: CONGESTION ACTIVE dpid=%s "
            "dynamic_flows=%s game=Queue1 background_TCP_UDP=Queue0 "
            "(destination-aware integration)", dpid, installed
        )

    # ==================================================
    # D2 FEATURE 6: CONGESTION RECOVERY
    # ==================================================

    def _restore_normal_policy(self, datapath):

        ofproto = datapath.ofproto

        parser = datapath.ofproto_parser

        # Delete only the temporary D2 policy flows.
        # D1 learned forwarding rules remain untouched.
        delete_request = parser.OFPFlowMod(
            datapath=datapath,
            cookie=self.DYNAMIC_POLICY_COOKIE,
            cookie_mask=0xFFFFFFFFFFFFFFFF,
            table_id=ofproto.OFPTT_ALL,
            command=ofproto.OFPFC_DELETE,
            out_port=ofproto.OFPP_ANY,
            out_group=ofproto.OFPG_ANY,
            match=parser.OFPMatch()
        )

        datapath.send_msg(delete_request)

        dpid = datapath.id

        self.policy_active[dpid] = False

        self.last_policy_change[dpid] = time.monotonic()

        self.logger.warning(
            "POLICY RECOVERY: NORMAL POLICY RESTORED dpid=%s; "
            "removed temporary D2 flows and retained D1 learned flows",
            dpid
        )

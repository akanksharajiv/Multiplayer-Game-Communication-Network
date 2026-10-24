
from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import (
    CONFIG_DISPATCHER,
    MAIN_DISPATCHER,
    set_ev_cls,
)
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ipv4, udp
from ryu.lib.packet import ether_types
import os
import logging


class GameSDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    GAME_UDP_PORT = int(os.getenv("GAME_UDP_PORT", "5000"))

    def __init__(self, *args, **kwargs):
        super(GameSDNController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}
        self.logger.setLevel(logging.INFO)

    def add_flow(
        self, datapath, priority, match, actions,
        idle_timeout=0, hard_timeout=0
    ):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        instructions = [
            parser.OFPInstructionActions(
                ofproto.OFPIT_APPLY_ACTIONS, actions
            )
        ]

        flow_mod = parser.OFPFlowMod(
            datapath=datapath,
            priority=priority,
            match=match,
            instructions=instructions,
            idle_timeout=idle_timeout,
            hard_timeout=hard_timeout,
        )
        datapath.send_msg(flow_mod)

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser

        # Send unknown packets to the controller for learning.
        match = parser.OFPMatch()
        actions = [
            parser.OFPActionOutput(
                ofproto.OFPP_CONTROLLER,
                ofproto.OFPCML_NO_BUFFER,
            )
        ]

        self.add_flow(datapath, 0, match, actions)
        self.logger.info(
            "Switch %s connected; OpenFlow 1.3 table initialized",
            datapath.id,
        )

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)

        if eth is None or eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dpid = datapath.id
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][eth.src] = in_port

        out_port = self.mac_to_port[dpid].get(
            eth.dst, ofproto.OFPP_FLOOD
        )

        actions = []

        # Identify IPv4 UDP game traffic.
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
                ip_pkt.src, udp_pkt.src_port,
                ip_pkt.dst, udp_pkt.dst_port,
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
                # Reserved for the configured game queue.
                # Queue 1 must be configured on the egress port
                # before this action can provide prioritisation.
                actions = [
                    parser.OFPActionSetQueue(1),
                    parser.OFPActionOutput(out_port),
                ]
                self.add_flow(
                    datapath, 200, match, actions,
                    idle_timeout=60,
                )
            else:
                match = parser.OFPMatch(
                    in_port=in_port,
                    eth_dst=eth.dst,
                )
                actions = [parser.OFPActionOutput(out_port)]
                self.add_flow(
                    datapath, 10, match, actions,
                    idle_timeout=60,
                )
        else:
            actions = [parser.OFPActionOutput(out_port)]

        actions_for_packet = actions
        data = None if msg.buffer_id != ofproto.OFP_NO_BUFFER else msg.data

        out = parser.OFPPacketOut(
            datapath=datapath,
            buffer_id=msg.buffer_id,
            in_port=in_port,
            actions=actions_for_packet,
            data=data,
        )
        datapath.send_msg(out)

    @set_ev_cls(
        ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER
    )
    def flow_stats_reply_handler(self, ev):
        for flow in ev.msg.body:
            if flow.priority > 0:
                self.logger.info(
                    "FLOW dpid=%s priority=%s packets=%s bytes=%s match=%s",
                    ev.msg.datapath.id,
                    flow.priority,
                    flow.packet_count,
                    flow.byte_count,
                    flow.match,
                )

    @set_ev_cls(
        ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER
    )
    def port_stats_reply_handler(self, ev):
        for port in ev.msg.body:
            self.logger.info(
                "PORT dpid=%s port=%s rx_packets=%s tx_packets=%s "
                "rx_bytes=%s tx_bytes=%s",
                ev.msg.datapath.id,
                port.port_no,
                port.rx_packets,
                port.tx_packets,
                port.rx_bytes,
                port.tx_bytes,
            )

"""
Implements a packet generator that simulates the TCP protocol, including support for
various congestion control mechanisms.
"""
from ns.packet.packet import Packet
from ns.utils.timer import Timer


class TCPPacketGenerator:
    """ Generates packets with a simulated TCP protocol.

        Parameters
        ----------
        env: simpy.Environment
            The simulation environment.
        flow: Flow
            The flow that serves as the source (eventually, this should be a list).
        element_id: str
            The ID for this element.
        rec_flow: bool
            Are we recording the statistics of packets generated?
    """
    def __init__(self,
                 env,
                 flow,
                 element_id=None,
                 rtt_estimate=1,
                 debug=False):
        self.element_id = element_id
        self.env = env
        self.out = None
        self.packets_sent = 0
        self.flow = flow
        self.bytes_acked = 0

        self.mss = 512  # maximum segment size, in bytes

        # the next sequence number to be sent, in bytes
        self.next_seq = 0
        # the maximum sequence number in the in-transit data buffer
        self.send_buffer = 0
        # the size of the congestion window, initialized to one segment
        self.cwnd = self.mss
        # the slow-start threshold, initialized to 65535 bytes (RFC 2001)
        self.ssthresh = 65535
        # the sequence number of the segment that is last acknowledged
        self.last_ack = 0
        # the count of duplicate acknolwedgments
        self.dupack = 0
        # the RTT estimate
        self.rtt_estimate = rtt_estimate
        # the retransmission timeout
        self.rto = self.rtt_estimate * 2
        # an estimate of the RTT deviation
        self.est_deviation = 0

        # the timers, one for each in-flight packets (segments) sent
        self.timers = {}
        # the time when each in-flight packets (segments) was sent
        self.sent_times = {}
        # the in-flight packets (segments)
        self.sent_packets = {}

        self.action = env.process(self.run())
        self.debug = debug

    def run(self):
        """The generator function used in simulations."""
        yield self.env.timeout(self.flow.start_time)

        while self.env.now < self.flow.finish_time and self.bytes_acked <= self.flow.size:
            # wait for the next data arrival
            if self.flow.arrival_dist is not None:
                yield self.env.timeout(self.flow.arrival_dist())
            if self.flow.size_dist is not None:
                self.send_buffer += self.flow.size_dist()
            else:
                self.send_buffer += self.mss

            # the sender can transmit up to the size of the congestion window
            if self.last_ack + self.cwnd >= self.next_seq + self.mss:
                self.packets_sent += 1

                packet = Packet(self.env.now,
                                self.mss,
                                self.next_seq,
                                src=self.element_id,
                                flow_id=self.flow.fid)

                self.sent_times[packet.packet_id + packet.size] = packet.time
                self.sent_packets[packet.packet_id + packet.size] = packet

                if self.debug:
                    print(
                        f"Sent packet {packet.packet_id} with flow_id {packet.flow_id} at "
                        f"time {self.env.now}.")

                self.out.put(packet)

                self.next_seq += packet.size
                self.timers[packet.packet_id] = Timer(self.env,
                                                      packet.packet_id,
                                                      self.timeout_callback,
                                                      self.rto)

    def timeout_callback(self, packet_id):
        """ To be called when a timer expired for a packet with 'packet_id'. """
        print(f"Timer expired for packet #{packet_id}.")

        # setting the congestion window to 1 segment
        self.cwnd = self.mss

        # doubling the retransmission timeout
        self.rto *= 2

        # retransmitting the segment
        resent_pkt = self.sent_packets[packet_id]
        if self.debug:
            print(
                f"Resending packet {resent_pkt.packet_id} with flow_id {resent_pkt.flow_id} at "
                f"time {self.env.now}.")

        self.out.put(resent_pkt)

        # starting a new timer for this segment
        self.timers[packet_id].restart(self.rto)

    def put(self, packet):
        """ On receiving an acknowledgment packet. """
        assert packet.flow_id > 10000  # the received packet must be an ack

        if packet.flow_id == self.last_ack:
            self.dupack += 1
        else:
            # fast recovery in RFC 2001 and TCP Reno
            self.cwnd = self.ssthresh
            self.dupack = 0

        if self.dupack == 3:
            # fast retransmit in RFC 2001 and TCP Reno
            self.ssthresh = max(2 * self.mss, self.cwnd / 2)
            self.cwnd = self.ssthresh + 3 * self.mss

            resent_pkt = self.sent_packets[packet.packet_id]
            if self.debug:
                print(f"Resending packet {resent_pkt.packet_id} with flow_id "
                      f"{resent_pkt.flow_id} at time {self.env.now}.")

            self.out.put(resent_pkt)

            return
        elif self.dupack > 3:
            # fast retransmit in RFC 2001 and TCP Reno
            self.cwnd += self.mss

            if self.last_ack + self.cwnd >= packet.packet_id:
                resent_pkt = self.sent_packets[packet.packet_id]
                if self.debug:
                    print(
                        f"Resending packet {resent_pkt.packet_id} with flow_id "
                        f"{resent_pkt.flow_id} at time {self.env.now}.")

                self.out.put(resent_pkt)

            return

        if self.dupack == 0:
            # new ack received, update the RTT estimate and the retransmission timout
            sample_rtt = self.env.now - self.sent_times[packet.packet_id]

            # Jacobsen '88: Congestion Avoidance and Control
            sample_err = sample_rtt - self.rtt_estimate
            self.rtt_estimate += 0.125 * sample_err
            self.est_deviation += 0.25 * (abs(sample_err) - self.est_deviation)
            self.rto = self.rtt_estimate + 4 * self.est_deviation

            self.last_ack = packet.packet_id

            if self.cwnd <= self.ssthresh:
                # slow start
                self.cwnd += self.mss
            else:
                # congestion avoidance
                self.cwnd += self.mss * self.mss / self.cwnd

            self.timers[packet.packet_id].stop()
            del self.timers[packet.packet_id]
            del self.sent_times[packet.packet_id]
            del self.sent_packets[packet.packet_id]
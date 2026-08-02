class BGPPeerFSM:
    def __init__(self, local_asn: int, remote_asn: int):
        self.local_asn = local_asn
        self.remote_asn = remote_asn
        self.state = "IDLE"
        self.hold_time = 90

    def connect(self) -> str:
        if self.state == "IDLE": self.state = "CONNECT"
        return self.state

    def receive_open(self, remote_asn: int, hold_time: int) -> bool:
        if self.state in ("CONNECT", "ACTIVE") and remote_asn == self.remote_asn:
            self.hold_time = hold_time
            self.state = "OPENCONFIRM"
            return True
        return False

    def receive_keepalive(self) -> str:
        if self.state == "OPENCONFIRM": self.state = "ESTABLISHED"
        return self.state

    def receive_notification(self, code: int) -> str:
        self.state = "IDLE"
        return self.state

    def get_state(self) -> str: return self.state

def create_bgp_fsm(local_asn: int = 65001, remote_asn: int = 65002):
    return BGPPeerFSM(local_asn, remote_asn)

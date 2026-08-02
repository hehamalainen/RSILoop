class SIPCallFSM:
    def __init__(self, call_id: str):
        self.call_id = call_id
        self.state = "IDLE"

    def send_invite(self) -> str:
        if self.state == "IDLE": self.state = "CALLING"
        return self.state

    def receive_trying_100(self) -> str:
        if self.state in ("CALLING", "PROCEEDING"): self.state = "PROCEEDING"
        return self.state

    def receive_ringing_180(self) -> str:
        if self.state in ("CALLING", "PROCEEDING"): self.state = "PROCEEDING"
        return self.state

    def receive_ok_200(self) -> str:
        if self.state == "PROCEEDING": self.state = "EARLY_OK"
        return self.state

    def send_ack(self) -> str:
        if self.state == "EARLY_OK": self.state = "CONFIRMED"
        return self.state

    def send_bye(self) -> str:
        if self.state == "CONFIRMED": self.state = "TERMINATED"
        return self.state

    def get_state(self) -> str: return self.state

def create_sip_fsm(call_id: str = "call_123"): return SIPCallFSM(call_id)

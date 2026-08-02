class Rung1BGP:
    def __init__(self, l, r): self.state = "IDLE"; self.hold_time = 90; self.local_asn = l; self.remote_asn = r
    def connect(self): self.state = "CONNECT"; return "CONNECT"
    def receive_open(self, r, h): self.state = "OPENCONFIRM"; return True
    def receive_keepalive(self): self.state = "ESTABLISHED"; return "ESTABLISHED"
    def receive_notification(self, c): self.state = "IDLE"; return "IDLE"
    def get_state(self): return self.state

def rung_1_solution(l, r): return Rung1BGP(l, r)

class Rung2BGP:
    def __init__(self, l, r): self.state = "IDLE"; self.hold_time = 90; self.local_asn = l; self.remote_asn = r
    def connect(self): return "IDLE"
    def receive_open(self, r, h): return False
    def receive_keepalive(self): return "IDLE"
    def receive_notification(self, c): return "IDLE"
    def get_state(self): return "IDLE"

def rung_2_solution(l, r): return Rung2BGP(l, r)

def rung_3_solution(l, r): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

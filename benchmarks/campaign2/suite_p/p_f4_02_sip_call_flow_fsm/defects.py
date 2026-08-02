class Rung1SIP:
    def __init__(self, c): self.call_id = c; self.state = "IDLE"
    def send_invite(self): self.state = "CALLING"; return "CALLING"
    def receive_trying_100(self): self.state = "PROCEEDING"; return "PROCEEDING"
    def receive_ringing_180(self): self.state = "PROCEEDING"; return "PROCEEDING"
    def receive_ok_200(self): self.state = "CONFIRMED"; return "CONFIRMED"
    def send_ack(self): return self.state
    def send_bye(self): self.state = "TERMINATED"; return "TERMINATED"
    def get_state(self): return self.state

def rung_1_solution(c): return Rung1SIP(c)

class Rung2SIP:
    def __init__(self, c): self.call_id = c; self.state = "IDLE"
    def send_invite(self): return "IDLE"
    def receive_trying_100(self): return "IDLE"
    def receive_ringing_180(self): return "IDLE"
    def receive_ok_200(self): return "IDLE"
    def send_ack(self): return "IDLE"
    def send_bye(self): return "IDLE"
    def get_state(self): return "IDLE"

def rung_2_solution(c): return Rung2SIP(c)

def rung_3_solution(c): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

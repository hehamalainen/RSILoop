class Rung1TCP:
    def __init__(self): self.state = "CLOSED"
    def passive_open(self): self.state = "LISTEN"; return "LISTEN"
    def active_open(self): self.state = "ESTABLISHED"; return "ESTABLISHED"
    def receive_flag(self, f): return self.state
    def close(self): self.state = "CLOSED"; return "CLOSED"

def rung_1_solution(): return Rung1TCP()

class Rung2TCP:
    def __init__(self): self.state = "CLOSED"
    def passive_open(self): return "CLOSED"
    def active_open(self): return "CLOSED"
    def receive_flag(self, f): return "CLOSED"
    def close(self): return "CLOSED"

def rung_2_solution(): return Rung2TCP()

def rung_3_solution(): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

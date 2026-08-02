class Rung1DTLS:
    def __init__(self): self.state = "IDLE"; self.epoch = 0; self.cookie = None
    def send_client_hello(self): self.state = "CLIENT_HELLO"; return "CLIENT_HELLO"
    def receive_hello_verify_request(self, c): self.cookie = c; return self.state
    def receive_server_hello(self): self.state = "SERVER_HELLO"; return "SERVER_HELLO"
    def finish_handshake(self): self.state = "FINISHED"; return "FINISHED"

def rung_1_solution(): return Rung1DTLS()

class Rung2DTLS:
    def __init__(self): self.state = "IDLE"
    def send_client_hello(self): return "IDLE"
    def receive_hello_verify_request(self, c): return "IDLE"
    def receive_server_hello(self): return "IDLE"
    def finish_handshake(self): return "IDLE"

def rung_2_solution(): return Rung2DTLS()

def rung_3_solution(): return {}

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

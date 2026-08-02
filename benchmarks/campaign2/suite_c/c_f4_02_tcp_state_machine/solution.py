class TCPConnection:
    def __init__(self):
        self.state = "CLOSED"

    def passive_open(self) -> str:
        if self.state == "CLOSED": self.state = "LISTEN"
        return self.state

    def active_open(self) -> str:
        if self.state == "CLOSED": self.state = "SYN_SENT"
        return self.state

    def receive_flag(self, flag: str) -> str:
        if self.state == "LISTEN" and flag == "SYN": self.state = "SYN_RCVD"
        elif self.state == "SYN_SENT" and flag == "SYN_ACK": self.state = "ESTABLISHED"
        elif self.state == "SYN_RCVD" and flag == "ACK": self.state = "ESTABLISHED"
        elif self.state == "ESTABLISHED" and flag == "FIN": self.state = "CLOSE_WAIT"
        elif self.state == "FIN_WAIT_1" and flag == "ACK": self.state = "FIN_WAIT_2"
        elif self.state == "FIN_WAIT_2" and flag == "FIN": self.state = "TIME_WAIT"
        return self.state

    def close(self) -> str:
        if self.state == "ESTABLISHED": self.state = "FIN_WAIT_1"
        elif self.state == "CLOSE_WAIT": self.state = "LAST_ACK"
        return self.state

def create_tcp_fsm(): return TCPConnection()

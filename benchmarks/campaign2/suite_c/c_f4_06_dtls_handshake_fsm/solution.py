class DTLSHandshake:
    def __init__(self):
        self.state = "IDLE"
        self.cookie = None
        self.epoch = 0

    def send_client_hello(self) -> str:
        if self.state == "IDLE": self.state = "CLIENT_HELLO"
        return self.state

    def receive_hello_verify_request(self, cookie: str) -> str:
        if self.state == "CLIENT_HELLO":
            self.cookie = cookie
            self.state = "CLIENT_HELLO_WITH_COOKIE"
        return self.state

    def receive_server_hello(self) -> str:
        if self.state in ("CLIENT_HELLO", "CLIENT_HELLO_WITH_COOKIE"):
            self.state = "SERVER_HELLO"
        return self.state

    def finish_handshake(self) -> str:
        if self.state == "SERVER_HELLO":
            self.state = "FINISHED"
            self.epoch = 1
        return self.state

def create_dtls_fsm(): return DTLSHandshake()

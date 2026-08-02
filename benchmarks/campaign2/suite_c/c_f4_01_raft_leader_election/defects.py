class Rung1Raft:
    def __init__(self, i, p):
        self.node_id = i; self.peers = p; self.state = "FOLLOWER"; self.current_term = 0; self.voted_for = None; self.votes_received = set()
    def handle_election_timeout(self):
        self.current_term += 1; self.state = "CANDIDATE"; self.voted_for = self.node_id; self.votes_received = {self.node_id}; return self.state
    def receive_request_vote(self, t, c):
        return True
    def receive_vote_response(self, p, g):
        if g: self.votes_received.add(p)
        if len(self.votes_received) >= (len(self.peers)+1)//2 + 1: self.state = "LEADER"
        return self.state

def rung_1_solution(i, p): return Rung1Raft(i, p)

class Rung2Raft:
    def __init__(self, i, p): self.state = "FOLLOWER"; self.current_term = 0; self.voted_for = None
    def handle_election_timeout(self): self.state = "CANDIDATE"; return "CANDIDATE"
    def receive_request_vote(self, t, c): return False
    def receive_vote_response(self, p, g): return "CANDIDATE"

def rung_2_solution(i, p): return Rung2Raft(i, p)

def rung_3_solution(i, p): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

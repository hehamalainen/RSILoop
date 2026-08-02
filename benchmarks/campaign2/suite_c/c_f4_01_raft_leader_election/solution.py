class RaftNode:
    def __init__(self, node_id: str, peers: list[str]):
        self.node_id = node_id
        self.peers = peers
        self.state = "FOLLOWER"
        self.current_term = 0
        self.voted_for = None
        self.votes_received = set()

    def handle_election_timeout(self) -> str:
        self.current_term += 1
        self.state = "CANDIDATE"
        self.voted_for = self.node_id
        self.votes_received = {self.node_id}
        return self.state

    def receive_request_vote(self, term: int, candidate_id: str) -> bool:
        if term > self.current_term:
            self.current_term = term
            self.state = "FOLLOWER"
            self.voted_for = None
        if term == self.current_term and (self.voted_for is None or self.voted_for == candidate_id):
            self.voted_for = candidate_id
            return True
        return False

    def receive_vote_response(self, from_peer: str, granted: bool) -> str:
        if self.state == "CANDIDATE" and granted:
            self.votes_received.add(from_peer)
            majority = (len(self.peers) + 1) // 2 + 1
            if len(self.votes_received) >= majority:
                self.state = "LEADER"
        return self.state

def create_raft_node(node_id: str, peers: list[str]): return RaftNode(node_id, peers)

def run_evaluation(module):
    func = getattr(module, "create_raft_node", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        if n.state == "FOLLOWER" and n.current_term == 0: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        if n.handle_election_timeout() == "CANDIDATE" and n.current_term == 1: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        if n.receive_request_vote(1, "n2") is True: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"]); n.handle_election_timeout()
        if n.receive_vote_response("n2", True) == "LEADER": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        n.receive_request_vote(1, "n2")
        if n.receive_request_vote(1, "n3") is False: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        n.current_term = 5
        if n.receive_request_vote(3, "n2") is False: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"]); n.handle_election_timeout()
        if n.receive_vote_response("n2", False) == "CANDIDATE": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        n = func("n1", ["n2"])
        n.handle_election_timeout()
        n.receive_vote_response("n2", True)
        if n.state == "LEADER": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        n.current_term = 5
        if n.receive_request_vote(6, "n2") is True and n.current_term == 6: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        n = func("n1", ["n2", "n3"])
        if n.voted_for is None: earned += 1
    except: pass
    return earned, 10

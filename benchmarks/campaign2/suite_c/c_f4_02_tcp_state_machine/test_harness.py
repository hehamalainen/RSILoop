def run_evaluation(module):
    func = getattr(module, "create_tcp_fsm", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        t = func()
        if t.state == "CLOSED": earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        t = func()
        if t.passive_open() == "LISTEN": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        t = func()
        if t.active_open() == "SYN_SENT": earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        t = func(); t.passive_open()
        if t.receive_flag("SYN") == "SYN_RCVD": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        t = func(); t.passive_open(); t.receive_flag("SYN")
        if t.receive_flag("ACK") == "ESTABLISHED": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        t = func(); t.active_open()
        if t.receive_flag("SYN_ACK") == "ESTABLISHED": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        t = func(); t.active_open(); t.receive_flag("SYN_ACK")
        if t.close() == "FIN_WAIT_1": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        t = func(); t.active_open(); t.receive_flag("SYN_ACK"); t.close(); t.receive_flag("ACK")
        if t.receive_flag("FIN") == "TIME_WAIT": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        t = func(); t.active_open(); t.receive_flag("SYN_ACK")
        if t.receive_flag("FIN") == "CLOSE_WAIT": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        t = func()
        if t.receive_flag("INVALID") == "CLOSED": earned += 1
    except: pass
    return earned, 10

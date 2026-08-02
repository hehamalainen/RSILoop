def run_evaluation(module):
    func = getattr(module, "create_bgp_fsm", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        b = func(65001, 65002)
        if b.get_state() == "IDLE": earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        b = func(65001, 65002)
        if b.connect() == "CONNECT": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        b = func(65001, 65002); b.connect()
        if b.receive_open(65002, 90) is True and b.get_state() == "OPENCONFIRM": earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        b = func(65001, 65002); b.connect(); b.receive_open(65002, 90)
        if b.receive_keepalive() == "ESTABLISHED": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        b = func(65001, 65002); b.connect(); b.receive_open(65002, 90); b.receive_keepalive()
        if b.receive_notification(1) == "IDLE": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        b = func(65001, 65002); b.connect()
        if b.receive_open(99999, 90) is False: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        b = func(65001, 65002)
        if b.hold_time == 90: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        b = func(65001, 65002); b.connect(); b.receive_open(65002, 30)
        if b.hold_time == 30: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        b = func(65001, 65002)
        if b.local_asn == 65001 and b.remote_asn == 65002: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        b = func(65001, 65002); b.connect(); b.receive_open(65002, 90); b.receive_keepalive()
        if b.get_state() == "ESTABLISHED": earned += 1
    except: pass
    return earned, 10

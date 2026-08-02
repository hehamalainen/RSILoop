def run_evaluation(module):
    func = getattr(module, "create_sip_fsm", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        s = func("call1")
        if s.get_state() == "IDLE": earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        s = func("call1")
        if s.send_invite() == "CALLING": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        s = func("call1"); s.send_invite()
        if s.receive_trying_100() == "PROCEEDING": earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        s = func("call1"); s.send_invite(); s.receive_trying_100()
        if s.receive_ok_200() == "EARLY_OK": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        s = func("call1"); s.send_invite(); s.receive_trying_100(); s.receive_ok_200()
        if s.send_ack() == "CONFIRMED": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        s = func("call1"); s.send_invite(); s.receive_trying_100(); s.receive_ok_200(); s.send_ack()
        if s.send_bye() == "TERMINATED": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        s = func("call1")
        if s.send_bye() == "IDLE": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        s = func("call1"); s.send_invite()
        if s.receive_ringing_180() == "PROCEEDING": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        s = func("my_call_id")
        if s.call_id == "my_call_id": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        s = func("call1"); s.send_invite(); s.receive_trying_100(); s.receive_ok_200(); s.send_ack()
        if s.get_state() == "CONFIRMED": earned += 1
    except: pass
    return earned, 10

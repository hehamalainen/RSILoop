def run_evaluation(module):
    func = getattr(module, "create_dtls_fsm", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        d = func()
        if d.state == "IDLE" and d.epoch == 0: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        d = func()
        if d.send_client_hello() == "CLIENT_HELLO": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        d = func(); d.send_client_hello()
        if d.receive_hello_verify_request("secret_cookie") == "CLIENT_HELLO_WITH_COOKIE": earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        d = func(); d.send_client_hello(); d.receive_hello_verify_request("cookie")
        if d.cookie == "cookie": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        d = func(); d.send_client_hello(); d.receive_hello_verify_request("cookie")
        if d.receive_server_hello() == "SERVER_HELLO": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        d = func(); d.send_client_hello(); d.receive_hello_verify_request("cookie"); d.receive_server_hello()
        if d.finish_handshake() == "FINISHED": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        d = func(); d.send_client_hello(); d.receive_hello_verify_request("cookie"); d.receive_server_hello(); d.finish_handshake()
        if d.epoch == 1: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        d = func()
        if d.finish_handshake() == "IDLE": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        d = func(); d.send_client_hello(); d.receive_server_hello()
        if d.state == "SERVER_HELLO": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        d = func()
        if d.cookie is None: earned += 1
    except: pass
    return earned, 10

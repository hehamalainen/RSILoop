def run_evaluation(module):
    func = getattr(module, "create_websocket_parser", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x81, 0x02]) + b"hi")
        if res == [{"fin": True, "opcode": 1, "payload": b"hi"}]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        p = func()
        mask = bytes([0x01, 0x02, 0x03, 0x04])
        raw = bytes(b ^ mask[i%4] for i, b in enumerate(b"hi"))
        res = p.feed_bytes(bytes([0x81, 0x82]) + mask + raw)
        if res[0]["payload"] == b"hi": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        p = func()
        if p.feed_bytes(b"") == []: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x88, 0x00]))
        if res[0]["opcode"] == 8: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x89, 0x00]))
        if res[0]["opcode"] == 9: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        p = func()
        res1 = p.feed_bytes(bytes([0x81, 0x02]) + b"h")
        res2 = p.feed_bytes(b"i")
        if len(res1) == 0 and len(res2) == 1 and res2[0]["payload"] == b"hi": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x81, 0x00]))
        if res[0]["payload"] == b"": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x81, 0x02]) + b"ab" + bytes([0x81, 0x02]) + b"cd")
        if len(res) == 2: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x81, 0x02]) + b"ab")
        if res[0]["fin"] is True: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        p = func()
        res = p.feed_bytes(bytes([0x01, 0x02]) + b"ab")
        if res[0]["fin"] is False: earned += 1
    except: pass
    return earned, 10

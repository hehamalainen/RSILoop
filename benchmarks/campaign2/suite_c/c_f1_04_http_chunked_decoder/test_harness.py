def run_evaluation(module):
    func = getattr(module, "decode_chunked_stream", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        if func(bytes([0x35]) + b"\r\nhello\r\n" + bytes([0x30]) + b"\r\n\r\n") == {"data": b"hello", "trailers": {}}: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        if func(bytes([0x35]) + b"\r\nhello\r\n" + bytes([0x35]) + b"\r\nworld\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"] == b"helloworld": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func(bytes([0x35]) + b"\r\nhello\r\n" + bytes([0x30]) + b"\r\nX-Trailer: test\r\n\r\n")["trailers"] == {"X-Trailer": "test"}: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func(b"") == {"data": b"", "trailers": {}}: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if func(bytes([0x30]) + b"\r\n\r\n")["data"] == b"": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if len(func(bytes([0x41]) + b"\r\n0123456789\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"]) == 10: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func(bytes([0x31]) + b"\r\na\r\n" + bytes([0x31]) + b"\r\nb\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"] == b"ab": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func(bytes([0x33]) + b"\r\nabc\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"] == b"abc": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if len(func(bytes([0x32]) + b"\r\nxy\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"]) == 2: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        if func(bytes([0x34]) + b"\r\ntest\r\n" + bytes([0x30]) + b"\r\n\r\n")["data"] == b"test": earned += 1
    except: pass
    return earned, 10

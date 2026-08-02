def run_evaluation(module):
    func = getattr(module, "decode_varint_stream", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        if func(bytes([0x01, 0x02, 0x03])) == [1, 2, 3]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        if func(bytes([0x96, 0x01])) == [150]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func(bytes([0x00])) == [0]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func(b"") == []: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if func(bytes([0xAC, 0x02])) == [300]: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func(bytes([0x7F])) == [127]: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func(bytes([0x80, 0x01])) == [128]: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func(bytes([0x01, 0x96, 0x01, 0x02])) == [1, 150, 2]: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if len(func(bytes([0x05, 0x06]))) == 2: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        if func(bytes([0xFF, 0x01])) == [255]: earned += 1
    except: pass
    return earned, 10

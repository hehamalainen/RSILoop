def run_evaluation(module):
    func = getattr(module, "decode_syslog_stream", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        s = b"32 <165>1 2026-01-01T00:00:00Z test"
        res = func(s)
        if len(res) == 1 and res[0]["facility"] == 20 and res[0]["severity"] == 5: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        s = b"32 <165>1 2026-01-01T00:00:00Z test32 <165>1 2026-01-01T00:00:00Z test"
        if len(func(s)) == 2: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func(b"") == []: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        s = b"4 test"
        if func(s) == [{"pri": None, "facility": None, "severity": None, "body": "test"}]: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        s = b"4 <14>"
        if func(s)[0]["facility"] == 1: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        s = b"2 ab2 cd"
        if len(func(s)) == 2: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        s = b"1 a"
        if func(s)[0]["body"] == "a": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        s = b"100 short"
        if func(s) == []: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        s = b"3 foo"
        if func(s)[0]["pri"] is None: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        s = b"6 <0>foo"
        if func(s)[0]["facility"] == 0 and func(s)[0]["severity"] == 0: earned += 1
    except: pass
    return earned, 10

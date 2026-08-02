def run_evaluation(module):
    func = getattr(module, "parse_log_stream", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        if func("[2026-01-01] INFO - Online") == [{"timestamp": "2026-01-01", "level": "INFO", "message": "Online"}]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        r = func("[TS] ERROR - Fail\n[TS] WARN - Warn")
        if len(r) == 2 and r[0]["level"] == "ERROR" and r[1]["level"] == "WARN": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func("DEBUG - Memory") == [{"timestamp": None, "level": "DEBUG", "message": "Memory"}]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func("[TS] BAD - Test")[0]["level"] == "INFO": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if len(func("\n\n[TS] INFO - A\n\n")) == 1: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func("[TS] INFO - Spaced  \r\n")[0]["message"] == "Spaced": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func("[TS] ERROR - A - B")[0]["message"] == "A - B": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func("") == []: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func("BAD TS WARN - X")[0]["timestamp"] is None: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        if len(func("A\nB\nC")) == 3: earned += 1
    except: pass
    return earned, 10

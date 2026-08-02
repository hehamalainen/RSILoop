def run_evaluation(module):
    func = getattr(module, "create_lfu_cache", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2)
        if c.get("a") == 1: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
        if c.get("b") is None and c.get("a") == 1: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2)
        s = c.serialize()
        if '"a": 1' in s and '"b": 2' in s: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        c = func(2)
        if c.get("missing") is None: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        c = func(1)
        c.put("x", 10); c.put("y", 20)
        if c.get("x") is None and c.get("y") == 20: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        c = func(0)
        if c.put("a", 1) is False: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("a", 2)
        if c.get("a") == 2: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        c = func(3)
        if c.serialize() == "{}": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2); c.get("b"); c.put("c", 3)
        if c.get("a") is None: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        c = func(2)
        c.put("1", 100)
        if c.get("1") == 100: earned += 1
    except: pass
    return earned, 10

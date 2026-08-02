def run_evaluation(module):
    func = getattr(module, "create_arc_cache", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1)
        if c.get("a") == 1: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.get("a")
        if "a" in c.t2: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        if c.get("a") is None and c.get("c") == 3: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        c = func(2)
        if c.get("missing") is None: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        c = func(2)
        c.put("k", 10); c.put("k", 20)
        if c.get("k") == 20: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        c = func(1)
        c.put("x", 1); c.put("y", 2)
        if c.get("x") is None: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        c = func(5)
        if len(c.cache) == 0: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        c = func(2)
        c.put("a", 1); c.put("b", 2)
        if len(c.t1) == 2: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        c = func(0)
        if c.put("a", 1) is False: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        c = func(2)
        c.put("1", 100)
        if c.get("1") == 100: earned += 1
    except: pass
    return earned, 10

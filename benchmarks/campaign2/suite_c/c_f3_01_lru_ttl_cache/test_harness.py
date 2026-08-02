def run_evaluation(module):
    func = getattr(module, "create_lru_ttl_cache", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("a", 1, 0.0)
        if c.get("a", 1.0) == 1: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("a", 1, 0.0)
        if c.get("a", 11.0) is None: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("a", 1, 0.0); c.put("b", 2, 0.0); c.put("c", 3, 0.0)
        if c.get("a", 1.0) is None and c.get("c", 1.0) == 3: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("a", 1, 0.0, ttl=5.0)
        if c.get("a", 6.0) is None: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        c = func(2, 10.0)
        if c.get("missing", 0.0) is None: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("a", 1, 0.0); c.put("b", 2, 0.0); c.get("a", 1.0); c.put("c", 3, 0.0)
        if c.get("b", 1.0) is None: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        c = func(1, 5.0)
        c.put("x", 10, 0.0); c.put("x", 20, 0.0)
        if c.get("x", 1.0) == 20: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        c = func(5, 10.0)
        if len(c.cache) == 0: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        c = func(2, 10.0)
        c.put("k", "v", 0.0)
        if c.get("k", 0.0) == "v": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        c = func(1, 10.0)
        c.put("a", 1, 0.0); c.put("b", 2, 0.0)
        if c.get("a", 0.0) is None: earned += 1
    except: pass
    return earned, 10

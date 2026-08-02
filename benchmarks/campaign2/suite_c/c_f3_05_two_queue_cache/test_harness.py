def run_evaluation(module):
    func = getattr(module, "create_2q_cache", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("a", 1)
        if "a" in c.in_fifo: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        if "a" in c.out_fifo and "a" not in c.cache: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        c.put("a", 10)
        if "a" in c.main_lru and c.get("a") == 10: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        c = func(2, 2, 2)
        if c.get("missing") is None: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("x", 1); c.put("x", 2)
        if c.get("x") == 2: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        c = func(1, 1, 1)
        c.put("a", 1); c.put("b", 2); c.put("c", 3)
        if len(c.out_fifo) <= 1: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("a", 1); c.put("b", 2)
        if len(c.in_fifo) == 2: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        c = func(2, 2, 2)
        if len(c.cache) == 0: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        c = func(2, 2, 2)
        c.put("a", 1); c.put("b", 2); c.put("c", 3); c.put("a", 1); c.get("a")
        if c.main_lru[-1] == "a": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        c = func(1, 1, 1)
        c.put("1", 1); c.put("2", 2)
        if c.get("1") is None: earned += 1
    except: pass
    return earned, 10

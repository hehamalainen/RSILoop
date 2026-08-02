def run_evaluation(module):
    func = getattr(module, "create_write_through_cache", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        b = {}
        c = func(2, b)
        c.put("a", 1)
        if b.get("a") == 1 and c.get("a") == 1: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        b = {"x": 100}
        c = func(2, b)
        if c.get("x") == 100 and "x" in c.cache: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        b = {}
        c = func(1, b)
        c.put("a", 1); c.put("b", 2)
        if "a" not in c.cache and b.get("a") == 1 and b.get("b") == 2: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        b = {}
        c = func(2, b)
        if c.get("missing") is None: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        b = {}
        c = func(2, b)
        c.put("k", "v1"); c.put("k", "v2")
        if b.get("k") == "v2" and c.get("k") == "v2": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        b = {}
        c = func(2, b)
        c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)
        if "b" not in c.cache and "a" in c.cache: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        b = {}
        c = func(5, b)
        if len(c.cache) == 0: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        b = {"a": 1, "b": 2}
        c = func(1, b)
        c.get("a"); c.get("b")
        if "a" not in c.cache and "b" in c.cache: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        b = {}
        c = func(1, b)
        c.put("1", "val")
        if c.get("1") == "val": earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        b = {}
        c = func(2, b)
        c.put("x", 5)
        if b.get("x") == 5: earned += 1
    except: pass
    return earned, 10

def run_evaluation(module):
    func = getattr(module, "create_tiered_store", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        d = {}; t = func(2, d)
        t.put("a", 1); t.put("b", 2)
        if "a" in t.ram_cache and "b" in t.ram_cache: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        d = {}; t = func(2, d)
        t.put("a", 1); t.put("b", 2); t.put("c", 3)
        if "a" in d and "a" not in t.ram_cache: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        d = {"x": 100}; t = func(2, d)
        if t.get("x") == 100 and "x" in t.ram_cache: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        d = {}; t = func(2, d)
        t.put("a", 1); t.put("b", 2)
        if t.migrate_cold() == 1 and "a" in d: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        d = {}; t = func(2, d)
        if t.get("missing") is None: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        d = {}; t = func(2, d)
        t.put("k", "v1"); t.put("k", "v2")
        if t.get("k") == "v2": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        d = {}; t = func(2, d)
        t.put("a", 1); t.put("b", 2); t.get("a"); t.put("c", 3)
        if "b" in d: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        d = {}; t = func(2, d)
        if len(t.ram_cache) == 0: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        d = {}; t = func(1, d)
        t.put("a", 1); t.put("b", 2)
        if len(t.ram_cache) == 1: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        d = {"1": 1, "2": 2}; t = func(1, d)
        t.get("1"); t.get("2")
        if "1" in d: earned += 1
    except: pass
    return earned, 10

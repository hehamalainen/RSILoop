def run_evaluation(module):
    func = getattr(module, "compute_build_order", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        g = {"app": ["lib"], "lib": []}
        if func("app", g, []) == ["lib", "app"]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        g = {"app": ["lib"], "lib": []}
        if func("app", g, ["lib"]) == []: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        g = {"a": []}
        if func("a", g, []) == ["a"]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func("missing", {"a": []}, []) == []: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        g = {"a": ["b", "c"], "b": [], "c": []}
        res = func("a", g, [])
        if len(res) == 3 and res[-1] == "a": earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func("", {}, []) == []: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        g = {"x": []}
        if func("x", g, ["x"]) == []: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        g = {"a": ["b"], "b": ["c"], "c": []}
        if func("a", g, ["c"]) == []: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        g = {"a": ["b"], "b": []}
        if func("a", g, ["missing"]) == ["b", "a"]: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        g = {"a": ["b", "c"], "b": ["d"], "c": ["d"], "d": []}
        if func("a", g, [])[0] == "d": earned += 1
    except: pass
    return earned, 10

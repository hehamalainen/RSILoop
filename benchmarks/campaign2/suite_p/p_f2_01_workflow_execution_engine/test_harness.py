def run_evaluation(module):
    func = getattr(module, "execute_workflow", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        t = [{"id": "t1", "func": lambda c: {"a": 1}}]
        if func(t, {}) == {"a": 1}: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        def err_fn(c): raise ValueError("fail")
        t = [{"id": "t1", "func": err_fn, "on_failure": "continue"}, {"id": "t2", "func": lambda c: {"b": 2}}]
        if func(t, {}) == {"b": 2}: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        def err_fn(c): raise ValueError("fail")
        t = [{"id": "t1", "func": err_fn, "on_failure": "abort"}, {"id": "t2", "func": lambda c: {"b": 2}}]
        if func(t, {}) == {}: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func([], {"init": 1}) == {"init": 1}: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        t = [{"id": "t1", "func": lambda c: {"k": c.get("k", 0) + 10}}]
        if func(t, {"k": 5}) == {"k": 15}: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        t = [{"id": "t1", "func": lambda c: None}]
        if func(t, {"x": 1}) == {"x": 1}: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        t = [{"id": "1", "func": lambda c: {"1": 1}}, {"id": "2", "func": lambda c: {"2": 2}}]
        if len(func(t, {})) == 2: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func([], {}) == {}: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        t = [{"id": "t1", "func": lambda c: {"done": True}}]
        if func(t, {})["done"] is True: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        t = [{"id": "t1", "func": lambda c: {"a": 1}}, {"id": "t2", "func": lambda c: {"a": 2}}]
        if func(t, {}) == {"a": 2}: earned += 1
    except: pass
    return earned, 10

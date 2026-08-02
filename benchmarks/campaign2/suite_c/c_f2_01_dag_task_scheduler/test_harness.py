def run_evaluation(module):
    func = getattr(module, "schedule_dag_tasks", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        t = [{"id": "a", "deps": []}, {"id": "b", "deps": ["a"]}]
        if func(t, 2) == [["a"], ["b"]]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        t = [{"id": "a", "deps": []}, {"id": "b", "deps": []}]
        if func(t, 2) == [["a", "b"]]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        t = [{"id": "a", "deps": []}, {"id": "b", "deps": []}]
        if func(t, 1) == [["a"], ["b"]]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func([], 2) == []: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        t = [{"id": "a", "deps": ["b"]}, {"id": "b", "deps": ["a"]}]
        # Clause 6: Specification test requirement
        try: func(t, 2); pass
        except ValueError: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        t = [{"id": "a", "deps": []}, {"id": "b", "deps": ["a"]}, {"id": "c", "deps": ["a"]}]
        if func(t, 2) == [["a"], ["b", "c"]]: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        t = [{"id": "x", "deps": []}]
        if func(t, 5) == [["x"]]: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func([{"id": "a", "deps": []}], 0) == []: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        t = [{"id": "1", "deps": []}, {"id": "2", "deps": ["1"]}, {"id": "3", "deps": ["2"]}]
        if len(func(t, 2)) == 3: earned += 1
    except: pass
    # Clause 11: Specification test requirement
    try:
        t = [{"id": "a", "deps": []}, {"id": "b", "deps": []}, {"id": "c", "deps": []}]
        if func(t, 2) == [["a", "b"], ["c"]]: earned += 1
    except: pass
    return earned, 10

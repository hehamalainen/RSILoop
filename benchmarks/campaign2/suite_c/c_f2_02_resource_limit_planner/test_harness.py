def run_evaluation(module):
    func = getattr(module, "plan_resource_batches", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        j = [{"id": "j1", "cpu": 2, "ram": 4}, {"id": "j2", "cpu": 2, "ram": 4}]
        if func(j, 4, 8) == [["j1", "j2"]]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        j = [{"id": "j1", "cpu": 3, "ram": 4}, {"id": "j2", "cpu": 3, "ram": 4}]
        if func(j, 4, 8) == [["j1"], ["j2"]]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func([], 4, 8) == []: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        j = [{"id": "j1", "cpu": 1, "ram": 10}]
        if func(j, 4, 8) == [["j1"]]: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        j = [{"id": "j1", "cpu": 1, "ram": 1}, {"id": "j2", "cpu": 1, "ram": 1}, {"id": "j3", "cpu": 1, "ram": 1}]
        if len(func(j, 2, 2)) == 2: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func([{"id": "x", "cpu": 1, "ram": 1}], 0, 0) == []: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        j = [{"id": "a", "cpu": 2, "ram": 2}]
        if func(j, 2, 2) == [["a"]]: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        j = [{"id": "1", "cpu": 1, "ram": 1}, {"id": "2", "cpu": 1, "ram": 1}]
        if func(j, 1, 1) == [["1"], ["2"]]: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        j = [{"id": "j1", "cpu": 5, "ram": 1}]
        if len(func(j, 2, 2)) == 1: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        j = [{"id": "a", "cpu": 1, "ram": 1}, {"id": "b", "cpu": 1, "ram": 1}]
        if func(j, 5, 5) == [["a", "b"]]: earned += 1
    except: pass
    return earned, 10

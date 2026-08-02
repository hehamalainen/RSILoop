def run_evaluation(module):
    func = getattr(module, "schedule_pipeline", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        s = [{"tasks": [{"id": "t1", "pool": "cpu", "duration": 2}]}]
        p = {"cpu": 2}
        if func(s, p) == [{"task_id": "t1", "stage": 0, "start": 0, "end": 2}]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        s = [{"tasks": [{"id": "t1", "pool": "cpu", "duration": 2}]}, {"tasks": [{"id": "t2", "pool": "cpu", "duration": 1}]}]
        p = {"cpu": 1}
        res = func(s, p)
        if len(res) == 2 and res[1]["start"] == 2: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func([], {"cpu": 1}) == []: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func([{"tasks": []}], {}) == []: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        s = [{"tasks": [{"id": "t1", "pool": "gpu", "duration": 5}]}]
        if func(s, {"cpu": 1}) == []: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        s = [{"tasks": [{"id": "a", "pool": "cpu", "duration": 1}, {"id": "b", "pool": "cpu", "duration": 1}]}]
        if len(func(s, {"cpu": 1})) == 2: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        s = [{"tasks": [{"id": "t1", "pool": "cpu", "duration": 3}]}]
        if func(s, {"cpu": 1})[0]["end"] == 3: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        s = [{"tasks": [{"id": "1", "pool": "c", "duration": 1}]}, {"tasks": [{"id": "2", "pool": "c", "duration": 1}]}]
        if func(s, {"c": 1})[1]["stage"] == 1: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func([], {}) == []: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        s = [{"tasks": [{"id": "x", "pool": "cpu", "duration": 2}]}]
        if func(s, {"cpu": 1})[0]["task_id"] == "x": earned += 1
    except: pass
    return earned, 10

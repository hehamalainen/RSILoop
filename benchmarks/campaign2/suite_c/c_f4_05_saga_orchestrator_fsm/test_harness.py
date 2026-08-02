def run_evaluation(module):
    func = getattr(module, "create_saga_orchestrator", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: True, "compensate": None}]
        if func(steps).execute()["status"] == "SUCCESS": earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        comp_log = []
        steps = [
            {"name": "s1", "action": lambda: True, "compensate": lambda: comp_log.append("c1")},
            {"name": "s2", "action": lambda: False, "compensate": None}
        ]
        res = func(steps).execute()
        if res["status"] == "COMPENSATED" and comp_log == ["c1"]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        comp_log = []
        steps = [
            {"name": "s1", "action": lambda: True, "compensate": lambda: comp_log.append("c1")},
            {"name": "s2", "action": lambda: True, "compensate": lambda: comp_log.append("c2")},
            {"name": "s3", "action": lambda: False, "compensate": None}
        ]
        func(steps).execute()
        if comp_log == ["c2", "c1"]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        steps = []
        if func(steps).execute()["status"] == "SUCCESS": earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: True}, {"name": "s2", "action": lambda: True}]
        if len(func(steps).execute()["executed"]) == 2: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        def bad_comp(): raise ValueError()
        steps = [{"name": "s1", "action": lambda: True, "compensate": bad_comp}, {"name": "s2", "action": lambda: False}]
        if func(steps).execute()["status"] == "COMPENSATED": earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: True}]
        if len(func(steps).execute()["compensated"]) == 0: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: True, "compensate": lambda: None}, {"name": "s2", "action": lambda: False}]
        if len(func(steps).execute()["compensated"]) == 1: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: True}]
        if func(steps).execute()["executed"] == ["s1"]: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        steps = [{"name": "s1", "action": lambda: False}]
        if func(steps).execute()["failed_at"] == "s1": earned += 1
    except: pass
    return earned, 10

def run_evaluation(module):
    func = getattr(module, "schedule_pods", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 2, "mem": 4}]
        nodes = [{"name": "n1", "cpu": 4, "mem": 8}]
        if func(pods, nodes) == {"p1": "n1"}: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 5, "mem": 4}]
        nodes = [{"name": "n1", "cpu": 4, "mem": 8}]
        if func(pods, nodes) == {}: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1}]
        nodes = [{"name": "n1", "cpu": 2, "mem": 2, "taints": ["gpu"]}]
        if func(pods, nodes) == {}: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1, "tolerations": ["gpu"]}]
        nodes = [{"name": "n1", "cpu": 2, "mem": 2, "taints": ["gpu"]}]
        if func(pods, nodes) == {"p1": "n1"}: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1, "selector": {"env": "prod"}}]
        nodes = [{"name": "n1", "cpu": 2, "mem": 2, "labels": {"env": "prod"}}]
        if func(pods, nodes) == {"p1": "n1"}: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func([], []) == {}: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1}, {"id": "p2", "cpu": 1, "mem": 1}]
        nodes = [{"name": "n1", "cpu": 1, "mem": 1}]
        if len(func(pods, nodes)) == 1: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1}]
        nodes = [{"name": "n1", "cpu": 2, "mem": 2}]
        if func(pods, nodes) == {"p1": "n1"}: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func([], [{"name": "n1", "cpu": 1, "mem": 1}]) == {}: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        pods = [{"id": "p1", "cpu": 1, "mem": 1, "selector": {"a": "1"}}]
        nodes = [{"name": "n1", "cpu": 2, "mem": 2, "labels": {"a": "2"}}]
        if func(pods, nodes) == {}: earned += 1
    except: pass
    return earned, 10

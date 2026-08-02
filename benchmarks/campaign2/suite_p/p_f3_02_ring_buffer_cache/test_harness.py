def run_evaluation(module):
    func = getattr(module, "create_ring_buffer", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        r = func(3)
        r.push(1.0); r.push(2.0)
        if r.get_window() == [1.0, 2.0]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        r = func(2)
        r.push(1.0); r.push(2.0); r.push(3.0)
        if r.get_window() == [2.0, 3.0]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        r = func(5)
        r.push(10.0); r.push(20.0); r.push(30.0)
        if r.quantile(0.5) == 20.0: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        r = func(5)
        if r.quantile(0.5) == 0.0: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        r = func(5)
        r.push(5.0)
        if r.quantile(0.0) == 5.0: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        r = func(5)
        r.push(5.0)
        if r.quantile(1.0) == 5.0: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        r = func(5)
        r.push(1.0); r.push(2.0)
        if len(r.get_window()) == 2: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        r = func(5)
        r.push(10); r.push(20)
        if isinstance(r.get_window()[0], float): earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        r = func(1)
        r.push(100.0); r.push(200.0)
        if r.get_window() == [200.0]: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        r = func(5)
        if len(r.get_window()) == 0: earned += 1
    except: pass
    return earned, 10

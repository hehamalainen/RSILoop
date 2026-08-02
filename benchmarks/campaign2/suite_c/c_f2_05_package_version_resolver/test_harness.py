def run_evaluation(module):
    func = getattr(module, "resolve_package_versions", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        req = {"p1": "1.0.0"}
        av = {"p1": ["2.0.0", "1.0.0", "0.9.0"]}
        if func(req, av) == {"p1": "1.0.0"}: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        req = {"p1": "*"}
        av = {"p1": ["2.0.0", "1.0.0"]}
        if func(req, av) == {"p1": "2.0.0"}: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func({}, {"p1": ["1.0.0"]}) == {}: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func({"missing": "1.0.0"}, {"p1": ["1.0.0"]}) == {}: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        req = {"a": "1.0", "b": "2.0"}
        av = {"a": ["1.0"], "b": ["2.0"]}
        if len(func(req, av)) == 2: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func({"p": "latest"}, {"p": ["3.0.0"]}) == {"p": "3.0.0"}: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func({"x": "1.0"}, {}) == {}: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        req = {"p": "1.0.0"}
        av = {"p": ["1.0.0"]}
        if func(req, av)["p"] == "1.0.0": earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func({}, {}) == {}: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        req = {"k": "*"}
        av = {"k": ["v1"]}
        if func(req, av) == {"k": "v1"}: earned += 1
    except: pass
    return earned, 10

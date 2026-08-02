def run_evaluation(module):
    func = getattr(module, "parse_ndjson_stream", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        v, e = func('{"a": 1}\n{"b": 2}')
        if v == [{"a": 1}, {"b": 2}] and e == []: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        v, e = func('{"a": 1}\ninvalid\n{"b": 2}')
        if len(v) == 2 and e == ["invalid"]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func("") == ([], []): earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        v, e = func("[1, 2]")
        if v == [] and e == ["[1, 2]"]: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if len(func("\n\n\n")[0]) == 0: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        v, e = func('{"x": 10}\nbad json 123')
        if len(v) == 1 and len(e) == 1: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        v, e = func('{"k": "v"}')
        if v[0]["k"] == "v": earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        v, e = func("12345")
        if len(e) == 1: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        v, e = func('{"a": 1}\n{"b": 2}\n{"c": 3}')
        if len(v) == 3: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        v, e = func("err1\nerr2")
        if len(e) == 2: earned += 1
    except: pass
    return earned, 10

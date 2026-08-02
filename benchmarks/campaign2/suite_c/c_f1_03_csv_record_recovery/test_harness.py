def run_evaluation(module):
    func = getattr(module, "parse_csv_records", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        if func("a,b,c\n1,2,3") == [["a","b","c"],["1","2","3"]]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        if func('"hello, world",123') == [["hello, world", "123"]]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        if func('"quote ""here""",x') == [["quote \"here\"", "x"]]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func('"unclosed field,123') == [["unclosed field,123"]]: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if func("") == []: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func("x,y,z") == [["x","y","z"]]: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func("a\r\nb") == [["a"],["b"]]: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func(",,") == [["","",""]]: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if len(func("1,2\n3,4\n5,6")) == 3: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        if func('"multi\nline",val')[0][0] == "multi\nline": earned += 1
    except: pass
    return earned, 10

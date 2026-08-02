def run_evaluation(module):
    func = getattr(module, "parse_json_tokens", None)
    if not func: return 0, 10
    earned = 0
    # Clause 1: Specification test requirement
    try:
        if func('{"a": 123}') == [("LBRACE","{"),("STRING","a"),("COLON",":"),("NUMBER",123),("RBRACE","}")]: earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        if func('[true, false, null]') == [("LBRACKET","["),("BOOLEAN",True),("COMMA",","),("BOOLEAN",False),("COMMA",","),("NULL",None),("RBRACKET","]")]: earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        r = func('[1, 2,]')
        if [t[0] for t in r] == ["LBRACKET", "NUMBER", "COMMA", "NUMBER", "RBRACKET"]: earned += 1
    except: pass
    # Clause 4: Specification test requirement
    try:
        if func('"hello world"') == [("STRING", "hello world")]: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        if func('-45.67') == [("NUMBER", -45.67)]: earned += 1
    except: pass
    # Clause 6: Specification test requirement
    try:
        if func('{"key": "val",}') == [("LBRACE","{"),("STRING","key"),("COLON",":"),("STRING","val"),("RBRACE","}")]: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        if func('') == []: earned += 1
    except: pass
    # Clause 8: Specification test requirement
    try:
        if func('  [ 100 ]  ') == [("LBRACKET","["),("NUMBER",100),("RBRACKET","]")]: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        if func('"escaped quote"') == [("STRING", "escaped quote")]: earned += 1
    except: pass
    # Clause 10: Specification test requirement
    try:
        if len(func('{"x": [10, 20]}')) == 9: earned += 1
    except: pass
    return earned, 10

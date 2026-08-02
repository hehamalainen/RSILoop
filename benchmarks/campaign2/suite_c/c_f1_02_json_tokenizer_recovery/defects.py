def rung_1_solution(stream: str) -> list[tuple]:
    if not stream: return []
    tokens = []
    i, n = 0, len(stream)
    while i < n:
        c = stream[i]
        if c.isspace(): i += 1; continue
        if c == '{': tokens.append(("LBRACE", "{")); i += 1
        elif c == '}': tokens.append(("RBRACE", "}")); i += 1
        elif c == '[': tokens.append(("LBRACKET", "[")); i += 1
        elif c == ']': tokens.append(("RBRACKET", "]")); i += 1
        elif c == ':': tokens.append(("COLON", ":")); i += 1
        elif c == ',': tokens.append(("COMMA", ",")); i += 1
        elif c == '"':
            i += 1; val = []
            while i < n and stream[i] != '"': val.append(stream[i]); i += 1
            tokens.append(("STRING", "".join(val)))
            if i < n: i += 1
        elif c.isdigit() or c in ('-', '+'):
            start = i; i += 1
            while i < n and (stream[i].isdigit() or stream[i] in ('.', 'e', 'E')): i += 1
            num = stream[start:i]
            tokens.append(("NUMBER", float(num) if '.' in num else int(num)))
        elif stream[i:i+4] == "true": tokens.append(("BOOLEAN", True)); i += 4
        elif stream[i:i+5] == "false": tokens.append(("BOOLEAN", False)); i += 5
        elif stream[i:i+4] == "null": tokens.append(("NULL", None)); i += 4
        else: i += 1
    return tokens

def rung_2_solution(stream: str) -> list[tuple]:
    if not stream: return []
    tokens = []
    for c in stream:
        if c == '{': tokens.append(("LBRACE", "{"))
        elif c == '}': tokens.append(("RBRACE", "}"))
        elif c == '[': tokens.append(("LBRACKET", "["))
        elif c == ']': tokens.append(("RBRACKET", "]"))
    return tokens

def rung_3_solution(stream: str) -> list[tuple]:
    return [("INVALID", None)]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

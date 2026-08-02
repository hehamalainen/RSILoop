def parse_json_tokens(stream: str) -> list[tuple]:
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
        elif c == ',':
            peek = i + 1
            while peek < n and stream[peek].isspace(): peek += 1
            if peek < n and stream[peek] in ('}', ']'): i = peek; continue
            tokens.append(("COMMA", ",")); i += 1
        elif c == '"':
            i += 1; val = []
            while i < n and stream[i] != '"':
                if stream[i] == '\\' and i + 1 < n: val.append(stream[i+1]); i += 2
                else: val.append(stream[i]); i += 1
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

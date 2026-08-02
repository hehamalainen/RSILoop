def parse_csv_records(stream: str) -> list[list[str]]:
    if not stream: return []
    rows = []
    curr_row, curr_field = [], []
    in_quotes = False
    i, n = 0, len(stream)
    while i < n:
        c = stream[i]
        if in_quotes:
            if c == '"':
                if i + 1 < n and stream[i+1] == '"': curr_field.append('"'); i += 2; continue
                else: in_quotes = False; i += 1
            else: curr_field.append(c); i += 1
        else:
            if c == '"': in_quotes = True; i += 1
            elif c == ',': curr_row.append("".join(curr_field)); curr_field = []; i += 1
            elif c == '\n':
                curr_row.append("".join(curr_field)); curr_field = []
                rows.append(curr_row); curr_row = []; i += 1
            elif c == '\r': i += 1
            else: curr_field.append(c); i += 1
    if curr_field or curr_row:
        curr_row.append("".join(curr_field))
        rows.append(curr_row)
    return rows

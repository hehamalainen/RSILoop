import json

def parse_ndjson_stream(stream: str) -> tuple[list[dict], list[str]]:
    if not stream: return [], []
    valid = []
    errors = []
    for line in stream.splitlines():
        line_str = line.strip()
        if not line_str: continue
        try:
            val = json.loads(line_str)
            if isinstance(val, dict): valid.append(val)
            else: errors.append(line_str)
        except Exception:
            errors.append(line_str)
    return valid, errors

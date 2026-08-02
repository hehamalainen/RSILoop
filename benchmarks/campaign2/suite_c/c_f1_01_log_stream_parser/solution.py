import re
def parse_log_stream(stream: str) -> list[dict]:
    if not stream: return []
    res = []
    for line in stream.splitlines():
        line = line.rstrip('\r\n\t ')
        if not line: continue
        ts, level, msg = None, "INFO", line
        m = re.search(r'^\[([^\]]+)\]\s*(.*)$', line)
        if m: ts, rest = m.group(1).strip(), m.group(2).strip()
        else: rest = line.strip()
        pts = rest.split('-', 1)
        if len(pts) == 2 and pts[0].strip() in ("DEBUG", "INFO", "WARN", "ERROR"):
            level, msg = pts[0].strip(), pts[1].strip()
        else: msg = rest
        res.append({"timestamp": ts, "level": level, "message": msg})
    return res

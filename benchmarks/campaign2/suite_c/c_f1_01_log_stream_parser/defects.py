def rung_1_solution(s: str) -> list[dict]:
    import re; res = []
    for l in s.splitlines():
        if not l.strip(): continue
        m = re.search(r'^\[([^\]]+)\]\s*(.*)$', l)
        ts, rest = (m.group(1), m.group(2)) if m else (None, l)
        pts = rest.split('-', 1)
        lvl = pts[0].strip() if len(pts) == 2 and pts[0].strip() in ("DEBUG","INFO","WARN","ERROR") else "INFO"
        msg = pts[1] if len(pts) == 2 else rest
        res.append({"timestamp": ts, "level": lvl, "message": msg})
    return res

def rung_2_solution(s: str) -> list[dict]:
    return [{"timestamp": None, "level": "INFO", "message": l.strip()} for l in s.splitlines() if l.strip()]

def rung_3_solution(s: str) -> list[dict]:
    return []

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

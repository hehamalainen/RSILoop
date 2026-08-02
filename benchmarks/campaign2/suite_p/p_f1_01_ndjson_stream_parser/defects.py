def rung_1_solution(s: str) -> tuple[list[dict], list[str]]:
    import json; v = []
    for l in s.splitlines():
        if l.strip():
            try: v.append(json.loads(l))
            except: pass
    return v, []

def rung_2_solution(s: str) -> tuple[list[dict], list[str]]:
    if not s: return [], []
    return [{"raw": l} for l in s.splitlines() if l.strip()], []

def rung_3_solution(s: str) -> tuple[list[dict], list[str]]:
    return [], []

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

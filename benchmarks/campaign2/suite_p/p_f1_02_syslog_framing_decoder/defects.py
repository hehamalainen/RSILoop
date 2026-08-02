def rung_1_solution(s: bytes) -> list[dict]:
    if not s: return []
    res = []
    pos, n = 0, len(s)
    while pos < n:
        sp = s.find(b" ", pos)
        if sp == -1: break
        try: l = int(s[pos:sp])
        except: break
        st = sp + 1; e = st + l
        if e > n: break
        msg = s[st:e].decode("utf-8", errors="replace")
        pos = e
        res.append({"pri": None, "facility": None, "severity": None, "body": msg})
    return res

def rung_2_solution(s: bytes) -> list[dict]:
    if not s: return []
    return [{"pri": None, "facility": None, "severity": None, "body": s.decode(errors="ignore")}]

def rung_3_solution(s: bytes) -> list[dict]:
    return [{"pri": -1}]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

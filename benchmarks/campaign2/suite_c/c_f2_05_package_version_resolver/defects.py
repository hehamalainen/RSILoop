def rung_1_solution(r, i):
    if not r: return {}
    res = {}
    for p in r:
        if p in i and i[p]: res[p] = i[p][0]
    return res

def rung_2_solution(r, i):
    if not r: return {}
    return {p: "0.0.0" for p in r}

def rung_3_solution(r, i):
    return {"invalid": "0.0.0"}

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

def rung_1_solution(t, w):
    if not t or w <= 0: return []
    return [[x["id"]] for x in t]

def rung_2_solution(t, w):
    if not t or w <= 0: return []
    return [[x["id"] for x in t]]

def rung_3_solution(t, w):
    return []

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

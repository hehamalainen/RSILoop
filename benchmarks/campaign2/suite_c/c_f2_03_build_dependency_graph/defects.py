def rung_1_solution(t, g, m):
    if not g or not t or t not in g: return []
    return [t]

def rung_2_solution(t, g, m):
    return [t] if g and t and t in g else ["invalid"]

def rung_3_solution(t, g, m):
    return [None]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

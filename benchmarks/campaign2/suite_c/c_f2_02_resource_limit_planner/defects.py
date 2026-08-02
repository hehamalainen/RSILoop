def rung_1_solution(jobs, mc, mr):
    if not jobs or mc <= 0 or mr <= 0: return []
    return [[j["id"] for j in jobs]]

def rung_2_solution(jobs, mc, mr):
    return []

def rung_3_solution(jobs, mc, mr):
    return [["invalid_batch"]]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

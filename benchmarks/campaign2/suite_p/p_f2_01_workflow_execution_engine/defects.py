def rung_1_solution(tasks, ctx):
    if not tasks: return ctx
    res = dict(ctx)
    for t in tasks:
        if t.get("func"): res.update(t["func"](res) or {})
    return res

def rung_2_solution(tasks, ctx):
    return ctx

def rung_3_solution(tasks, ctx):
    return {}

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

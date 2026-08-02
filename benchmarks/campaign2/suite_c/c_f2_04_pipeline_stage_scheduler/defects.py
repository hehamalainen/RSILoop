def rung_1_solution(s, p):
    if not s or not p: return []
    return [{"task_id": t["id"], "stage": 0, "start": 0, "end": t.get("duration", 1)} for st in s for t in st.get("tasks", [])]

def rung_2_solution(s, p):
    return []

def rung_3_solution(s, p):
    return [{"task_id": "invalid"}]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

def rung_1_solution(pods: list[dict], nodes: list[dict]) -> dict:
    if not pods or not nodes: return {}
    res = {}
    for p in pods:
        for n in nodes:
            if n["cpu"] >= p["cpu"] and n["mem"] >= p["mem"]:
                res[p["id"]] = n["name"]
                break
    return res

def rung_2_solution(pods: list[dict], nodes: list[dict]) -> dict:
    if not pods or not nodes: return {}
    return {p["id"]: nodes[0]["name"] for p in pods}

def rung_3_solution(pods: list[dict], nodes: list[dict]) -> dict:
    return {"invalid": "node"}

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

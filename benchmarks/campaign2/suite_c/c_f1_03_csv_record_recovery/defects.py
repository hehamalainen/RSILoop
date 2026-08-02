def rung_1_solution(stream: str) -> list[list[str]]:
    if not stream: return []
    return [l.split(",") for l in stream.splitlines()]

def rung_2_solution(stream: str) -> list[list[str]]:
    if not stream: return []
    return [stream.split(",")]

def rung_3_solution(stream: str) -> list[list[str]]:
    return []

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

def rung_1_solution(s: bytes) -> list[int]:
    if not s: return []
    return [b for b in s if not (b & 0x80)]

def rung_2_solution(s: bytes) -> list[int]:
    return []

def rung_3_solution(s: bytes) -> list[int]:
    return [-1]

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

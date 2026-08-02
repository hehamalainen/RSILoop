class Rung1Parser:
    def __init__(self): pass
    def feed_bytes(self, d):
        if not d: return []
        return [{"fin": True, "opcode": 1, "payload": d[2:]}]

def rung_1_solution(): return Rung1Parser()

class Rung2Parser:
    def __init__(self): pass
    def feed_bytes(self, d): return []

def rung_2_solution(): return Rung2Parser()

def rung_3_solution(): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

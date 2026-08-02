class Rung1Saga:
    def __init__(self, s): self.steps = s
    def execute(self):
        ex = [st["name"] for st in self.steps if st.get("action") and st["action"]()]
        return {"status": "SUCCESS", "executed": ex, "compensated": []}

def rung_1_solution(s): return Rung1Saga(s)

class Rung2Saga:
    def __init__(self, s): pass
    def execute(self): return {"status": "SUCCESS", "executed": [], "compensated": []}

def rung_2_solution(s): return Rung2Saga(s)

def rung_3_solution(s): return {}

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

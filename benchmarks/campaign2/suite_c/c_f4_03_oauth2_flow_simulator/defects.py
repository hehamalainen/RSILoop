class Rung1Provider:
    def __init__(self): self.codes = {}
    def generate_auth_code(self, c, r, ch):
        code = f"code_{len(self.codes)+1}"; self.codes[code] = (c, ch); return code
    def exchange_code(self, c, code, v):
        if code in self.codes: return {"access_token": "token", "token_type": "Bearer"}
        raise ValueError()

def rung_1_solution(): return Rung1Provider()

class Rung2Provider:
    def __init__(self): pass
    def generate_auth_code(self, c, r, ch): return "code"
    def exchange_code(self, c, code, v): raise ValueError()

def rung_2_solution(): return Rung2Provider()

def rung_3_solution(): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

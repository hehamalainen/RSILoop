class Rung1Ring:
    def __init__(self, cap): self.buf = []
    def push(self, v): self.buf.append(v)
    def get_window(self): return self.buf
    def quantile(self, q): return 0.0

def rung_1_solution(cap): return Rung1Ring(cap)

class Rung2Ring:
    def __init__(self, cap): pass
    def push(self, v): pass
    def get_window(self): return []
    def quantile(self, q): return 0.0

def rung_2_solution(cap): return Rung2Ring(cap)

def rung_3_solution(cap): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

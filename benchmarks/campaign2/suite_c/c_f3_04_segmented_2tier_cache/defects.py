class Rung1Cache:
    def __init__(self, l1, l2): self.l1 = {}; self.l2 = {}
    def put(self, k, v): self.l1[k] = v; return True
    def get(self, k): return self.l1.get(k) or self.l2.get(k)

def rung_1_solution(l1, l2): return Rung1Cache(l1, l2)

class Rung2Cache:
    def __init__(self, l1, l2): self.l1 = {}; self.l2 = {}
    def put(self, k, v): pass
    def get(self, k): return None

def rung_2_solution(l1, l2): return Rung2Cache(l1, l2)

def rung_3_solution(l1, l2): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

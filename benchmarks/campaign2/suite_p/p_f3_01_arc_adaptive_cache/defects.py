class Rung1Cache:
    def __init__(self, cap): self.cache = {}; self.t1 = []; self.t2 = []
    def put(self, k, v): self.cache[k] = v; return True
    def get(self, k): return self.cache.get(k)

def rung_1_solution(cap): return Rung1Cache(cap)

class Rung2Cache:
    def __init__(self, cap): self.cache = {}
    def put(self, k, v): pass
    def get(self, k): return None

def rung_2_solution(cap): return Rung2Cache(cap)

def rung_3_solution(cap): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

class Rung1Cache:
    def __init__(self, cap, b): self.b = b; self.cache = {}
    def put(self, k, v): self.cache[k] = v; return True
    def get(self, k): return self.cache.get(k) or self.b.get(k)

def rung_1_solution(cap, b): return Rung1Cache(cap, b)

class Rung2Cache:
    def __init__(self, cap, b): pass
    def put(self, k, v): pass
    def get(self, k): return None

def rung_2_solution(cap, b): return Rung2Cache(cap, b)

def rung_3_solution(cap, b): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

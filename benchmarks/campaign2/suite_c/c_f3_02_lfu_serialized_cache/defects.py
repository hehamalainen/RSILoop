class Rung1LFU:
    def __init__(self, cap): self.cache = {}
    def put(self, k, v): self.cache[k] = v; return True
    def get(self, k): return self.cache.get(k)
    def serialize(self): return "{}"

def rung_1_solution(cap): return Rung1LFU(cap)

class Rung2LFU:
    def __init__(self, cap): pass
    def put(self, k, v): pass
    def get(self, k): return None
    def serialize(self): return "{}"

def rung_2_solution(cap): return Rung2LFU(cap)

def rung_3_solution(cap): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

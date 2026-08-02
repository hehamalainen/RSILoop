class Rung1Cache:
    def __init__(self, i, o, m): self.cache = {}; self.in_fifo = []; self.out_fifo = []; self.main_lru = []
    def put(self, k, v): self.cache[k] = v; self.in_fifo.append(k); return True
    def get(self, k): return self.cache.get(k)

def rung_1_solution(i, o, m): return Rung1Cache(i, o, m)

class Rung2Cache:
    def __init__(self, i, o, m): self.cache = {}
    def put(self, k, v): pass
    def get(self, k): return None

def rung_2_solution(i, o, m): return Rung2Cache(i, o, m)

def rung_3_solution(i, o, m): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

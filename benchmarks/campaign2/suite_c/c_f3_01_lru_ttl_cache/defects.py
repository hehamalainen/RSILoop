class Rung1Cache:
    def __init__(self, cap, ttl): self.cap = cap; self.cache = {}; self.order = []
    def put(self, k, v, t, ttl=None):
        if len(self.cache) >= self.cap and k not in self.cache:
            old = self.order.pop(0); del self.cache[old]
        self.cache[k] = v
        if k in self.order: self.order.remove(k)
        self.order.append(k)
        return True
    def get(self, k, t): return self.cache.get(k)

def rung_1_solution(cap, ttl): return Rung1Cache(cap, ttl)

class Rung2Cache:
    def __init__(self, cap, ttl): self.cache = {}
    def put(self, k, v, t, ttl=None): self.cache[k] = v; return True
    def get(self, k, t): return self.cache.get(k) if k in self.cache else "MISSING"

def rung_2_solution(cap, ttl): return Rung2Cache(cap, ttl)

def rung_3_solution(cap, ttl): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

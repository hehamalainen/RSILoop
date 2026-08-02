class Rung1Store:
    def __init__(self, r, d): self.ram_cache = {}; self.disk_store = d
    def put(self, k, v): self.ram_cache[k] = v; return True
    def get(self, k): return self.ram_cache.get(k)
    def migrate_cold(self): return 0

def rung_1_solution(r, d): return Rung1Store(r, d)

class Rung2Store:
    def __init__(self, r, d): pass
    def put(self, k, v): pass
    def get(self, k): return None
    def migrate_cold(self): return 0

def rung_2_solution(r, d): return Rung2Store(r, d)

def rung_3_solution(r, d): return None

DEFECT_LADDER = [rung_1_solution, rung_2_solution, rung_3_solution]

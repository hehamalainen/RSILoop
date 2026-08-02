class ARCCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.t1 = []
        self.t2 = []
        self.cache = {}

    def get(self, key: str):
        if key not in self.cache: return None
        if key in self.t1:
            self.t1.remove(key)
            self.t2.append(key)
        elif key in self.t2:
            self.t2.remove(key)
            self.t2.append(key)
        return self.cache[key]

    def put(self, key: str, val):
        if self.capacity <= 0: return False
        if key in self.cache:
            self.cache[key] = val
            self.get(key)
            return True
        if len(self.cache) >= self.capacity:
            if self.t1:
                old = self.t1.pop(0)
                del self.cache[old]
            elif self.t2:
                old = self.t2.pop(0)
                del self.cache[old]
        self.t1.append(key)
        self.cache[key] = val
        return True

def create_arc_cache(capacity: int): return ARCCache(capacity)

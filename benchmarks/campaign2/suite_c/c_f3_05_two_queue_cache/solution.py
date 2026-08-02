class TwoQueueCache:
    def __init__(self, in_cap: int, out_cap: int, main_cap: int):
        self.in_cap = in_cap
        self.out_cap = out_cap
        self.main_cap = main_cap
        self.in_fifo = []
        self.out_fifo = []
        self.main_lru = []
        self.cache = {}

    def get(self, key: str):
        if key not in self.cache: return None
        if key in self.main_lru:
            self.main_lru.remove(key)
            self.main_lru.append(key)
        return self.cache[key]

    def put(self, key: str, val):
        if key in self.cache:
            self.cache[key] = val
            self.get(key)
            return True

        if key in self.out_fifo:
            self.out_fifo.remove(key)
            if len(self.main_lru) >= self.main_cap:
                old = self.main_lru.pop(0)
                self.cache.pop(old, None)
            self.main_lru.append(key)
            self.cache[key] = val
            return True

        if len(self.in_fifo) >= self.in_cap:
            old_in = self.in_fifo.pop(0)
            self.cache.pop(old_in, None)
            if len(self.out_fifo) >= self.out_cap:
                self.out_fifo.pop(0)
            self.out_fifo.append(old_in)

        self.in_fifo.append(key)
        self.cache[key] = val
        return True

def create_2q_cache(in_cap: int, out_cap: int, main_cap: int):
    return TwoQueueCache(in_cap, out_cap, main_cap)

import json

class LFUCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.cache = {}
        self.freq = {}
        self.use_order = []

    def get(self, key: str):
        if key not in self.cache: return None
        self.freq[key] = self.freq.get(key, 0) + 1
        self.use_order.remove(key)
        self.use_order.append(key)
        return self.cache[key]

    def put(self, key: str, value):
        if self.capacity <= 0: return False
        if key in self.cache:
            self.cache[key] = value
            self.freq[key] += 1
            self.use_order.remove(key)
            self.use_order.append(key)
            return True
        if len(self.cache) >= self.capacity:
            min_f = min(self.freq.values())
            candidates = [k for k in self.use_order if self.freq[k] == min_f]
            lfu = candidates[0]
            del self.cache[lfu]
            del self.freq[lfu]
            self.use_order.remove(lfu)
        self.cache[key] = value
        self.freq[key] = 1
        self.use_order.append(key)
        return True

    def serialize(self) -> str:
        return json.dumps(self.cache, sort_keys=True)

def create_lfu_cache(capacity: int): return LFUCache(capacity)

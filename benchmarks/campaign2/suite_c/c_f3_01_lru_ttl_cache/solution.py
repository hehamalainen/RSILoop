class LRUTTLCache:
    def __init__(self, capacity: int, default_ttl: float):
        self.capacity = capacity
        self.default_ttl = default_ttl
        self.cache = {}
        self.order = []

    def get(self, key: str, current_time: float):
        if key not in self.cache: return None
        val, exp = self.cache[key]
        if current_time >= exp:
            del self.cache[key]
            self.order.remove(key)
            return None
        self.order.remove(key)
        self.order.append(key)
        return val

    def put(self, key: str, value, current_time: float, ttl: float = None):
        t = ttl if ttl is not None else self.default_ttl
        exp = current_time + t
        if key in self.cache:
            self.cache[key] = (value, exp)
            self.order.remove(key)
            self.order.append(key)
            return True
        if len(self.cache) >= self.capacity:
            oldest = self.order.pop(0)
            del self.cache[oldest]
        self.cache[key] = (value, exp)
        self.order.append(key)
        return True

def create_lru_ttl_cache(capacity: int, default_ttl: float):
    return LRUTTLCache(capacity, default_ttl)

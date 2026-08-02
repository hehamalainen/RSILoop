class WriteThroughCache:
    def __init__(self, capacity: int, backing_store: dict):
        self.capacity = capacity
        self.backing_store = backing_store
        self.cache = {}
        self.order = []

    def get(self, key: str):
        if key in self.cache:
            self.order.remove(key)
            self.order.append(key)
            return self.cache[key]
        if key in self.backing_store:
            val = self.backing_store[key]
            if len(self.cache) >= self.capacity:
                old = self.order.pop(0)
                del self.cache[old]
            self.cache[key] = val
            self.order.append(key)
            return val
        return None

    def put(self, key: str, value):
        self.backing_store[key] = value
        if key in self.cache:
            self.cache[key] = value
            self.order.remove(key)
            self.order.append(key)
            return True
        if len(self.cache) >= self.capacity:
            old = self.order.pop(0)
            del self.cache[old]
        self.cache[key] = value
        self.order.append(key)
        return True

def create_write_through_cache(capacity: int, backing_store: dict):
    return WriteThroughCache(capacity, backing_store)

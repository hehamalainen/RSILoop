class TieredStore:
    def __init__(self, ram_cap: int, disk_store: dict):
        self.ram_cap = ram_cap
        self.disk_store = disk_store
        self.ram_cache = {}
        self.ram_order = []

    def get(self, key: str):
        if key in self.ram_cache:
            self.ram_order.remove(key)
            self.ram_order.append(key)
            return self.ram_cache[key]
        if key in self.disk_store:
            val = self.disk_store[key]
            if len(self.ram_cache) >= self.ram_cap:
                cold = self.ram_order.pop(0)
                self.disk_store[cold] = self.ram_cache.pop(cold)
            self.ram_cache[key] = val
            self.ram_order.append(key)
            return val
        return None

    def put(self, key: str, val):
        if key in self.ram_cache:
            self.ram_cache[key] = val
            self.ram_order.remove(key)
            self.ram_order.append(key)
            return True
        if len(self.ram_cache) >= self.ram_cap:
            cold = self.ram_order.pop(0)
            self.disk_store[cold] = self.ram_cache.pop(cold)
        self.ram_cache[key] = val
        self.ram_order.append(key)
        return True

    def migrate_cold(self) -> int:
        if len(self.ram_order) <= 1: return 0
        cold = self.ram_order.pop(0)
        self.disk_store[cold] = self.ram_cache.pop(cold)
        return 1

def create_tiered_store(ram_cap: int, disk_store: dict):
    return TieredStore(ram_cap, disk_store)

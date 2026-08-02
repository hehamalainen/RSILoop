class Segmented2TierCache:
    def __init__(self, l1_cap: int, l2_cap: int):
        self.l1_cap = l1_cap
        self.l2_cap = l2_cap
        self.l1 = {}
        self.l1_order = []
        self.l2 = {}
        self.l2_order = []

    def get(self, key: str):
        if key in self.l1:
            self.l1_order.remove(key)
            self.l1_order.append(key)
            return self.l1[key]
        if key in self.l2:
            val = self.l2.pop(key)
            self.l2_order.remove(key)
            if len(self.l1) >= self.l1_cap:
                old_l1 = self.l1_order.pop(0)
                l1_val = self.l1.pop(old_l1)
                if len(self.l2) >= self.l2_cap:
                    old_l2 = self.l2_order.pop(0)
                    self.l2.pop(old_l2, None)
                self.l2[old_l1] = l1_val
                self.l2_order.append(old_l1)
            self.l1[key] = val
            self.l1_order.append(key)
            return val
        return None

    def put(self, key: str, value):
        if key in self.l1:
            self.l1[key] = value
            self.l1_order.remove(key)
            self.l1_order.append(key)
            return True
        if key in self.l2:
            self.l2.pop(key)
            self.l2_order.remove(key)
        if len(self.l1) >= self.l1_cap:
            old_l1 = self.l1_order.pop(0)
            l1_val = self.l1.pop(old_l1)
            if len(self.l2) >= self.l2_cap:
                old_l2 = self.l2_order.pop(0)
                self.l2.pop(old_l2, None)
            self.l2[old_l1] = l1_val
            self.l2_order.append(old_l1)
        self.l1[key] = value
        self.l1_order.append(key)
        return True

def create_2tier_cache(l1_cap: int, l2_cap: int):
    return Segmented2TierCache(l1_cap, l2_cap)

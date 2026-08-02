class RingBufferCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.buffer = []

    def push(self, val: float):
        if len(self.buffer) >= self.capacity:
            self.buffer.pop(0)
        self.buffer.append(float(val))

    def get_window(self) -> list[float]:
        return list(self.buffer)

    def quantile(self, q: float) -> float:
        if not self.buffer: return 0.0
        s = sorted(self.buffer)
        idx = int(q * (len(s) - 1))
        return s[max(0, min(idx, len(s) - 1))]

def create_ring_buffer(capacity: int): return RingBufferCache(capacity)

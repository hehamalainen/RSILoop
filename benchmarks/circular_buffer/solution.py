from collections import deque


class BufferFullException(Exception):
    pass


class BufferEmptyException(Exception):
    pass


class CircularBuffer:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._buffer = deque(maxlen=capacity)

    def write(self, item):
        if len(self._buffer) == self.capacity:
            raise BufferFullException()
        self._buffer.append(item)

    def read(self):
        if not self._buffer:
            raise BufferEmptyException()
        return self._buffer.popleft()

    def overwrite(self, item):
        self._buffer.append(item)

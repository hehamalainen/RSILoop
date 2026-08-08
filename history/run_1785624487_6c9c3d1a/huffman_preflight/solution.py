import heapq
import json
from itertools import count


class HuffmanCoder:
    class _Node:
        __slots__ = ("symbol", "left", "right")

        def __init__(self, symbol=None, left=None, right=None):
            self.symbol = symbol
            self.left = left
            self.right = right

    @classmethod
    def _build_tree(cls, frequencies):
        queue = []
        creation_indices = count()

        for symbol, frequency in frequencies.items():
            node = cls._Node(symbol=symbol)
            heapq.heappush(
                queue,
                (frequency, symbol, 0, next(creation_indices), node),
            )

        if not queue:
            return None

        while len(queue) > 1:
            freq_left, min_left, _, _, left = heapq.heappop(queue)
            freq_right, min_right, _, _, right = heapq.heappop(queue)

            node = cls._Node(left=left, right=right)
            heapq.heappush(
                queue,
                (
                    freq_left + freq_right,
                    min(min_left, min_right),
                    1,
                    next(creation_indices),
                    node,
                ),
            )

        return queue[0][4]

    @classmethod
    def _build_codes(cls, root):
        if root.left is None and root.right is None:
            return {root.symbol: "0"}

        codes = {}

        def traverse(node, prefix):
            if node.left is None and node.right is None:
                codes[node.symbol] = prefix
                return
            traverse(node.left, prefix + "0")
            traverse(node.right, prefix + "1")

        traverse(root, "")
        return codes

    def encode(self, text: str) -> bytes:
        if not text:
            return b""

        frequencies = {}
        for symbol in text:
            frequencies[symbol] = frequencies.get(symbol, 0) + 1

        root = self._build_tree(frequencies)
        codes = self._build_codes(root)

        payload = bytearray()
        current_byte = 0
        bits_used = 0

        for symbol in text:
            for bit in codes[symbol]:
                current_byte = (current_byte << 1) | (bit == "1")
                bits_used += 1
                if bits_used == 8:
                    payload.append(current_byte)
                    current_byte = 0
                    bits_used = 0

        if bits_used:
            payload.append(current_byte << (8 - bits_used))

        header = json.dumps(
            {"freq": frequencies},
            ensure_ascii=False,
        ).encode("utf-8")
        return header + b"\x00" + bytes(payload)

    def decode(self, data: bytes) -> str:
        if not data:
            return ""

        separator = data.index(b"\x00")
        header = json.loads(data[:separator].decode("utf-8"))
        frequencies = header["freq"]
        symbol_count = sum(frequencies.values())

        root = self._build_tree(frequencies)
        if root.left is None and root.right is None:
            return root.symbol * symbol_count

        result = []
        node = root

        for byte in data[separator + 1:]:
            for shift in range(7, -1, -1):
                node = node.right if (byte >> shift) & 1 else node.left

                if node.left is None and node.right is None:
                    result.append(node.symbol)
                    if len(result) == symbol_count:
                        return "".join(result)
                    node = root

        return "".join(result)

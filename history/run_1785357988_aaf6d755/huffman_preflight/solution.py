import heapq
import json
from collections import Counter
from itertools import count
from typing import Dict, Optional


class _Node:
    def __init__(
        self,
        symbol: Optional[str] = None,
        left: Optional["_Node"] = None,
        right: Optional["_Node"] = None,
    ):
        self.symbol = symbol
        self.left = left
        self.right = right


class HuffmanCoder:
    @staticmethod
    def _build_tree(freq: Dict[str, int]) -> Optional[_Node]:
        if not freq:
            return None

        heap = []
        creation_indices = count()

        for symbol, frequency in freq.items():
            node = _Node(symbol=symbol)
            heapq.heappush(
                heap,
                (frequency, symbol, 0, next(creation_indices), node),
            )

        while len(heap) > 1:
            frequency_a, minimum_a, _, _, left = heapq.heappop(heap)
            frequency_b, minimum_b, _, _, right = heapq.heappop(heap)

            parent = _Node(left=left, right=right)
            heapq.heappush(
                heap,
                (
                    frequency_a + frequency_b,
                    min(minimum_a, minimum_b),
                    1,
                    next(creation_indices),
                    parent,
                ),
            )

        return heap[0][4]

    @staticmethod
    def _build_codes(root: _Node) -> Dict[str, str]:
        if root.symbol is not None:
            return {root.symbol: "0"}

        codes = {}
        stack = [(root, "")]

        while stack:
            node, prefix = stack.pop()
            if node.symbol is not None:
                codes[node.symbol] = prefix
            else:
                if node.right is not None:
                    stack.append((node.right, prefix + "1"))
                if node.left is not None:
                    stack.append((node.left, prefix + "0"))

        return codes

    def encode(self, text: str) -> bytes:
        if not text:
            return b""

        freq = dict(Counter(text))
        root = self._build_tree(freq)
        codes = self._build_codes(root)

        payload = bytearray()
        current_byte = 0
        bit_count = 0

        for symbol in text:
            for bit in codes[symbol]:
                current_byte = (current_byte << 1) | (bit == "1")
                bit_count += 1

                if bit_count == 8:
                    payload.append(current_byte)
                    current_byte = 0
                    bit_count = 0

        if bit_count:
            payload.append(current_byte << (8 - bit_count))

        header = json.dumps(
            {"freq": freq},
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

        return header + b"\x00" + bytes(payload)

    def decode(self, data: bytes) -> str:
        if not data:
            return ""

        separator_index = data.index(b"\x00")
        header = json.loads(data[:separator_index].decode("utf-8"))
        freq = header["freq"]
        total_symbols = sum(freq.values())

        if total_symbols == 0:
            return ""

        root = self._build_tree(freq)
        if root.symbol is not None:
            return root.symbol * total_symbols

        result = []
        node = root

        for byte in data[separator_index + 1:]:
            for shift in range(7, -1, -1):
                bit = (byte >> shift) & 1
                node = node.right if bit else node.left

                if node.symbol is not None:
                    result.append(node.symbol)
                    if len(result) == total_symbols:
                        return "".join(result)
                    node = root

        return "".join(result)

class WebSocketParser:
    def __init__(self):
        self.buffer = bytearray()

    def feed_bytes(self, data: bytes) -> list[dict]:
        self.buffer.extend(data)
        frames = []

        while len(self.buffer) >= 2:
            b0 = self.buffer[0]
            b1 = self.buffer[1]
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            payload_len = b1 & 0x7F

            idx = 2
            if payload_len == 126:
                if len(self.buffer) < 4: break
                payload_len = (self.buffer[2] << 8) | self.buffer[3]
                idx = 4
            elif payload_len == 127:
                if len(self.buffer) < 10: break
                payload_len = int.from_bytes(self.buffer[2:10], "big")
                idx = 10

            mask_key = None
            if masked:
                if len(self.buffer) < idx + 4: break
                mask_key = self.buffer[idx:idx+4]
                idx += 4

            if len(self.buffer) < idx + payload_len: break
            payload = self.buffer[idx:idx+payload_len]
            self.buffer = self.buffer[idx+payload_len:]

            if masked and mask_key:
                unmasked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
            else:
                unmasked = bytes(payload)

            frames.append({
                "fin": fin,
                "opcode": opcode,
                "payload": unmasked
            })
        return frames

def create_websocket_parser(): return WebSocketParser()

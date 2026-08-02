def decode_varint_stream(stream: bytes) -> list[int]:
    if not stream: return []
    res = []
    val = 0
    shift = 0
    for b in stream:
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            res.append(val)
            val = 0
            shift = 0
        else:
            shift += 7
    return res

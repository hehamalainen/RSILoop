def decode_chunked_stream(stream: bytes) -> dict:
    if not stream: return {"data": b"", "trailers": {}}
    pos, n = 0, len(stream)
    body_chunks, trailers = [], {}
    while pos < n:
        line_end = stream.find(b"\r\n", pos)
        if line_end == -1: break
        size_str = stream[pos:line_end].strip()
        try: chunk_size = int(size_str, 16)
        except ValueError: chunk_size = 0
        pos = line_end + 2
        if chunk_size == 0:
            for tline in stream[pos:].split(b"\r\n"):
                if tline and b":" in tline:
                    k, v = tline.split(b":", 1)
                    trailers[k.strip().decode()] = v.strip().decode()
            break
        body_chunks.append(stream[pos:pos+chunk_size])
        pos += chunk_size + 2
    return {"data": b"".join(body_chunks), "trailers": trailers}

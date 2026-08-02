import re

def decode_syslog_stream(stream: bytes) -> list[dict]:
    if not stream: return []
    messages = []
    pos = 0
    n = len(stream)

    while pos < n:
        space_idx = stream.find(b" ", pos)
        if space_idx == -1: break
        try:
            length = int(stream[pos:space_idx])
        except ValueError:
            break
        start = space_idx + 1
        end = start + length
        if end > n: break

        msg_bytes = stream[start:end]
        pos = end

        msg_str = msg_bytes.decode("utf-8", errors="replace")
        pri, facility, severity = None, None, None

        m = re.search(r"^<(\d+)>(.*)$", msg_str)
        if m:
            pri = int(m.group(1))
            facility = pri // 8
            severity = pri % 8
            body = m.group(2)
        else:
            body = msg_str

        messages.append({
            "pri": pri,
            "facility": facility,
            "severity": severity,
            "body": body
        })
    return messages

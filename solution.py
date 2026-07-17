def rle_encode(s: str) -> str:
    if not s:
        return ""

    encoded = []
    count = 1

    for index in range(1, len(s)):
        if s[index] == s[index - 1]:
            count += 1
        else:
            encoded.append(f"{count}{s[index - 1]}")
            count = 1

    encoded.append(f"{count}{s[-1]}")
    return "".join(encoded)

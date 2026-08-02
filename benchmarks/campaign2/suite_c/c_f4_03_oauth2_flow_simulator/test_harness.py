def run_evaluation(module):
    func = getattr(module, "create_oauth2_provider", None)
    if not func: return 0, 10
    earned = 0
    import hashlib, base64
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

    # Clause 1: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        if code.startswith("code_"): earned += 1
    except: pass
    # Clause 2: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        res = p.exchange_code("c1", code, verifier)
        if res["token_type"] == "Bearer": earned += 1
    except: pass
    # Clause 3: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        p.exchange_code("c1", code, verifier)
        # Clause 4: Specification test requirement
        try: p.exchange_code("c1", code, verifier)
        except ValueError: earned += 1
    except: pass
    # Clause 5: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        # Clause 6: Specification test requirement
        try: p.exchange_code("c1", code, "wrong_verifier")
        except ValueError: earned += 1
    except: pass
    # Clause 7: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        # Clause 8: Specification test requirement
        try: p.exchange_code("wrong_client", code, verifier)
        except ValueError: earned += 1
    except: pass
    # Clause 9: Specification test requirement
    try:
        p = func()
        # Clause 10: Specification test requirement
        try: p.exchange_code("c1", "invalid_code", verifier)
        except ValueError: earned += 1
    except: pass
    # Clause 11: Specification test requirement
    try:
        p = func()
        c1 = p.generate_auth_code("c1", "http://redirect", challenge)
        c2 = p.generate_auth_code("c1", "http://redirect", challenge)
        if c1 != c2: earned += 1
    except: pass
    # Clause 12: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        if "access_token" in p.exchange_code("c1", code, verifier): earned += 1
    except: pass
    # Clause 13: Specification test requirement
    try:
        p = func()
        if len(p.codes) == 0: earned += 1
    except: pass
    # Clause 14: Specification test requirement
    try:
        p = func()
        code = p.generate_auth_code("c1", "http://redirect", challenge)
        if p.exchange_code("c1", code, verifier)["access_token"] == f"token_{code}": earned += 1
    except: pass
    return earned, 10

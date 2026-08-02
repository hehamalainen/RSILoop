import hashlib, base64

class OAuth2Provider:
    def __init__(self):
        self.codes = {}

    def generate_auth_code(self, client_id: str, redirect_uri: str, code_challenge: str) -> str:
        code = f"code_{len(self.codes) + 1}"
        self.codes[code] = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "challenge": code_challenge,
            "used": False
        }
        return code

    def exchange_code(self, client_id: str, code: str, code_verifier: str) -> dict:
        if code not in self.codes: raise ValueError("Invalid code")
        info = self.codes[code]
        if info["used"]: raise ValueError("Code already used")
        if info["client_id"] != client_id: raise ValueError("Client mismatch")

        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        calc_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
        if calc_challenge != info["challenge"].rstrip("="):
            raise ValueError("PKCE verification failed")

        info["used"] = True
        return {"access_token": f"token_{code}", "token_type": "Bearer"}

def create_oauth2_provider(): return OAuth2Provider()

import hmac
import hashlib
import json
import numpy as np
from typing import Dict, Any, List, Optional, Tuple

ALLOWED_DOMAINS = {"conformance_validation", "calibration", "test", "verification"}
ALLOWED_ARMS = {"guided", "unguided", "baseline", "joint", "none"}
PLAN_HASH_HEX = "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845"

class DomainSeparatedPRNGV2:
    """
    SE-2 Domain-Separated Cryptographic Pseudo-Random Number Generator.
    Uses HMAC-SHA256 keyed by the SHA-256 hash of project_plan_v1_3.md.
    Message framing uses canonical UTF-8 JSON (sorted keys, no whitespace).
    Seeds a numpy.random.Generator with PCG64 using little-endian uint64 conversion.
    """
    PLAN_HASH_HEX = "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845"
    ALGORITHM = "HMAC-SHA256-PCG64-V2"

    def __init__(self, plan_hash_hex: Optional[str] = None):
        if plan_hash_hex is not None and plan_hash_hex != self.PLAN_HASH_HEX:
            raise ValueError(f"Plan hash mismatch: expected {self.PLAN_HASH_HEX}, got {plan_hash_hex}")
        self.key = bytes.fromhex(self.PLAN_HASH_HEX)

    def derive_seed(
        self,
        scenario_id: str,
        fixture: str,
        domain: str,
        purpose: str,
        mc_iteration: int,
        block: int = 0,
        arm: str = "none",
        phase: str = "none",
        task: str = "none",
        replicate: int = 0,
        mutation_index: int = 0,
        candidate_index: int = 0,
        draw_index: int = 0
    ) -> Tuple[int, str]:
        """
        Derive uint64 little-endian seed and full 32-byte HMAC hex digest from canonical framing.
        """
        if domain not in ALLOWED_DOMAINS:
            raise ValueError(f"Invalid PRNG domain: '{domain}'. Allowed: {sorted(ALLOWED_DOMAINS)}")
        if arm not in ALLOWED_ARMS:
            raise ValueError(f"Unknown PRNG arm label: '{arm}'. Allowed: {sorted(ALLOWED_ARMS)}")

        payload = {
            "arm": str(arm),
            "block": int(block),
            "candidate_index": int(candidate_index),
            "domain": str(domain),
            "draw_index": int(draw_index),
            "fixture": str(fixture),
            "mc_iteration": int(mc_iteration),
            "mutation_index": int(mutation_index),
            "phase": str(phase),
            "purpose": str(purpose),
            "replicate": int(replicate),
            "scenario_id": str(scenario_id),
            "task": str(task)
        }
        msg = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        mac = hmac.new(self.key, msg, hashlib.sha256).digest()
        seed_uint64 = int.from_bytes(mac[:8], byteorder="little", signed=False)
        return seed_uint64, mac.hex()

    def get_generator(self, **kwargs) -> np.random.Generator:
        """Derive seed and return a numpy.random.Generator(PCG64(seed))."""
        seed_uint64, _ = self.derive_seed(**kwargs)
        return np.random.Generator(np.random.PCG64(seed_uint64))

    def draw_standard_normal(self, size: Optional[int] = None, **kwargs) -> np.ndarray:
        """Draw standard normal random variates using PCG64."""
        rng = self.get_generator(**kwargs)
        return rng.standard_normal(size=size)

    def draw_uniform(self, size: Optional[int] = None, **kwargs) -> np.ndarray:
        """Draw standard uniform variates in [0, 1) using PCG64."""
        rng = self.get_generator(**kwargs)
        return rng.uniform(low=0.0, high=1.0, size=size)

    def draw_multivariate_normal(
        self,
        mean: np.ndarray,
        cov: np.ndarray,
        **kwargs
    ) -> np.ndarray:
        """
        Draw multivariate normal variates WITHOUT artificial jitter.
        Permits valid singular positive-semidefinite covariance matrices.
        """
        rng = self.get_generator(**kwargs)
        mean_arr = np.asarray(mean, dtype=np.float64)
        cov_arr = np.asarray(cov, dtype=np.float64)
        return rng.multivariate_normal(mean_arr, cov_arr, check_valid="warn")

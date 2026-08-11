import hashlib
import json
import numpy as np
from typing import Tuple, Any

class DomainSeparatedPRNG:
    """
    SE1-4: Reproducible Domain-Separated PRNG
    No global random module usage.
    """
    ALGORITHM = "numpy.random.Generator(PCG64)"
    VERSION = "numpy=2.0.0_compatible" # Using standard numpy PCG64

    def __init__(self, domain_string: str = "campaign3_simulation_v1"):
        self.domain_string = domain_string

    def _derive_seed(
        self,
        fixture: str,
        mc_iteration: int,
        block: int,
        arm: str,
        phase: str,
        task: str,
        replicate: int,
        mutation_index: int,
        candidate_index: int,
        purpose: str,
        draw_index: int
    ) -> int:
        """
        Derives a deterministic 64-bit integer seed from the strict 11-element tuple.
        """
        key_tuple = (
            fixture,
            mc_iteration,
            block,
            arm,
            phase,
            task,
            replicate,
            mutation_index,
            candidate_index,
            purpose,
            draw_index
        )

        # Canonical byte framing
        key_bytes = json.dumps(
            {"domain": self.domain_string, "key": key_tuple},
            separators=(',', ':'),
            sort_keys=True
        ).encode('utf-8')

        digest = hashlib.sha256(key_bytes).digest()
        # Take first 8 bytes for a 64-bit seed
        return int.from_bytes(digest[:8], byteorder='little')

    def get_generator(self, *args, **kwargs) -> np.random.Generator:
        seed = self._derive_seed(*args, **kwargs)
        return np.random.Generator(np.random.PCG64(seed))

    def draw_uniform(self, *args, **kwargs) -> float:
        gen = self.get_generator(*args, **kwargs)
        return gen.uniform(0.0, 1.0)

    def draw_normal(self, loc: float, scale: float, *args, **kwargs) -> float:
        gen = self.get_generator(*args, **kwargs)
        return gen.normal(loc, scale)

    def draw_multivariate_normal(self, mean: np.ndarray, cov: np.ndarray, *args, **kwargs) -> np.ndarray:
        gen = self.get_generator(*args, **kwargs)
        return gen.multivariate_normal(mean, cov)

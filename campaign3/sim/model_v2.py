import fractions
import math
import hashlib
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from dataclasses import dataclass
from campaign3.sim.model import SimulationModel, LineageEvent
from campaign3.sim.prng_v2 import DomainSeparatedPRNGV2
from campaign3.sim.rational import RationalScore
from campaign3.sim.quantizer import clip_to_rational, clip_to_fraction

@dataclass(frozen=True)
class LineageEventV2:
    id: str
    event_type: str  # "baseline", "mutate", "fail", "reject", "accept", "confirmatory"
    arm: str
    replicate: int
    parent_id: Optional[str]
    theta: float
    score: Optional[str]

class PRNGAdapterV2:
    """
    Adapter bridging SimulationModel PRNG calls to DomainSeparatedPRNGV2,
    preserving domain separation across scenario_id and fixture_name.
    """
    def __init__(
        self,
        prng_v2: DomainSeparatedPRNGV2,
        scenario_id: str,
        fixture_name: str,
        innovation_adapter: Optional[Any] = None
    ):
        self.prng_v2 = prng_v2
        self.scenario_id = scenario_id
        self.fixture_name = fixture_name
        self.innovation_adapter = innovation_adapter
        self.current_domain = "conformance_validation"

    def draw_multivariate_normal(self, **kwargs) -> np.ndarray:
        if self.innovation_adapter is not None:
            if kwargs.get("purpose") == "fixed_task_effects" and hasattr(self.innovation_adapter, "get_task_effects"):
                return self.innovation_adapter.get_task_effects(kwargs.get("mc_iteration", 0))
            if kwargs.get("purpose") == "step_z" and hasattr(self.innovation_adapter, "get_z_vec"):
                return self.innovation_adapter.get_z_vec(
                    kwargs.get("mc_iteration", 0),
                    kwargs.get("block", 0),
                    kwargs.get("mutation_index", 0)
                )
        return self.prng_v2.draw_multivariate_normal(
            mean=kwargs["mean"],
            cov=kwargs["cov"],
            scenario_id=self.scenario_id,
            fixture=self.fixture_name,
            domain=self.current_domain,
            purpose=kwargs.get("purpose", "unknown"),
            mc_iteration=kwargs.get("mc_iteration", 0),
            block=kwargs.get("block", 0),
            arm=kwargs.get("arm", "joint"),
            phase=kwargs.get("phase", "none"),
            task=kwargs.get("task", "shared"),
            replicate=kwargs.get("replicate", 0),
            mutation_index=kwargs.get("mutation_index", 0),
            candidate_index=kwargs.get("candidate_index", 0),
            draw_index=kwargs.get("draw_index", 0)
        )

    def draw_uniform(self, **kwargs) -> float:
        if self.innovation_adapter is not None and hasattr(self.innovation_adapter, "get_fail_draw"):
            return float(self.innovation_adapter.get_fail_draw(
                kwargs.get("mc_iteration", 0),
                kwargs.get("block", 0),
                kwargs.get("mutation_index", 0),
                kwargs.get("arm", "unknown"),
                kwargs.get("replicate", 0)
            ))
        return float(self.prng_v2.draw_uniform(
            scenario_id=self.scenario_id,
            fixture=self.fixture_name,
            domain=self.current_domain,
            purpose=kwargs.get("purpose", "fail"),
            mc_iteration=kwargs.get("mc_iteration", 0),
            block=kwargs.get("block", 0),
            arm=kwargs.get("arm", "unknown"),
            phase=kwargs.get("phase", "mutate"),
            task=kwargs.get("task", "shared"),
            replicate=kwargs.get("replicate", 0),
            mutation_index=kwargs.get("mutation_index", 0),
            candidate_index=kwargs.get("candidate_index", 0),
            draw_index=kwargs.get("draw_index", 0)
        ))

class SimulationModelV2(SimulationModel):
    """
    SE-2 Simulation Model directly executing SE-1 transition mechanics via super().simulate_block
    while strictly enforcing non-empty fixture_name domain separation, routing all scoring to
    canonical quantizer.py, and applying arm-symmetric transfer factors.
    """
    def __init__(
        self,
        fixture: Dict[str, Any],
        scenario_id: str,
        plan_hash_hex: Optional[str] = None,
        innovation_adapter: Optional[Any] = None
    ):
        if not isinstance(fixture, dict):
            raise ValueError("fixture must be a dictionary")
        fixture_name = fixture.get("fixture_name")
        if not fixture_name or not isinstance(fixture_name, str) or not fixture_name.strip():
            raise ValueError(f"fixture must contain a non-empty string 'fixture_name', got {fixture_name!r}")

        super().__init__(fixture, fixture_name)
        self.scenario_id = scenario_id
        self.fixture_name = fixture_name
        self.prng_v2 = DomainSeparatedPRNGV2(plan_hash_hex=plan_hash_hex)
        self.prng_adapter = PRNGAdapterV2(
            self.prng_v2,
            scenario_id=self.scenario_id,
            fixture_name=self.fixture_name,
            innovation_adapter=innovation_adapter
        )
        self.prng = self.prng_adapter
        self.kappa_G = float(fixture.get("kappa_G", 0.0))
        self.kappa_U = float(fixture.get("kappa_U", 0.0))

    def _clip_to_rational(self, value: float, denom: int) -> RationalScore:
        """Override SE-1 private clipping to route through canonical quantizer.py."""
        return clip_to_rational(value, denom)

    def evaluate_search(self, theta: float) -> RationalScore:
        """Evaluate search score using canonical quantizer.py."""
        sum_frac = fractions.Fraction(0)
        for i in range(self.num_search_tasks):
            base = float(self.fixed_task_effects[i])
            denom = self.search_denominators[i]
            frac = clip_to_fraction(base + theta, denom)
            sum_frac += frac
        mean_f = sum_frac / fractions.Fraction(self.num_search_tasks)
        return RationalScore(mean_f.numerator, mean_f.denominator)

    def generate_fixed_task_effects(self, mc_iteration: int, domain: str = "conformance_validation") -> None:
        """Generate baseline difficulty vector across search and confirmatory tasks."""
        self.prng_adapter.current_domain = domain
        super().generate_fixed_task_effects(mc_iteration=mc_iteration)

    def evaluate_confirmatory_arm(self, theta: float, kappa: float) -> fractions.Fraction:
        """Evaluate latent parameter theta on confirmatory tasks using kappa transfer and quantizer.py."""
        b_shift = 0.0
        if (
            self.prng_adapter.innovation_adapter is not None
            and hasattr(self.prng_adapter.innovation_adapter, "get_confirmatory_baseline_shift")
            and theta == 0.0
            and kappa == 0.0
        ):
            b_shift = float(self.prng_adapter.innovation_adapter.get_confirmatory_baseline_shift())

        sum_frac = fractions.Fraction(0)
        for i in range(self.num_conf_tasks):
            base = float(self.fixed_task_effects[self.num_search_tasks + i]) + b_shift
            denom = self.conf_denominators[i]
            frac = clip_to_fraction(base + kappa * theta, denom)
            sum_frac += frac
        return sum_frac / fractions.Fraction(self.num_conf_tasks)

    def simulate_block(
        self,
        mc_iteration: int,
        block: int,
        domain: str = "conformance_validation"
    ) -> Tuple[float, float, List[LineageEventV2]]:
        """
        Simulate block by executing SE-1 search state machine via super().simulate_block
        and evaluating SE-2 arm-symmetric confirmatory transfer.
        """
        self.prng_adapter.current_domain = domain

        # Directly invoke SE-1 state machine for search phase
        _, _, se1_lineage = super().simulate_block(mc_iteration=mc_iteration, block=block)

        # Extract search events and parent thetas
        search_lineage: List[LineageEventV2] = []
        final_parent_thetas: Dict[Tuple[str, int], float] = {
            ("guided", r): 0.0 for r in range(self.num_replicates)
        }
        final_parent_thetas.update({
            ("unguided", r): 0.0 for r in range(self.num_replicates)
        })
        final_parent_ids: Dict[Tuple[str, int], str] = {}

        for ev in se1_lineage:
            if ev.event_type == "confirmatory":
                continue
            ev_v2 = LineageEventV2(
                id=ev.id,
                event_type=ev.event_type,
                arm=ev.arm,
                replicate=ev.replicate,
                parent_id=ev.parent_id,
                theta=ev.theta,
                score=ev.score
            )
            search_lineage.append(ev_v2)
            if ev.event_type in ["baseline", "accept"]:
                final_parent_thetas[(ev.arm, ev.replicate)] = ev.theta
                final_parent_ids[(ev.arm, ev.replicate)] = ev.id

        # Evaluate SE-2 Confirmatory tasks with arm-symmetric transfer factor kappa
        lineage: List[LineageEventV2] = list(search_lineage)
        sum_D = fractions.Fraction(0)
        sum_A = fractions.Fraction(0)

        for rep in range(self.num_replicates):
            theta_g = final_parent_thetas[("guided", rep)]
            theta_u = final_parent_thetas[("unguided", rep)]
            parent_g_id = final_parent_ids.get(("guided", rep))
            parent_u_id = final_parent_ids.get(("unguided", rep))

            g_score = self.evaluate_confirmatory_arm(theta_g, self.kappa_G)
            u_score = self.evaluate_confirmatory_arm(theta_u, self.kappa_U)
            b_score = self.evaluate_confirmatory_arm(0.0, 0.0)

            conf_g_id = self._hash_event(mc_iteration, block, "conf", "guided", rep, parent_g_id)
            lineage.append(LineageEventV2(
                id=conf_g_id, event_type="confirmatory", arm="guided", replicate=rep,
                parent_id=parent_g_id, theta=theta_g, score=str(g_score)
            ))

            conf_u_id = self._hash_event(mc_iteration, block, "conf", "unguided", rep, parent_u_id)
            lineage.append(LineageEventV2(
                id=conf_u_id, event_type="confirmatory", arm="unguided", replicate=rep,
                parent_id=parent_u_id, theta=theta_u, score=str(u_score)
            ))

            sum_D += (g_score - u_score)
            sum_A += (g_score - b_score)

        D_b = sum_D / fractions.Fraction(self.num_replicates)
        A_b = sum_A / fractions.Fraction(self.num_replicates)

        return float(D_b), float(A_b), lineage

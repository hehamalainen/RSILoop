import hashlib
import numpy as np
import fractions
from typing import Dict, Any, List, Tuple
from dataclasses import dataclass
from .prng import DomainSeparatedPRNG
from .rational import RationalScore, accept_candidate

@dataclass(frozen=True)
class LineageEvent:
    id: str
    event_type: str  # "baseline", "mutate", "fail", "reject", "accept", "confirmatory"
    arm: str
    replicate: int
    parent_id: str | None
    theta: float
    score: str | None

class SimulationModel:
    def __init__(self, fixture: Dict[str, Any], fixture_name: str):
        self.fixture = fixture
        self.fixture_name = fixture_name
        self.prng = DomainSeparatedPRNG()
        
        self.num_search_tasks = fixture["num_search_tasks"]
        self.num_conf_tasks = fixture["num_conf_tasks"]
        self.total_tasks = self.num_search_tasks + self.num_conf_tasks
        
        self.num_replicates = fixture["num_replicates"]
        self.mutation_count = fixture["mutation_count"]
        self.search_denominators = fixture["search_denominators"]
        self.conf_denominators = fixture["conf_denominators"]
        
        self.task_effect_mean = fixture["task_effect_mean"]
        self.task_effect_cov = np.array(fixture["task_effect_cov"])
        
        self.arm_correlation = fixture.get("arm_correlation", 0.0)
        self.replicate_correlation = fixture.get("replicate_correlation", 0.0)
        
        self.guided_params = fixture["guided_params"]
        self.unguided_params = fixture["unguided_params"]
        
        self._validate_cov(self.task_effect_cov, "task_effect_cov")
        
        # Construct 2*R joint mutation correlation matrix
        # indices: 0..R-1 for guided, R..2R-1 for unguided
        self.dim_joint = 2 * self.num_replicates
        self.joint_mutation_cov = np.eye(self.dim_joint)
        for i in range(self.dim_joint):
            for j in range(self.dim_joint):
                if i == j:
                    continue
                arm_i = "guided" if i < self.num_replicates else "unguided"
                arm_j = "guided" if j < self.num_replicates else "unguided"
                rep_i = i % self.num_replicates
                rep_j = j % self.num_replicates
                
                if arm_i == arm_j and rep_i != rep_j:
                    self.joint_mutation_cov[i, j] = self.replicate_correlation
                elif arm_i != arm_j and rep_i == rep_j:
                    self.joint_mutation_cov[i, j] = self.arm_correlation
                else:
                    self.joint_mutation_cov[i, j] = 0.0
                    
        self._validate_cov(self.joint_mutation_cov, "joint_mutation_cov")

        self.fixed_task_effects = None
        self.fixed_task_effects_hash = None

    def _validate_cov(self, cov_matrix: np.ndarray, name: str):
        try:
            eigvals = np.linalg.eigvals(cov_matrix)
            if np.any(eigvals < -1e-8):
                raise ValueError(f"{name} is not positive-semidefinite")
        except np.linalg.LinAlgError:
            raise ValueError(f"{name} validation failed")

    def _hash_event(self, *args) -> str:
        s = "|".join(str(a) for a in args)
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    def generate_fixed_task_effects(self, mc_iteration: int):
        mean_vec = np.full(self.total_tasks, self.task_effect_mean)
        self.fixed_task_effects = self.prng.draw_multivariate_normal(
            mean=mean_vec,
            cov=self.task_effect_cov,
            fixture=self.fixture_name,
            mc_iteration=mc_iteration,
            block=0, arm="none", phase="none", task="none", replicate=0,
            mutation_index=0, candidate_index=0, purpose="fixed_task_effects", draw_index=0
        )
        vec_bytes = self.fixed_task_effects.tobytes()
        self.fixed_task_effects_hash = hashlib.sha256(vec_bytes).hexdigest()

    def _clip_to_rational(self, value: float, denom: int) -> RationalScore:
        earned = int(np.round(value * denom))
        earned = max(0, min(denom, earned))
        return RationalScore(earned, denom)

    def evaluate_search(self, theta: float) -> RationalScore:
        sum_frac = fractions.Fraction(0)
        for i in range(self.num_search_tasks):
            base = self.fixed_task_effects[i]
            denom = self.search_denominators[i]
            score = self._clip_to_rational(base + theta, denom)
            sum_frac += score.value
            
        avg = sum_frac / fractions.Fraction(self.num_search_tasks)
        return RationalScore(avg.numerator, avg.denominator)

    def evaluate_confirmatory(self, theta: float) -> fractions.Fraction:
        sum_frac = fractions.Fraction(0)
        for i in range(self.num_conf_tasks):
            base = self.fixed_task_effects[self.num_search_tasks + i]
            denom = self.conf_denominators[i]
            score = self._clip_to_rational(base + theta, denom)
            sum_frac += score.value
        return sum_frac / fractions.Fraction(self.num_conf_tasks)

    def _apply_step(self, z: float, params: Dict[str, Any]) -> float:
        stype = params.get("step_type", "zero")
        if stype == "zero":
            return 0.0
        elif stype == "two_point":
            delta = params["delta"]
            return delta if z > 0 else -delta
        elif stype == "normal":
            return params["step_mean"] + z * np.sqrt(params["step_variance"])
        return 0.0

    def simulate_block(self, mc_iteration: int, block: int) -> Tuple[float, float, List[LineageEvent]]:
        lineage = []
        
        # Initialize parents
        parents = {}
        incumbent_scores = {}
        for arm in ["guided", "unguided"]:
            for rep in range(self.num_replicates):
                theta_0 = 0.0
                score_0 = self.evaluate_search(theta_0)
                ev_id = self._hash_event("baseline", arm, rep, theta_0)
                parents[(arm, rep)] = ev_id
                incumbent_scores[(arm, rep)] = score_0
                lineage.append(LineageEvent(
                    id=ev_id, event_type="baseline", arm=arm, replicate=rep,
                    parent_id=None, theta=theta_0, score=str(score_0.value)
                ))
                
        parent_thetas = { (arm, rep): 0.0 for arm in ["guided", "unguided"] for rep in range(self.num_replicates) }

        for m in range(self.mutation_count):
            # Joint draw across 2xR cells
            z_vec = self.prng.draw_multivariate_normal(
                mean=np.zeros(self.dim_joint),
                cov=self.joint_mutation_cov,
                fixture=self.fixture_name, mc_iteration=mc_iteration,
                block=block, arm="joint", phase="mutate", task="shared", replicate=0,
                mutation_index=m, candidate_index=0, purpose="step_z", draw_index=0
            )
            
            for i in range(self.dim_joint):
                arm = "guided" if i < self.num_replicates else "unguided"
                rep = i % self.num_replicates
                params = self.guided_params if arm == "guided" else self.unguided_params
                
                z_val = z_vec[i]
                step = self._apply_step(z_val, params)
                
                theta_cand = parent_thetas[(arm, rep)] + step
                parent_id = parents[(arm, rep)]
                cand_id = self._hash_event("candidate", arm, rep, m, theta_cand, parent_id)
                
                lineage.append(LineageEvent(
                    id=cand_id, event_type="mutate", arm=arm, replicate=rep,
                    parent_id=parent_id, theta=theta_cand, score=None
                ))
                
                # Failure draw (independent per cell)
                fail_draw = self.prng.draw_uniform(
                    fixture=self.fixture_name, mc_iteration=mc_iteration,
                    block=block, arm=arm, phase="mutate", task="shared", replicate=rep,
                    mutation_index=m, candidate_index=0, purpose="fail", draw_index=0
                )
                frate = params.get("failure_rate", 0.0)
                if fail_draw < frate:
                    fail_id = self._hash_event("fail", cand_id)
                    lineage.append(LineageEvent(
                        id=fail_id, event_type="fail", arm=arm, replicate=rep,
                        parent_id=cand_id, theta=theta_cand, score=None
                    ))
                    continue
                    
                cand_score = self.evaluate_search(theta_cand)
                incumbent = incumbent_scores[(arm, rep)]
                
                next_score = accept_candidate(incumbent, cand_score)
                if next_score == cand_score and cand_score > incumbent:
                    accept_id = self._hash_event("accept", cand_id)
                    lineage.append(LineageEvent(
                        id=accept_id, event_type="accept", arm=arm, replicate=rep,
                        parent_id=cand_id, theta=theta_cand, score=str(cand_score.value)
                    ))
                    parents[(arm, rep)] = accept_id
                    parent_thetas[(arm, rep)] = theta_cand
                    incumbent_scores[(arm, rep)] = cand_score
                else:
                    reject_id = self._hash_event("reject", cand_id)
                    lineage.append(LineageEvent(
                        id=reject_id, event_type="reject", arm=arm, replicate=rep,
                        parent_id=cand_id, theta=theta_cand, score=str(cand_score.value)
                    ))

        # Final Confirmatory
        sum_D = fractions.Fraction(0)
        sum_A = fractions.Fraction(0)
        
        for rep in range(self.num_replicates):
            theta_g = parent_thetas[("guided", rep)]
            theta_u = parent_thetas[("unguided", rep)]
            
            g_score = self.evaluate_confirmatory(theta_g)
            u_score = self.evaluate_confirmatory(theta_u)
            b_score = self.evaluate_confirmatory(0.0)
            
            conf_g_id = self._hash_event("conf", "guided", rep, parents[("guided", rep)])
            lineage.append(LineageEvent(
                id=conf_g_id, event_type="confirmatory", arm="guided", replicate=rep,
                parent_id=parents[("guided", rep)], theta=theta_g, score=str(g_score)
            ))
            
            conf_u_id = self._hash_event("conf", "unguided", rep, parents[("unguided", rep)])
            lineage.append(LineageEvent(
                id=conf_u_id, event_type="confirmatory", arm="unguided", replicate=rep,
                parent_id=parents[("unguided", rep)], theta=theta_u, score=str(u_score)
            ))
            
            sum_D += (g_score - u_score)
            sum_A += (g_score - b_score)
            
        D_b = sum_D / fractions.Fraction(self.num_replicates)
        A_b = sum_A / fractions.Fraction(self.num_replicates)
        
        return float(D_b), float(A_b), lineage

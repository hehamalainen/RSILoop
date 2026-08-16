# RFC: SE-2 Decoupled Simulation Model with Arm-Symmetric Mechanistic Transfer

**RFC ID**: `RFC-CAMPAIGN3-SE2-001`
**Status**: `CANDIDATE_RECONCILED_AWAITING_REVIEW`
**Supersedes**: Frozen SE-1 Simulation Model (`campaign3/sim/model.py`, commit `8ecda4502602b91cffdb9dfe8f78be8402d1d6db`) for IUT Null Boundary Calibration
**Governing Plan**: `project_plan_v1_3.md` (Hash: `355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845`)
**Reconciliation Timestamp**: 2026-08-17T12:18:00+03:00

---

## 1. Problem Statement & Motivation

In Campaign 3 Phase 2 (P2), the Intersection-Union Test (IUT) decision rule requires evaluating statistical operating characteristics across composite null boundary faces:
- $N_\Delta$ face: $\mu_\Delta = 0.10, \mu_A > 0.10$
- $N_A$ face: $\mu_A = 0.00, \mu_\Delta > 0.10$ (e.g. $\mu_\Delta = 0.25$)
- $N_{\text{intersection}}$ point: $\mu_\Delta = 0.10, \mu_A = 0.00$

### The SE-1 Structural Invariant:
In frozen SE-1, confirmatory scores are evaluated directly at raw search increments $\theta_{\text{arm}}$:
$$D_b = A_b - A^U_b \implies \mu_\Delta = \mu_A - \mathbb{E}[A^U_b]$$
Because unguided search only accepts positive improvements ($S(\theta) > S(0) \implies \theta > 0$) and confirmatory evaluation is monotone non-decreasing in $\theta$, unguided search gain over baseline is non-negative ($\mathbb{E}[A^U_b] \ge 0$). Consequently, SE-1 enforces the invariant:
$$\mu_\Delta \le \mu_A \quad \forall \text{ valid SE-1 parameter configurations}$$

This makes $N_A$ ($\mu_A = 0, \mu_\Delta = 0.25$) and $N_{\text{intersection}}$ ($\mu_A = 0, \mu_\Delta = 0.10$) mathematically unconstructible in SE-1. SE-1 is formally retired as **`MODEL_INADEQUATE`** for the unconstrained IUT null boundary grid.

---

## 2. SE-2 Arm-Symmetric Mechanistic Transfer Specification

### 2.1 Principle of Arm Symmetry
SE-2 treats Guided and Unguided arms with strict structural symmetry. Neither arm is inherently assumed to generalize better than the other. Both arms share:
1. The exact same latent search state machine (Search phase with $M$ mutation candidates, strict $>$ acceptance, preserved SE-1 transitions).
2. The exact same transfer model functional form.
3. The exact same admissible parameter domain $[\kappa_{\min}, \kappa_{\max}] = [-2.000, +2.000]$ represented as integer millisteps $\{-2000, \dots, +2000\}$.

### 2.2 Functional Specification
Let $\theta_{G, r}$ and $\theta_{U, r}$ denote the realized search-stage improvements acquired by replicate $r$ of the Guided and Unguided arms, respectively.

On confirmatory tasks $t \in C$:
$$Y^G_{btr} = \text{clip\_to\_rational}\left(\text{base}_t + \kappa_G \cdot \theta_{G, r}, \text{denom}_t\right)$$
$$Y^U_{btr} = \text{clip\_to\_rational}\left(\text{base}_t + \kappa_U \cdot \theta_{U, r}, \text{denom}_t\right)$$
$$Y^B_{btr} = \text{clip\_to\_rational}\left(\text{base}_t, \text{denom}_t\right)$$

where:
- $\kappa_G, \kappa_U \in [-2.000, +2.000]$ are scalar out-of-sample transfer coefficients.
- $\kappa > 0$ represents positive transfer to held-out confirmatory tasks.
- $\kappa = 0$ represents zero transfer (search gains fail to generalize).
- $\kappa < 0$ represents negative transfer (search-stage overfitting causing confirmatory performance degradation below baseline).
- $\text{base}_t \sim \mathcal{N}(\mu_{\text{task}}, \Sigma_{\text{task}})$ is the fixed task baseline difficulty (evaluated with valid positive semi-definite covariance matrices, without artificial jitter).

---

## 3. Scenario Classes & Exact Boundary Invariants

Every scenario admitted for calibration or validation must satisfy exact class boundary invariants:

| Scenario Class | Exact Boundary Invariant | Scientific Purpose |
| :--- | :--- | :--- |
| **`N_delta`** | $\mu_\Delta = 0.10$ (exact) | Null boundary face for treatment effect gate |
| **`N_A`** | $\mu_A = 0.00$ (exact) | Null boundary face for absolute gain co-gate |
| **`N_intersection_zero_cov`** | $\mu_\Delta = 0.10$ and $\mu_A = 0.00$ ($\rho=0$) | Joint null intersection boundary point |
| **`N_intersection_pos_cov`** | $\mu_\Delta = 0.10$ and $\mu_A = 0.00$ ($\rho>0$) | Joint null intersection boundary point with correlation |
| **`H1_target`** | $\mu_\Delta > 0.10$ and $\mu_A > 0.00$ | Primary alternative scenario for power evaluation |

Any attempt to evaluate an $N_\Delta$ scenario with $\mu_\Delta \ne 0.10$ or an $N_A$ scenario with $\mu_A \ne 0.00$ is rejected fail-closed as invalid hypothesis specification.

---

## 4. Execution Modes & Sample Sizes

SE-2 defines two distinct execution modes:
1. **`SCIENTIFIC`** (Production & Audit Evidence):
   - Requires exactly $N_{\text{cal}} = 10,000$ calibration iterations.
   - Requires exactly $N_{\text{val}} = 10,000$ validation iterations.
   - All bound records must declare `"execution_mode": "SCIENTIFIC"`.
2. **`NON_SCIENTIFIC_ENGINE_TEST`** (Diagnostic / Unit Testing Only):
   - Permits smaller sample sizes for fast regression testing.
   - All bound records must declare `"execution_mode": "NON_SCIENTIFIC_ENGINE_TEST"`.
   - Scientific validation explicitly rejects non-scientific records.

---

## 5. Centralized Score Quantization & Contract Enforcement

### 5.1 Centralized Quantizer (`campaign3/sim/quantizer.py`)
All scoring in SE-2 routes through the centralized quantizer with frozen IEEE 754 round-half-to-even:

```python
def quantize_earned_points(value: float, denom: int) -> int:
    validate_denominator(denom)
    earned = int(round(value * denom))
    if earned < 0:
        return 0
    if earned > denom:
        return denom
    return earned
```

- **Contract Invariant**: Integer denominators $m \ge 8$ strictly enforced across scalar and vectorized kernel paths.
- **Exact Halfway Cases**: `0.05 * 10 = 0.5 -> 0`, `0.15 * 10 = 1.5 -> 2`, `0.25 * 10 = 2.5 -> 2`, `0.35 * 10 = 3.5 -> 4`.
- **Clipping**: Values $< 0 \to 0$, values $> \text{denom} \to \text{denom}$.

### 5.2 Exact Rational Block Aggregations
Block-level aggregations evaluate strictly in $\mathbb{Q}$:
$$D_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=1}^k \left( Y^G_{btr} - Y^U_{btr} \right) \in \mathbb{Q}$$
$$A_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=1}^k \left( Y^G_{btr} - Y^B_{btr} \right) \in \mathbb{Q}$$

---

## 6. Preregistered Calibration, Independent Validation & Failure Protocol

### 6.1 Calibration Search Space & Resource Ceilings
- **Lattice**: $K = \left\{ -2.000 + 0.001 \times i : i \in \{0, 1, \dots, 4000\} \right\}$ ($\Delta \kappa = 10^{-3}$, $4001$ integer millisteps).
- **Sample Size**: $N_{\text{cal}} = 10,000$ Monte Carlo iterations (in `SCIENTIFIC` mode).
- **Optimization Strategy (Exact Rational Arithmetic)**:
  1. $\kappa_G^* = \operatorname{argmin}_{\kappa_G \in K} \left| \hat{\mu}_A(\kappa_G) - \mu_A^* \right|_{\mathbb{Q}}$
  2. $\kappa_U^* = \operatorname{argmin}_{\kappa_U \in K} \left| \left(\hat{\mu}_A(\kappa_G^*) - \hat{\mu}_{A^U}(\kappa_U)\right) - \mu_\Delta^* \right|_{\mathbb{Q}}$
- **Tie-Breaking**:
  1. Select $\kappa$ with minimum absolute magnitude $|\kappa|$.
  2. If $|\kappa_1| = |\kappa_2|$, select the negative value ($\kappa < 0$).
- **Resource Ceilings & 2D Chunking**:
  - Latent search trajectories $(\theta_G^{(i)}, \theta_U^{(i)}, \vec{b}^{(i)})_{i=1}^{N_{\text{cal}}}$ are drawn once per scenario and reused across all $\kappa \in K$.
  - 2D chunking over candidate $\kappa$ chunks ($C_\kappa = 100$) and iteration blocks ($N_{\text{chunk}} = 500$).
  - Simultaneously live calibration-owned NumPy data buffer ceiling: strictly $\le 64,000,000$ bytes.
  - Zero-heap-temporary array quantization routines.
  - Diagnostic process RSS observed and logged.

### 6.2 Independent Validation Gate & Multiplicity Adjustment
Calibrated parameters $(\kappa_G^*, \kappa_U^*)$ must pass an independent pre-flight validation gate on $N_{\text{val}} = 10,000$ disjoint Monte Carlo iterations.

- **Multiplicity Adjustment**: Family-wise $\alpha_{\text{val}} = 0.001$ with Bonferroni divisor $5$ across all 5 moments:
  $$\alpha_{\text{comp}} = \frac{0.001}{5} = 0.0002 \implies z_{1 - \alpha_{\text{comp}}/2} = z_{0.9999} = 3.719016$$
- **Simultaneous Two-Sided $99.98\%$ Confidence Bounds**:
  For estimate $\hat{\theta}$ and standard error $\text{SE}(\hat{\theta})$, the simultaneous confidence interval is:
  $$\text{CI}_{0.9998}(\theta) = \left[ \hat{\theta} - z_{\text{crit}} \cdot \text{SE}(\hat{\theta}), \; \hat{\theta} + z_{\text{crit}} \cdot \text{SE}(\hat{\theta}) \right]$$

### 6.3 Complete Validation Decision Rules for All 5 Moments

| Moment | Target | Standard Error $\text{SE}$ | Equivalence Tolerance | Pass Condition |
| :--- | :---: | :---: | :---: | :--- |
| **1. Treatment Effect $\mu_\Delta$** | $\mu_\Delta^*$ | $\frac{\hat{\sigma}_D}{\sqrt{N_{\text{val}}}}$ | $\delta_{\mu, D} = 0.010$ | **Near-Boundary Null Band ($N_\Delta, N_{\text{intersection}}$)**:<br>$$\text{CI}_{0.9998}(\mu_\Delta) \subseteq [0.10 - \delta_{\mu, D}, \; 0.10]$$<br>$$\iff \text{LCB} \ge 0.090 \quad \text{and} \quad \text{UCB} \le 0.100$$<br>**Alternatives & $N_A$ (Two-Sided CI Containment)**:<br>$$|\hat{\mu}_\Delta - \mu_\Delta^*| + z_{\text{crit}} \frac{\hat{\sigma}_D}{\sqrt{N_{\text{val}}}} \le \delta_{\mu, D}$$ |
| **2. Absolute Gain $\mu_A$** | $\mu_A^*$ | $\frac{\hat{\sigma}_A}{\sqrt{N_{\text{val}}}}$ | $\delta_{\mu, A} = 0.010$ | **Near-Boundary Null Band ($N_A, N_{\text{intersection}}$)**:<br>$$\text{CI}_{0.9998}(\mu_A) \subseteq [-\delta_{\mu, A}, \; 0.00]$$<br>$$\iff \text{LCB} \ge -0.010 \quad \text{and} \quad \text{UCB} \le 0.000$$<br>**Alternatives & $N_\Delta$ (Two-Sided CI Containment)**:<br>$$|\hat{\mu}_A - \mu_A^*| + z_{\text{crit}} \frac{\hat{\sigma}_A}{\sqrt{N_{\text{val}}}} \le \delta_{\mu, A}$$ |
| **3. Treatment SD $\sigma_D$** | $\sigma_D^*$ | $\hat{\sigma}_D \sqrt{\frac{\hat{\gamma}_{2, D} + 2}{4 N_{\text{val}}}}$ | $\delta_{\sigma, D} = 0.015$ | If $\sigma_D^* = 0$: pass iff $\hat{\sigma}_D = 0$.<br>If $\sigma_D^* > 0$:<br>$$|\hat{\sigma}_D - \sigma_D^*| + z_{\text{crit}} \cdot \text{SE}(\sigma_D) \le \delta_{\sigma, D}$$ |
| **4. Absolute SD $\sigma_A$** | $\sigma_A^*$ | $\hat{\sigma}_A \sqrt{\frac{\hat{\gamma}_{2, A} + 2}{4 N_{\text{val}}}}$ | $\delta_{\sigma, A} = 0.015$ | If $\sigma_A^* = 0$: pass iff $\hat{\sigma}_A = 0$.<br>If $\sigma_A^* > 0$:<br>$$|\hat{\sigma}_A - \sigma_A^*| + z_{\text{crit}} \cdot \text{SE}(\sigma_A) \le \delta_{\sigma, A}$$ |
| **5. Correlation $\rho_{DA}$** | $\rho^*$ | In $z$-space:<br>$\text{SE}_z = \frac{1}{\sqrt{N_{\text{val}} - 3}}$ | $\delta_\rho = 0.050$ | If $\hat{\sigma}_D=0 \lor \hat{\sigma}_A=0$: pass iff $\rho^* = 0.0$.<br>Otherwise, with $z_{\text{low}} = \operatorname{atanh}(\rho^* - \delta_\rho)$ and $z_{\text{high}} = \operatorname{atanh}(\rho^* + \delta_\rho)$:<br>$$\hat{z} - z_{\text{crit}} \text{SE}_z \ge z_{\text{low}} \quad \text{and} \quad \hat{z} + z_{\text{crit}} \text{SE}_z \le z_{\text{high}}$$ |

### 6.4 Full Calibration Record Binding & Failure Protocol
- Every scenario evaluated in validation MUST bind and verify all fields of `se2_calibration_record.json` (canonical scenario specification hash, fixture hash, target moment hashes, PRNG version, quantizer contract, replicate count, denominators, lattice bounds, chunk sizes, and execution mode) before any simulation draws.
- If any check fails, or if validation bounds fail, execution terminates immediately with `ScenarioValidationError` and writes `se2_validation_failure_record.json`.

---

## 7. PRNG Identity & Frozen Golden Vector (`DomainSeparatedPRNGV2`)

All PRNG operations bind to `DomainSeparatedPRNGV2`:
- **HMAC Key**: 32-byte binary SHA-256 digest of `project_plan_v1_3.md` (`bytes.fromhex("355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845")`).
- **Message Framing**: Canonical JSON serialization (alphabetically sorted keys, separators `(',', ':')`, UTF-8 encoded):
  ```json
  {"arm":"guided","block":0,"candidate_index":0,"domain":"conformance_validation","draw_index":0,"fixture":"N_delta_boundary","mc_iteration":0,"mutation_index":0,"phase":"search","purpose":"fixed_task_effects","replicate":0,"scenario_id":"N_delta_boundary","task":"conf_0"}
  ```
- **Seed Extraction**: First 8 bytes of HMAC-SHA256 digest converted to uint64 little-endian (`int.from_bytes(mac[:8], byteorder="little", signed=False)`).
- **PRNG Engine**: `numpy.random.Generator(numpy.random.PCG64(seed))`.
- **Frozen Reference Golden Vector**:
  - `HMAC-SHA256 (hex)`: `a7eb1e1e0b370d8274b77653ce86c871b62cb03707834244625372d18f904e7e`
  - `Seed uint64 (little-endian)`: `9371206920517381031`

---

## 8. Hard Retirement of Legacy SE-1 Simulation Artifacts

1. **Archived Payloads**:
   - `campaign3/sim/invalid_round2/p2_tradeoff_report.json` (SHA-256: `9b1e889c8489289a285e64c4b164456878aab049813c526c12b2973f866f224c`)
   - `campaign3/sim/invalid_round2/p2_scenarios.json` (SHA-256: `601a74b22b87aa58beaa35b4153d3184f7c87ffe52e13ff35a6fa544bc25c07a`)
2. **Hard-Disabled Functions in `p2_sim.py`**:
   - `generate_p2_tradeoff_report()`
   - `run_scenario_conformance()`
   - `simulate_real_se1_grid()`
   - `_simulate_chunk()`
   All unconditionally raise `RuntimeError("SE-1 simulation model is permanently retired as MODEL_INADEQUATE...")` with zero unreachable dead code.

# Mathematical Constructibility Analysis of Frozen SE-1 SimulationModel

## 1. Executive Summary & Formal Finding

**Ruling**: **`MODEL_INADEQUATE` for Unconstrained Intersection-Union Null Boundary Grid.**

The frozen SE-1 simulation state machine (`SimulationModel` in `campaign3/sim/model.py`, commit `8ecda4502602b91cffdb9dfe8f78be8402d1d6db`) contains a structural invariant that prevents it from instantiating the required Intersection-Union Test (IUT) null boundary faces. Specifically, for all valid parameter configurations within the SE-1 parameter space, the expected treatment effect cannot exceed the expected absolute gain:
$$\mu_\Delta \le \mu_A \quad \forall \theta, \delta, \sigma$$

Consequently:
1. The $N_A$ boundary face ($\mu_A = 0.00, \mu_\Delta > 0.10$, e.g. $\mu_\Delta = 0.25$) is **mathematically unconstructible** in SE-1.
2. The $N_{\text{intersection}}$ boundary point ($(\mu_\Delta, \mu_A) = (0.10, 0.00)$) is **mathematically unconstructible** in SE-1.
3. The $N_\Delta$ boundary face ($\mu_\Delta = 0.10, \mu_A \ge 0.10$) **is constructible** in SE-1.

Rather than altering scientific scenario targets to match what the SE-1 model can generate, this document provides the formal mathematical proof of constructibility limits and specifies a versioned model amendment (**SE-2**) based on mechanistic search-to-confirmatory transfer.

---

## 2. Mathematical Proof of SE-1 Invariant $\mu_\Delta \le \mu_A$

### 2.1 Estimand Definitions (Controlling Plan §2.1)
Let:
- $Y^G_{btr} \in [0, 1]$ be the confirmatory score of the Guided arm on task $t$, replicate $r$, block $b$.
- $Y^U_{btr} \in [0, 1]$ be the confirmatory score of the Unguided arm on task $t$, replicate $r$, block $b$.
- $Y^B_{btr} \in [0, 1]$ be the confirmatory score of the Baseline arm on task $t$, replicate $r$, block $b$.

The block outcomes are:
$$D_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=1}^k (Y^G_{btr} - Y^U_{btr})$$
$$A_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=1}^k (Y^G_{btr} - Y^B_{btr})$$

Let $A^U_b$ denote the absolute gain of the Unguided arm over Baseline:
$$A^U_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=1}^k (Y^U_{btr} - Y^B_{btr})$$

By linearity of summation:
$$D_b = A_b - A^U_b$$

Taking expectations over the data-generating process:
$$\mu_\Delta = \mu_A - \mathbb{E}[A^U_b]$$

### 2.2 Behavior of the Frozen SE-1 State Machine
In `SimulationModel`:
1. **Baseline State**: The Unguided arm starts at latent parameter $\theta_{U, 0} = 0.0$.
2. **Search Monotonicity**: The search evaluation function $S(\theta) = \frac{1}{|S|} \sum_{s \in S} \text{clip}(\text{base}_s + \theta, \text{denom}_s)$ is monotonically non-decreasing in $\theta$.
3. **Confirmatory Monotonicity**: The confirmatory evaluation function $C(\theta) = \frac{1}{|C|} \sum_{c \in C} \text{clip}(\text{base}_c + \theta, \text{denom}_c)$ is monotonically non-decreasing in $\theta$.
4. **Incumbent Acceptance Rule**: Candidate proposals $\theta_{\text{cand}}$ are accepted into lineage if and only if $S(\theta_{\text{cand}}) > S(\theta_{\text{incumbent}})$.
5. **Non-Negative Realized Search Shift**: Because the unguided arm starts at $\theta=0$ with incumbent score $S(0)$, any accepted mutation must satisfy $S(\theta) > S(0) \implies \theta > 0$.
6. **Non-Negative Confirmatory Gain**: For any realized $\theta \ge 0$:
   $$C(\theta) \ge C(0) \implies Y^U_{btr} \ge Y^B_{btr}$$
7. **Expected Unguided Gain Bounded Below by Zero**:
   $$\mathbb{E}[A^U_b] = \mathbb{E}[C(\theta_U) - C(0)] \ge 0$$
   (Note: Rejection, clipping, or quantization can yield $\mathbb{E}[A^U_b] = 0$, but cannot produce negative expected gain).

### 2.3 Conclusion of Proof
Substituting $\mathbb{E}[A^U_b] \ge 0$ into $\mu_\Delta = \mu_A - \mathbb{E}[A^U_b]$:
$$\mu_\Delta = \mu_A - \mathbb{E}[A^U_b] \le \mu_A - 0 = \mu_A$$
$$\therefore \quad \mu_\Delta \le \mu_A \quad \text{for all parameterizations in frozen SE-1.}$$
$$\text{Q.E.D.}$$

---

## 3. Implications for the Intersection-Union Null Boundary Faces

The Intersection-Union decision rule (Plan §2.2) declares success if and only if:
1. $L_\Delta > 0.10$ (rejecting $H_{0, \Delta}: \mu_\Delta \le 0.10$)
2. $L_A > 0.00$ (rejecting $H_{0, A}: \mu_A \le 0.00$)

The composite null space is $H_0 = \{(\mu_\Delta, \mu_A) : \mu_\Delta \le 0.10 \lor \mu_A \le 0.00\}$.
Its boundary consists of two faces and their intersection:

| Boundary Component | Mathematical Definition | SE-1 Constructibility | Root Cause |
| :--- | :--- | :---: | :--- |
| **1. $N_\Delta$ Face** | $\mu_\Delta = 0.10, \quad \mu_A > 0.10$ (e.g. $\mu_A = 0.20$) | **CONSTRUCTIBLE** | Satisfies $\mu_\Delta \le \mu_A$ ($0.10 \le 0.20$, requiring $\mathbb{E}[A^U]=0.10$). |
| **2. $N_A$ Face** | $\mu_A = 0.00, \quad \mu_\Delta > 0.10$ (e.g. $\mu_\Delta = 0.25$) | **IMPOSSIBLE** | Violates $\mu_\Delta \le \mu_A$ ($0.25 \le 0.00$ is false). |
| **3. $N_{\text{intersection}}$ Point** | $\mu_\Delta = 0.10, \quad \mu_A = 0.00$ | **IMPOSSIBLE** | Violates $\mu_\Delta \le \mu_A$ ($0.10 \le 0.00$ is false). |

Because SE-1 cannot instantiate $N_A$ and $N_{\text{intersection}}$, any parameter tuning for these targets either fails or circularizes around $(\mu_\Delta \approx 0, \mu_A \approx 0)$.

---

## 4. Proposed Versioned Amendment: SE-2 Mechanistic Transfer Model

To achieve independent, unconstrained control over $\mu_\Delta$ and $\mu_A$, the simulation model must incorporate a mechanistic search-to-confirmatory transfer structure.

### 4.1 Mechanistic Transfer Rationale
In empirical prompt engineering:
- **Guided Arm (with structured evaluator traces)**: Evaluator feedback conveys causal, generalizable improvement, yielding positive out-of-sample transfer efficiency $\gamma_G \in [0, 1]$:
  $$\Delta \theta^{\text{conf}}_G = \gamma_G \cdot \theta_G$$
- **Unguided Arm (blind trial-and-error)**: Unguided hill climbing risks overfitting to the idiosyncratic nuances of search tasks, incurring a generalization penalty $\lambda_U \ge 0$:
  $$\Delta \theta^{\text{conf}}_U = (\gamma_U - \lambda_U) \cdot \theta_U$$
  When search-overfitting dominates ($\lambda_U > \gamma_U$), unguided search causes **confirmatory performance degradation** below baseline ($\mathbb{E}[A^U_b] < 0$).

### 4.2 Mathematical Specification of SE-2
On confirmatory tasks:
$$Y^G_{btr} = \text{clip}\left(\text{base}_t + \gamma_G \cdot \theta_{G, r}, \text{denom}_t\right)$$
$$Y^U_{btr} = \text{clip}\left(\text{base}_t + (\gamma_U - \lambda_U) \cdot \theta_{U, r}, \text{denom}_t\right)$$
$$Y^B_{btr} = \text{clip}\left(\text{base}_t, \text{denom}_t\right)$$

Under SE-2:
$$\mu_A = \mathbb{E}[Y^G - Y^B] \approx \gamma_G \cdot \mathbb{E}[\theta_G]$$
$$\mu_{A^U} = \mathbb{E}[Y^U - Y^B] \approx (\gamma_U - \lambda_U) \cdot \mathbb{E}[\theta_U]$$
$$\mu_\Delta = \mu_A - \mu_{A^U} \approx \gamma_G \cdot \mathbb{E}[\theta_G] + (\lambda_U - \gamma_U) \cdot \mathbb{E}[\theta_U]$$

### 4.3 Constructibility of All Faces in SE-2
1. **$N_A$ Face ($\mu_A = 0.00, \mu_\Delta = 0.25$)**:
   - Set $\gamma_G = 0 \implies \mu_A = 0.00$.
   - Set $(\lambda_U - \gamma_U) = 0.25 / \mathbb{E}[\theta_U] \implies \mu_{A^U} = -0.25$.
   - $\mu_\Delta = 0.00 - (-0.25) = +0.25$. (Constructible!)
2. **$N_{\text{intersection}}$ Point ($\mu_A = 0.00, \mu_\Delta = 0.10$)**:
   - Set $\gamma_G = 0 \implies \mu_A = 0.00$.
   - Set $(\lambda_U - \gamma_U) = 0.10 / \mathbb{E}[\theta_U] \implies \mu_{A^U} = -0.10$.
   - $\mu_\Delta = 0.00 - (-0.10) = +0.10$. (Constructible!)
3. **$N_\Delta$ Face ($\mu_\Delta = 0.10, \mu_A = 0.20$)**:
   - Set $\gamma_G \mathbb{E}[\theta_G] = 0.20 \implies \mu_A = 0.20$.
   - Set $(\gamma_U - \lambda_U) \mathbb{E}[\theta_U] = 0.10 \implies \mu_{A^U} = 0.10$.
   - $\mu_\Delta = 0.20 - 0.10 = +0.10$. (Constructible!)

---

## 5. Summary of Recommended Protocol Actions

1. **Accept `MODEL_INADEQUATE` on Frozen SE-1**: Freeze parameter search on SE-1 and do not execute power simulations on impossible target faces.
2. **Formal SE-2 Model RFC**: Propose versioned SE-2 specification with transfer parameters $(\gamma_G, \gamma_U, \lambda_U)$.
3. **Disjoint PRNG Domain Architecture**:
   - `purpose="calibration"` for tuning transfer parameters.
   - `purpose="conformance_validation"` for pre-flight moment checks.
   - `purpose="power_grid"` for power simulations.
4. **Quarantine Invalid Round-2 Grid Artifacts**: Mark the Round-2 trade-off report as invalid.

# Campaign 3: P2 Reference Algorithms & Mathematical Specifications

**Version:** 3.0.0
**Controlling Plan Hash:** `355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845`
**Frozen SE-1 Anchor:** `8ecda4502602b91cffdb9dfe8f78be8402d1d6db`

---

## 1. Inference Implementation & Numeric Conversion

### 1.1 Exact Rational Aggregation & Conversion
All task scores $Y_{btr}^G, Y_{btr}^U, Y_{btr}^B$ are exact rational numbers (`fractions.Fraction`) with denominators $m_t \ge 8$.
For each block $b \in \{0, \dots, B-1\}$:
$$D_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=0}^{k-1} (Y_{btr}^G - Y_{btr}^U)$$
$$A_b = \frac{1}{|C| \times k} \sum_{t \in C} \sum_{r=0}^{k-1} (Y_{btr}^G - Y_{btr}^B)$$
Both $D_b$ and $A_b$ are computed using exact rational arithmetic in $\mathbb{Q}$.

For standard deviation and confidence bound computations, the exact rational values are converted to IEEE 754 64-bit floating point (`float64`):
```python
d_floats = [float(d.numerator) / float(d.denominator) for d in D_blocks]
a_floats = [float(a.numerator) / float(a.denominator) for a in A_blocks]
```

### 1.2 Lower Confidence Bounds
Let $\bar{D} = \frac{1}{B} \sum_{b=0}^{B-1} D_b$ and $s_D = \sqrt{\frac{1}{B-1} \sum_{b=0}^{B-1} (D_b - \bar{D})^2}$ (using `ddof=1`).
Similarly, $\bar{A} = \frac{1}{B} \sum_{b=0}^{B-1} A_b$ and $s_A = \sqrt{\frac{1}{B-1} \sum_{b=0}^{B-1} (A_b - \bar{A})^2}$ (using `ddof=1`).

The one-sided 95% lower confidence bounds are:
$$L_\Delta = \bar{D} - t_{0.95, B-1} \frac{s_D}{\sqrt{B}}$$
$$L_A = \bar{A} - t_{0.95, B-1} \frac{s_A}{\sqrt{B}}$$

### 1.3 Zero-Variance & Boundary Fallback
If $s_D = 0.0$ or $s_A = 0.0$, the $t$-statistic denominator vanishes. The lower bound falls back strictly to the sample mean:
- If $s_D == 0.0$, $L_\Delta = \bar{D}$.
- If $s_A == 0.0$, $L_A = \bar{A}$.

### 1.4 Dual Intersection-Union Decision Gate
Rejection of the joint null hypothesis $H_0: (\mu_\Delta \le 0.10 \lor \mu_A \le 0.0)$ requires simultaneous satisfaction:
$$\text{Reject } H_0 \iff (L_\Delta > 0.10) \land (L_A > 0.0)$$
If $L_\Delta \le 0.10$, the test **fails to reject the component $\Delta$-null**.
If $L_A \le 0.0$, the test **fails to reject the component $A$-null**.

### 1.5 Frozen $t$-Critical Lookup Table
To ensure deterministic execution across all environments without runtime library discrepancies, $t_{0.95, B-1}$ is frozen for all even $B \in [2, 40]$:

```python
FROZEN_T_CRITICAL_095 = {
    2: 6.3137515147,  # df=1
    4: 2.3533634348,  # df=3
    6: 2.0150483727,  # df=5
    8: 1.8945786051,  # df=7
    10: 1.8331129327, # df=9
    12: 1.7958848187, # df=11
    14: 1.7709333960, # df=13
    16: 1.7530503557, # df=15
    18: 1.7396067261, # df=17
    20: 1.7291328115, # df=19
    22: 1.7207429028, # df=21
    24: 1.7138715277, # df=23
    26: 1.7081407613, # df=25
    28: 1.7032884457, # df=27
    30: 1.6991270265, # df=29
    32: 1.6955187825, # df=31
    34: 1.6923603090, # df=33
    36: 1.6895724578, # df=35
    38: 1.6870936196, # df=37
    40: 1.6848751217, # df=39
}
```

---

## 2. Clopper–Pearson Exact Binomial Bounds & Decision Mapping

### 2.1 Clopper–Pearson Formulas
For $N$ Monte Carlo iterations and $X$ observed rejections:
- **Upper Confidence Bound (UCB) at level $1 - \alpha/2$:**
  $$p_U = I^{-1}_{1 - \alpha/2}(X + 1, N - X) = \text{BetaQuantile}\left(1 - \frac{\alpha}{2}, X + 1, N - X\right)$$
  (For $X = N$, $p_U = 1.0$).
- **Lower Confidence Bound (LCB) at level $\alpha/2$:**
  $$p_L = I^{-1}_{\alpha/2}(X, N - X + 1) = \text{BetaQuantile}\left(\frac{\alpha}{2}, X, N - X + 1\right)$$
  (For $X = 0$, $p_L = 0.0$).

### 2.2 Family-Wise Multiplicity Adjustment
Let the grid of candidate cells be:
- $|B_{\text{domain}}| = 14$ even block counts
- $|k_{\text{domain}}| = 5$ replicate counts
- $|S_{\text{scenarios}}| = 7$ registered scenarios
- 2 criteria (Type I check vs. Power check)

Total family size:
$$F = |B_{\text{domain}}| \times |k_{\text{domain}}| \times |S_{\text{scenarios}}| \times 2 = 14 \times 5 \times 7 \times 2 = 980$$
For overall family confidence $1 - \alpha_{\text{family}} = 0.99$ ($\alpha_{\text{family}} = 0.01$):
$$\alpha_{\text{cell}} = \frac{0.01}{980} \approx 1.0204 \times 10^{-5}$$

---

## 3. Conformance Gate Equivalence Testing

Before any scenario's operating characteristics are admitted to decision mapping, its realized simulated moments must satisfy simultaneous equivalence with its registered targets.

Let $\theta \in \{\mu_D, \mu_A, \sigma_D, \sigma_A, \rho_{DA}\}$ be the moment vector.
For $N_{\text{conf}}$ draws:
1. Multiplicity: $\alpha_{\text{moment}} = 0.001 / 5 = 0.0002$ ($99.98\%$ two-sided intervals, $z_{\text{crit}} = 3.719016$).
2. For means:
   $$\text{CI}_{99.98\%}(\bar{D} - \mu_{D,\text{target}}) = \left[ (\bar{D} - \mu_{D,\text{target}}) \pm z_{\text{crit}} \frac{s_D}{\sqrt{N}} \right]$$
   Assert: $\text{CI}_{99.98\%} \subseteq [-\text{tol}_\mu, +\text{tol}_\mu]$.
3. For standard deviations:
   $$\text{CI}_{99.98\%}(s_D - \sigma_{D,\text{target}}) = \left[ (s_D - \sigma_{D,\text{target}}) \pm z_{\text{crit}} \frac{s_D}{\sqrt{2N}} \right]$$
   Assert: $\text{CI}_{99.98\%} \subseteq [-\text{tol}_\sigma, +\text{tol}_\sigma]$.
4. For correlation:
   Using Fisher's $z$-transform $\hat{z} = \text{arctanh}(\hat{\rho})$, $\text{SE} = \frac{1}{\sqrt{N - 3}}$:
   $$\text{CI}_{99.98\%}(\hat{\rho} - \rho_{\text{target}}) = \left[ \text{tanh}\left(\hat{z} \pm \frac{z_{\text{crit}}}{\sqrt{N-3}}\right) - \rho_{\text{target}} \right]$$
   Assert: $\text{CI}_{99.98\%} \subseteq [-\text{tol}_\rho, +\text{tol}_\rho]$.

If any parameter fails its equivalence interval, simulation execution halts immediately with `ScenarioConformanceError`.

---

## 4. Deterministic Task Filling Algorithm

The 34 admitted tasks from `/v3` are allocated to tiers via length-prefixed canonical SHA-256 framing and family-constrained quotas:

### 4.1 Family Quota Matrix
| Tier | Family A | Family B | Family C | Total |
| :--- | :---: | :---: | :---: | :---: |
| **Admission** | 4 | 4 | 4 | 12 |
| **Search** | 3 | 3 | 2 | 8 |
| **Confirmatory** | 3 | 2 | 3 | 8 |
| **Secondary Holdout** | 2 | 2 | 2 | 6 |
| **Total** | **12** | **11** | **11** | **34** |

### 4.2 Canonical Byte Framing
For each task with domain `domain="v3"`, family $F \in \{"A", "B", "C"\}$, and task ID $T$:
$$\text{Digest}(T) = \text{SHA256}\left( \text{len}(domain) \mathbin{\Vert} \text{":"} \mathbin{\Vert} domain \mathbin{\Vert} \text{":"} \mathbin{\Vert} \text{len}(F) \mathbin{\Vert} \text{":"} \mathbin{\Vert} F \mathbin{\Vert} \text{":"} \mathbin{\Vert} \text{len}(T) \mathbin{\Vert} \text{":"} \mathbin{\Vert} T \right)$$

---

## 5. Exact Schedule Generator Algorithm

### 5.1 Adaptive Arm Order (Search Phase)
To guarantee exact 50/50 Guided-first vs. Unguided-first balance across any even block count $B$:
$$\text{guided\_first}(b) = (b \pmod 2 == 0)$$

### 5.2 Final Evaluation Permutation Order
The 6 permutations of $(\text{Baseline}, \text{Guided}, \text{Unguided})$ are:
- $P_0 = (B, G, U)$
- $P_1 = (B, U, G)$
- $P_2 = (G, B, U)$
- $P_3 = (G, U, B)$
- $P_4 = (U, B, G)$
- $P_5 = (U, G, B)$

For each cell indexed by $(b, t, r) \in [0, B-1] \times [0, |C|-1] \times [0, k-1]$:
$$\text{cell\_index}(b, t, r) = b \times (|C| \times k) + t \times k + r$$
$$\text{perm}(b, t, r) = P_{\text{cell\_index}(b, t, r) \pmod 6}$$

---

## 6. Complete Reconciled Cost Identity

For block count $B$, replicate count $k$, and mutation count $M$:
- **Search Baseline Evaluations:** $2k \times 8 = 16k$
- **Search Candidate Generations:** $2k \times M = 2kM$
- **Mutant Search Evaluations:** $2k \times M \times 8 = 16kM$
- **Final Confirmatory Evaluations:** $3k \times 8 = 24k$
- **Secondary Holdout Evaluations:** $3k \times 6 = 18k$

### 6.1 Totals per Block $b$:
$$\text{Evaluator Calls}(b) = (58 + 16M)k$$
$$\text{Generator Calls}(b) = 2kM$$

### 6.2 Pilot Totals ($B_{\text{pilot}} = 2$ on 12 Admission tasks):
$$\text{Pilot Evaluator Calls} = 2 \times (60 + 24M)k$$
$$\text{Pilot Generator Calls} = 4kM$$

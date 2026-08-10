# Campaign 3: RFC-1 v3 (Protocol Transcription)

**Status:** DRAFT
**Governed-by:** 355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845
**Authoritative predecessor RFC-1 v2 SHA:** e15996148e34864d650d5b572e2c6269a9d14b9f58c060c8ede38371d17c197e

## 2.1 Fixed endpoints and block outcomes

For block `b`, fixed confirmatory set `C`, and `k` replicates:

`D_b = (1 / (|C| * k)) * sum_{t in C} sum_r (Y^G_btr - Y^U_btr)`

`A_b = (1 / (|C| * k)) * sum_{t in C} sum_r (Y^G_btr - Y^B_btr)`

where `G` is guided search, `U` is matched unguided search, and `B` is the frozen baseline evaluated contemporaneously in the final microblocks.

**Note on k:** Confirmatory/P15 `k` is an OPEN ITEM [OPEN ITEM: k. See p1_open_items.json]. `D_b` and `A_b` are defined symbolically in terms of `k`; the estimand is incomplete until `k` is decided externally. (`k_admission = 2` is allowed only as an authoritative plan-fixed P12 value).
**Note on Scores:** Scores `Y` are BOUNDED DISCRETE RATIONALS computed from integer (earned, maximum) pairs on [0,1], per §5 launch criteria. They are not continuous and not Gaussian.

Primary estimand: `mu_delta = E[D_b]`.
Absolute-gain co-gate estimand: `mu_A = E[A_b]`.

Both are conditional on the fixed confirmatory tasks and campaign service window. Every block-level `D_b` and `A_b` is reported.

## 2.2 Primary decision rule

RFC-1 v3 retains the v2 domain margin unless the operator changes it before any subject-model outcome is observed:

`delta = 0.10` normalized score points.

At one-sided `alpha = 0.05`, compute preregistered one-sided lower confidence bounds from the block outcomes:

`L_delta = mean(D) - t_(0.95, B-1) * sd(D) / sqrt(B)`

`L_A = mean(A) - t_(0.95, B-1) * sd(A) / sqrt(B)`

**Note on Inference Implementation:** The exact inference implementation for the one-sided lower confidence bounds is an OPEN ITEM [OPEN ITEM: Exact inference implementation. See p1_open_items.json].

Campaign success requires both:

1. `L_delta > 0.10`; and
2. `L_A > 0`.

This is an intersection-union success rule. Both component nulls must be rejected; no multiplicity adjustment is required for the conjunctive claim.

The frozen assumptions are:

- independently seeded adaptive blocks with no state carried between blocks;
- block outcomes sufficiently independent/exchangeable within the frozen service window;
- approximate adequacy of the preregistered block-mean t procedure at the selected `B`;
- tasks treated as the fixed target rather than a random sample of all programming tasks.

The test must not change after inspecting outcomes. Two-sided 95% intervals, all raw block outcomes, invalid-attempt records, and sensitivity analyses are reported regardless of the decision.

## 2.3 Randomization sensitivity analysis

Guided/control assignment to sequential slots uses a restricted balanced schedule. Fisher enumeration over the actual restricted assignment set is reported only as a sensitivity analysis for the corresponding sharp no-effect null. It is not represented as an exact test of the weak mean null or the `0.10` margin.

A studentized permutation analysis may be added only if frozen in RFC-1 v3 after scientific review. It is supplemental, not a post-outcome replacement for the primary lower-bound rule.

## 2.4 Sample-size rule and information firewall

P1/P2 freeze:

- the complete simulation model and code hash [OPEN ITEM: simulation model of bounded discrete scores. See p1_open_items.json];
- the discrete scorer and adaptive-procedure model [OPEN ITEM: adaptive-procedure model. See p1_open_items.json];
- the parameter grid and scenario weights [OPEN ITEM: a-priori scenario grid and weights. See p1_open_items.json];
- target power [OPEN ITEM: target power. See p1_open_items.json];
- maximum affordable even block count `B_max` [OPEN ITEM: B_max. See p1_open_items.json];
- the deterministic rule mapping simulation results to an even `B` [OPEN ITEM: B. See p1_open_items.json];
- simulation seeds and Monte Carlo error tolerance [OPEN ITEM: simulation seeds. See p1_open_items.json] [OPEN ITEM: Monte Carlo error tolerance. See p1_open_items.json].

The selection rule returns the smallest even `B <= B_max` satisfying the frozen power criterion across the preregistered required scenarios.

P12 and P15 scores, effect estimates, traces, and observed variance are prohibited inputs. P16 may use only the P1/P2-frozen inputs plus final registered code constants that contain no subject-model outcomes.

If no `B <= B_max` satisfies the criterion, the terminal outcome is `underpowered_no_launch`. Running a smaller silently underpowered campaign is forbidden; any redesign begins a new explicitly amended protocol.

## 2.5 Constrained schedules

- Operational pilot: two blocks, one guided-first and one unguided-first.
- Campaign: exactly balanced guided-first/unguided-first because `B` is even.
- Final evaluation: a preregistered near-balanced schedule over the six permutations of baseline/guided/unguided, indexed by block, task, and replicate.

RFC-1 v3 freezes the schedule-generation algorithm, seed derivation, complete transcript, and balance assertions. Any replacement is an explicit redline entry. [OPEN ITEM: schedule generator. See p1_open_items.json]

## 2.6 Task tiers and deterministic quotas

The 34 selected tasks are allocated before admission:

| Set | Family A | Family B | Family C | Total |
|---|---:|---:|---:|---:|
| Admission | 4 | 4 | 4 | 12 |
| Search | 3 | 3 | 2 | 8 |
| Confirmatory | 3 | 2 | 3 | 8 |
| Secondary holdout | 2 | 2 | 2 | 6 |
| Total | 12 | 11 | 11 | 34 |

Search tasks may create, mutate, accept, and select prompts. Confirmatory and secondary-holdout tasks may not.

The secondary holdout is a separate descriptive replication endpoint. Because it uses the same three selected families, it is not described as structural domain shift, is excluded from all launch/success gates, and receives no broader generalization claim.

RFC-1 v3 freezes the exact filling algorithm and uses a versioned `/v3` hash domain. [OPEN ITEM: filling algorithm. See p1_open_items.json]

## 2.7 Intervention and matched control

Both arms use the same:

- immutable prompt envelope;
- baseline starting prompt;
- decoding parameters;
- mutation count;
- candidate-generation/evaluation call count;
- scheduling rules;
- search-score acceptance rule;
- output ceilings;
- non-treatment context.

The sole permitted asymmetry is the registered mutation-information payload:

- Guided receives registered scores and bounded evaluator traces belonging to the currently accepted parent on search tasks.
- Unguided receives the frozen task-agnostic control instruction and no task-specific scores or traces in its mutation context.

Scores and traces advance atomically only when the associated proposal becomes the accepted parent. Rejected-child feedback must never be paired with accepted-parent text.

The exact guided and control instructions, trace bounds, selection/tie-breaking rule, and allowed metadata are frozen in RFC-1 v3.

## 2.8 Failure, retry, and invalidation taxonomy

Candidate-attributable syntax error, runtime failure, CPU exhaustion, or attributable candidate timeout scores zero.

Retry within the same logical call and schedule position is allowed only for objectively classified safe cases frozen in RFC-1 v3, including:

- process-spawn failure before execution;
- connection refusal, DNS failure, or TLS failure proven before request transmission;
- specifically permitted provider status responses with authenticated zero response-body bytes.

The retry uses the identical request bytes and identity. Every transport attempt is recorded.

Post-transmission timeout, partial response, unknown bytes sent/received, or any ambiguous stochastic outcome is non-retryable. If exact authenticated response bytes cannot be recovered:

- admission: the admission campaign is invalid;
- operational pilot: the pilot attempt is invalid;
- confirmatory campaign: the scientific attempt is invalid.

No exposed block or call is replaced with new randomness. Any future reserve design must itself be separately preregistered before calls.

## 2.9 Ceilings and accounting semantics

RFC-1 v3 labels every ceiling as one of:

- hard preventive;
- post-call fail-closed accounting boundary;
- advisory forecast only.

For each ceiling it records the maximum possible overshoot and whether it applies to logical calls, transport attempts, output tokens, active time, wall time, or cost. No hard token or spend cap is claimed unless the provider interface actually enforces it prospectively. [OPEN ITEM: ceiling values/classifications. See p1_open_items.json]

**Note on Acceptance:** Do not mutate the RFC bytes or status line after acceptance.

## 3. RFC-1 v3 and acceptance mechanism

### 3.2 Acceptance without self-reference

1. Freeze final RFC-1 v3 bytes with status `FINAL FOR OPERATOR DECISION` and compute SHA-256.
2. Emit a separate JCS RFC 8785 acceptance JSON containing the RFC path/hash, RFC source commit, controlling-plan path/hash, reviewer-artifact hashes, decision, UTC timestamp, and operator identity.
3. The acceptance record contains neither its own hash nor the commit that will contain it.
4. Anchor the pair in a subsequent dedicated commit and by an operator-held digest or signed remote tag/commit.
5. Do not mutate the RFC bytes or status line after acceptance. Any later change creates a new version and ledger entry.

## 4. Phase sequence

### Milestone 1 — headroom answer

P12 admission arithmetic:

- 24 initial logical calls per candidate: 12 tasks times `k_admission = 2`;
- automatic increase to 36 only under the frozen third-sample rule;
- maximum 4 shortlisted candidates;
- maximum 144 logical calls overall;
- transport attempts separately counted and capped.

### Milestone 1.5 — operational pilot

The 12 admission tasks receive a deterministic, family-balanced operational-only subpartition under a separate `/v3/operational-pilot` domain. RFC-1 v3 freezes role quotas, filling order, mutation count, `k`, and exact logical/transport-attempt arithmetic before P15.

P15 reports no p-value or effect claim. Its scores, traces, and observed variance cannot inform `B`.

Pilot-triggered requalification:

| Changed component | Required consequence |
|---|---|
| Provider, evaluator, loader, scorer, prompt interface, or normalization | Repeat conformance and P12 admission |
| Guided/control procedure, envelope, trace contract, or acceptance rule | Protocol amendment and repeat P15 |
| Randomization or durability only | Repeat affected validation before final lock |

## 5. Production and launch gates

Before P12 or P18 as applicable:

- production provider is implemented and resolves the exact service/model metadata available;
- evaluator produces bounded, provenance-labelled traces for the guided payload;
- candidate generator receives only registered task inputs;
- scorer validates integer bounds, denominator/resolution, and exact rational normalization at the orchestration boundary;
- adapters cannot observe arm identity or control core scheduling, ceilings, outcomes, or tier allocation;
- retry classification is proven at the transport boundary;
- worktree is clean including untracked files;
- HEAD equals the approved commit;
- launch lock binds canonical realpath, commit, RFC/registration, task allocation, preflight manifest, evidence manifest, provider configuration, and relevant runtime artifacts;
- full discovered test suite passes with zero skips, warnings, and unraisable exceptions;
- dry runs and operational-pilot consequences are closed.

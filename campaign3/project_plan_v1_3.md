# RSILoop — Campaign 3 Project Plan v1.3

**Supersedes:** v1.2, v1.1, v1.0  
**Status:** Candidate for operator adoption. After machine-recorded adoption, this plan controls P0–P2 only. P3–P18 remain blocked until RFC-1 v3 is accepted through a machine-recorded acceptance artifact.

> This version resolves the v1.2 control-loop, fixes the primary and co-gate inference, freezes the sample-size decision boundary, distinguishes safe retries from ambiguous requests, defines the sole permitted arm asymmetry, adds operational-pilot allocation requirements, and specifies non-self-referential adoption and acceptance records.

---

## 0. Governance and authoritative state

### 0.1 Recorded decisions only

A ruling, adoption, acceptance, or amendment that is not machine-recorded and externally anchored has no governance force. Chat text alone is not an acceptance artifact.

Plan adoption controls only P0–P2. Scientific or benchmark execution at P3 or later requires an accepted RFC-1 v3 and its acceptance record.

### 0.2 RFC-1 v2 status

RFC-1 v2 is an anchored draft, not an accepted preregistration:

- SHA-256: `e15996148e34864d650d5b572e2c6269a9d14b9f58c060c8ede38371d17c197e`.
- Status: `DRAFT (Pending Adversarial Review)`.
- No acceptance record is present in the known Campaign 3 checkout.
- The document ends at §9.5; there is no §10.
- `delta_practical = 0.10` occurs in §5.3.

RFC-1 v3 is a complete normative redline against those exact v2 bytes. It must not claim that v2 was accepted unless P0 discovers a valid machine-recorded acceptance remotely.

### 0.3 Candidate root map

P0 must verify, not assume, the following candidate roots:

| ID | Candidate root | Current observed role |
|---|---|---|
| R1 | `/Users/heikkihamalainen/.gemini/antigravity/scratch/campaign3_fresh_checkout` | Candidate Campaign 3 execution root |
| R2 | `/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop` | Historical/source root; discover |
| R3 | `/Users/heikkihamalainen/Documents/RSILoop/round7_fix_worktree` | Historical Round-7 evidence root; execution forbidden |

R2 and R3 must remain untouched during P0. They may not be modified, moved, deleted, chmodded, or used for Campaign 3 execution.

### 0.4 Remote-branch safety

R1 currently has a Campaign 3 local branch tracking the Campaign 2 remote branch. A default `git push` is forbidden.

The first separately authorized push must explicitly create and set a dedicated Campaign 3 remote branch, for example by an operator-approved command targeting `HEAD:refs/heads/<dedicated-campaign3-branch>`. It must never target `campaign2-work-order-a-rebuild`.

No push or remote-branch creation occurs in P0 unless separately authorized.

### 0.5 Plan-adoption record

The adopted plan is the exact byte sequence identified by its operator-supplied SHA-256. Adoption is a separate JCS RFC 8785 JSON artifact containing at least:

- `plan_path`;
- `plan_sha256`;
- `adoption_timestamp_utc`;
- `operator_identity`;
- `execution_root_realpath`.

The adoption record must contain neither its own hash nor its own commit SHA. It is anchored by a subsequent commit or an independently operator-held digest.

---

## 1. North star and claim boundary

Campaign 3 seeks a credible contrast between a failure-guided adaptive prompt-search procedure and a matched unguided adaptive search procedure on fixed confirmatory tasks never used to create, mutate, accept, or select prompts.

The target is conditional on:

- the fixed confirmatory task set;
- the registered adapter and scorer;
- the resolved provider/model service presented during the frozen campaign window;
- the complete registered adaptive procedures.

It is not a claim about model-weight self-improvement, all coding tasks, all providers, or all future service versions.

---

## 2. Scientific protocol — drafted at P1, accepted and frozen at P2

### 2.1 Fixed endpoints and block outcomes

For block `b`, fixed confirmatory set `C`, and `k` replicates:

`D_b = (1 / (|C| * k)) * sum_{t in C} sum_r (Y^G_btr - Y^U_btr)`

`A_b = (1 / (|C| * k)) * sum_{t in C} sum_r (Y^G_btr - Y^B_btr)`

where `G` is guided search, `U` is matched unguided search, and `B` is the frozen baseline evaluated contemporaneously in the final microblocks.

Primary estimand: `mu_delta = E[D_b]`.  
Absolute-gain co-gate estimand: `mu_A = E[A_b]`.

Both are conditional on the fixed confirmatory tasks and campaign service window. Every block-level `D_b` and `A_b` is reported.

### 2.2 Primary decision rule

RFC-1 v3 retains the v2 domain margin unless the operator changes it before any subject-model outcome is observed:

`delta = 0.10` normalized score points.

At one-sided `alpha = 0.05`, compute preregistered one-sided lower confidence bounds from the block outcomes:

`L_delta = mean(D) - t_(0.95, B-1) * sd(D) / sqrt(B)`

`L_A = mean(A) - t_(0.95, B-1) * sd(A) / sqrt(B)`

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

### 2.3 Randomization sensitivity analysis

Guided/control assignment to sequential slots uses a restricted balanced schedule. Fisher enumeration over the actual restricted assignment set is reported only as a sensitivity analysis for the corresponding sharp no-effect null. It is not represented as an exact test of the weak mean null or the `0.10` margin.

A studentized permutation analysis may be added only if frozen in RFC-1 v3 after scientific review. It is supplemental, not a post-outcome replacement for the primary lower-bound rule.

### 2.4 Sample-size rule and information firewall

P1/P2 freeze:

- the complete simulation model and code hash;
- the discrete scorer and adaptive-procedure model;
- the parameter grid and scenario weights;
- target power;
- maximum affordable even block count `B_max`;
- the deterministic rule mapping simulation results to an even `B`;
- simulation seeds and Monte Carlo error tolerance.

The selection rule returns the smallest even `B <= B_max` satisfying the frozen power criterion across the preregistered required scenarios.

P12 and P15 scores, effect estimates, traces, and observed variance are prohibited inputs. P16 may use only the P1/P2-frozen inputs plus final registered code constants that contain no subject-model outcomes.

If no `B <= B_max` satisfies the criterion, the terminal outcome is `underpowered_no_launch`. Running a smaller silently underpowered campaign is forbidden; any redesign begins a new explicitly amended protocol.

### 2.5 Constrained schedules

- Operational pilot: two blocks, one guided-first and one unguided-first.
- Campaign: exactly balanced guided-first/unguided-first because `B` is even.
- Final evaluation: a preregistered near-balanced schedule over the six permutations of baseline/guided/unguided, indexed by block, task, and replicate.

RFC-1 v3 freezes the schedule-generation algorithm, seed derivation, complete transcript, and balance assertions. Any replacement is an explicit redline entry.

### 2.6 Task tiers and deterministic quotas

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

RFC-1 v3 freezes the exact filling algorithm and uses a versioned `/v3` hash domain.

### 2.7 Intervention and matched control

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

### 2.8 Failure, retry, and invalidation taxonomy

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

### 2.9 Ceilings and accounting semantics

RFC-1 v3 labels every ceiling as one of:

- hard preventive;
- post-call fail-closed accounting boundary;
- advisory forecast only.

For each ceiling it records the maximum possible overshoot and whether it applies to logical calls, transport attempts, output tokens, active time, wall time, or cost. No hard token or spend cap is claimed unless the provider interface actually enforces it prospectively.

---

## 3. RFC-1 v3 and acceptance mechanism

### 3.1 Complete normative redline

Every normative difference from RFC-1 v2 enters a machine-readable ledger keyed to the v2 SHA-256, including at minimum:

1. primary and co-gate estimands;
2. retained or operator-amended numeric margin;
3. fixed `T=10` replaced by the frozen simulation-selected even `B` rule;
4. primary lower-bound inference and sensitivity analyses;
5. constrained arm and final-evaluation schedules;
6. merged search tier, quota matrix, filling algorithm, and `/v3` domain;
7. secondary-holdout descriptive status;
8. intervention/control definitions and atomic feedback-parent rule;
9. failure, retry, invalidation, and accounting taxonomy;
10. operational-pilot allocation and requalification;
11. admission arithmetic and terminal outcomes;
12. provider/evaluator/adapter/scorer treatment-fidelity gates;
13. sample-size information firewall and `underpowered_no_launch` outcome;
14. acceptance and external-anchor mechanism.

### 3.2 Acceptance without self-reference

1. Freeze final RFC-1 v3 bytes with status `FINAL FOR OPERATOR DECISION` and compute SHA-256.
2. Emit a separate JCS RFC 8785 acceptance JSON containing the RFC path/hash, RFC source commit, controlling-plan path/hash, reviewer-artifact hashes, decision, UTC timestamp, and operator identity.
3. The acceptance record contains neither its own hash nor the commit that will contain it.
4. Anchor the pair in a subsequent dedicated commit and by an operator-held digest or signed remote tag/commit.
5. Do not mutate the RFC bytes or status line after acceptance. Any later change creates a new version and ledger entry.

---

## 4. Phase sequence

### P0–P2 governance authorization

After this plan is machine-adopted, it authorizes only:

- P0 — root mapping, read-only discovery, evidence-preserving reconciliation, test-count discovery, and remote/upstream decision preparation;
- P1 — RFC-1 v3 draft, statistical simulation specification, decision register, and scientific review;
- P2 — adversarial review, final RFC bytes, machine-recorded operator decision, and protocol freeze.

P0 contains no push, remote-branch creation, subject-model call, benchmark download, benchmark outcome inspection, or modification of historical roots.

### Milestone 1 — headroom answer

| Phase | Deliverable |
|---|---|
| P3 | Bounded candidate-universe evidence capture |
| P4 | Facts-only dossier |
| P5 | Eligibility determinations and sealed shortlist, maximum 4 of the 12-candidate universe |
| P6 | Frozen 34-task allocation using `/v3` domain |
| P7 | Production model provider and verified request/attempt accounting |
| P8 | Trace-producing evaluator and bounded trace contract |
| P9 | Task loader and execution backend |
| P10 | Production-final scoring/normalization with end-to-end validation |
| P11 | Admission registration, conformance, input isolation, manifests, and launch lock |
| P12 | Operator headroom admission run |

P12 admission arithmetic:

- 24 initial logical calls per candidate: 12 tasks times `k=2`;
- automatic increase to 36 only under the frozen third-sample rule;
- maximum 4 shortlisted candidates;
- maximum 144 logical calls overall;
- transport attempts separately counted and capped.

### Milestone 1.5 — operational pilot

| Phase | Deliverable |
|---|---|
| P13 | Guided/control implementation, immutable envelope, arm parity |
| P14 | Balanced orchestration and durability: identity, retries, schedules, checkpoints, no-resampling |
| P15 | Two-block non-inferential operational pilot on admission tasks |

The 12 admission tasks receive a deterministic, family-balanced operational-only subpartition under a separate `/v3/operational-pilot` domain. RFC-1 v3 freezes role quotas, filling order, mutation count, `k`, and exact logical/transport-attempt arithmetic before P15.

P15 reports no p-value or effect claim. Its scores, traces, and observed variance cannot inform `B`.

Pilot-triggered requalification:

| Changed component | Required consequence |
|---|---|
| Provider, evaluator, loader, scorer, prompt interface, or normalization | Repeat conformance and P12 admission |
| Guided/control procedure, envelope, trace contract, or acceptance rule | Protocol amendment and repeat P15 |
| Randomization or durability only | Repeat affected validation before final lock |

### Milestone 2 — Campaign 3

| Phase | Deliverable |
|---|---|
| P16 | Execute frozen sample-size rule behind information firewall; set even `B` or terminate `underpowered_no_launch` |
| P17 | Final registration, clean commit, manifests, external anchor, launch lock |
| P18 | Operator Campaign 3 run |

---

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

No production scientific call is authorized by this project plan alone.

---

## 6. Compression, review, and lint

### 6.1 Work-order sizing

Prefer small independently reviewable orders. Schedule pressure cannot relax scientific or authorization gates.

### 6.2 Parallel work

Evidence/dossier work may overlap only with benchmark-agnostic interface specification after P2. Scientific review is a P2 gate and does not run as a non-blocking background activity.

### 6.3 Protected blocker class

No finding affecting the estimand, treatment fidelity, primary/co-gate scoring, randomization, missingness, retry safety, artifact identity, or authorization may be time-boxed or accepted merely as a residual limitation.

### 6.4 Work-order lint

1. Does any artifact contain its own hash or its own future commit SHA?
2. Does any check depend on data that cannot exist at that execution point?
3. Is every asserted tool/provider capability supported by current help or authoritative documentation?
4. Does every Git check include tracked, staged, and untracked state as applicable?
5. Are inventories explicitly frozen rather than globbed?
6. Is file mode distinguished from cryptographic/external anchoring?
7. Are retrieval, effective, cutoff, decision, and execution times distinct?
8. Is the exact discovered test harness/count recorded rather than asserted from memory?
9. Are exact counts and equality invariants used where required?
10. Is every SHA full-length and machine-captured?
11. Are all ceilings numerically frozen and truthfully classified as hard, accounting, or advisory?
12. Are in-scope and out-of-scope boundaries explicit?
13. Does the order discover repository state rather than assume it?
14. Does every change to frozen bytes have an explicit versioned ledger entry?
15. Is every ruling, adoption, acceptance, and exception machine-recorded and anchored?
16. Is every cited clause verified against authoritative hashed bytes rather than memory?

---

## 7. Permitted claim language

### 7.1 Failure to meet campaign success rule

> The experiment did not establish both that mean guided-minus-control performance exceeded the preregistered margin of 0.10 and that guided performance exceeded the frozen baseline on the fixed confirmatory tasks during the campaign window.

Report separately which component gate failed and both intervals. Failure is not proof that guided search has no effect.

### 7.2 Successful campaign

> Under the registered fixed-task, adapter, provider-window, and adaptive-procedure conditions, the one-sided lower confidence bound for mean guided-minus-control performance exceeded 0.10 and the one-sided lower confidence bound for mean guided-minus-baseline performance exceeded zero.

No claim generalizes beyond the registered fixed tasks and service window.

### 7.3 Campaigns 1–2 archive

> For the service presented as `gpt-5.6-sol` during the applicable campaign windows, Campaign 1's Huffman task and Campaign 2's eight Suite P tasks provided insufficient baseline headroom under their preregistered gates.

---

## 8. Immediate authorized sequence after adoption

1. Execute operator P0 discovery and emit the root/adoption records.
2. Prepare, but do not execute, the explicit dedicated-Campaign-3 upstream operation for separate approval.
3. Draft RFC-1 v3 and the complete normative ledger against the v2 hash.
4. Obtain scientific review of the inference, simulation, control, and retry rules.
5. Freeze final RFC bytes and record the operator decision through the non-self-referential acceptance mechanism.
6. Only after recorded acceptance may P3 begin.

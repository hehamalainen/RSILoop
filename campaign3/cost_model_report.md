# Campaign 3: Cost Algebra & Affordability Inputs (SE-3R)

> [!IMPORTANT]
> **Governance Status**: NON-NORMATIVE / ANALYTICAL CLOSEOUT ONLY.
> This report derives exact parametric cost formulas across all four candidate design variants.
> It does **NOT** freeze, select, or recommend any values for `M`, `R`, `k`, `B`, `B_max`, `T_solution`, or `T_mutation`.
> RFC-1 v3 remains in `DRAFT` status and items P3–P18 remain blocked.

## 1. Preregistered Task Counts
- **Search Tasks**: 8 tasks
- **Confirmatory Tasks**: 8 tasks
- **Secondary Holdout Tasks**: 6 tasks
- **Final Tasks Total**: 14 tasks (8 confirmatory + 6 holdout)

## 2. Unresolved Design Axes & Four Candidate Variants
The total call count depends on two independent, unresolved scientific and engineering design axes:
1. **Axis A (Search Starting Baseline)**:
   - `SHARED`: 8k calls per block, shared across both arms.
   - `SEPARATE`: 16k calls per block, matched 8k guided and 8k unguided.
2. **Axis B (Mutation Lineage Topology)**:
   - `BLOCK_SHARED`: 2M mutation generator calls per block (shared across replicates).
   - `REPLICATE_SPECIFIC`: 2kM mutation generator calls per block (replicate-specific lineages).

### Exact Per-Block and B-Block Formulas

| Variant | Solution Calls (Per Block) | Mutation Calls (Per Block) | Provider Logical Calls (Per Block) | B-Block Logical Calls |
|---|---|---|---|---|
| `SHARED_BLOCK_SHARED` | `(50 + 16M)k` | `2M` | `(50 + 16M)k + 2M` | `[50k + (16k + 2)M]B` |
| `SHARED_REPLICATE_SPECIFIC` | `(50 + 16M)k` | `2kM` | `(50 + 18M)k` | `(50 + 18M)kB` |
| `SEPARATE_BLOCK_SHARED` | `(58 + 16M)k` | `2M` | `(58 + 16M)k + 2M` | `[58k + (16k + 2)M]B` |
| `SEPARATE_REPLICATE_SPECIFIC` | `(58 + 16M)k` | `2kM` | `(58 + 18M)k` | `(58 + 18M)kB` |

## 3. Call Classes, Retry Multiplier & Token Semantics
- **Subject-Model Solution Calls**: Solution attempts submitted to the model under test.
- **Mutation-Generation Calls**: Prompts to synthesize mutation candidates.
- **Local Evaluator Executions**: Harness execution of test suites (local compute, 0 provider tokens).
- **Total Provider Logical Calls**: `Solution Calls + Mutation Calls`.
- **Maximum Transport Attempts**: `R * Logical Calls`, where `R >= 1` is a mandatory symbolic integer multiplier (no default).

> [!NOTE]
> Pre-response / zero-body retries contribute zero output tokens but may incur provider-dependent input token billing exposure.

## 4. Affine Coefficient Tables (`logical_calls = intercept + slope * M`)
For all candidate $k \in \{1, 2, 3\}$ and even $B \in \{2, \dots, 40\}$, the call count is an affine function of $M$:

### Variant: `SHARED_BLOCK_SHARED`

| k | B | Logical Intercept | Logical Slope | Solution Intercept | Solution Slope | Mutation Intercept | Mutation Slope |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 100 | 36 | 100 | 32 | 0 | 4 |
| 1 | 4 | 200 | 72 | 200 | 64 | 0 | 8 |
| 1 | 6 | 300 | 108 | 300 | 96 | 0 | 12 |
| 1 | 8 | 400 | 144 | 400 | 128 | 0 | 16 |
| 1 | 10 | 500 | 180 | 500 | 160 | 0 | 20 |
| 1 | 12 | 600 | 216 | 600 | 192 | 0 | 24 |
| 1 | 14 | 700 | 252 | 700 | 224 | 0 | 28 |
| 1 | 16 | 800 | 288 | 800 | 256 | 0 | 32 |
| 1 | 18 | 900 | 324 | 900 | 288 | 0 | 36 |
| 1 | 20 | 1000 | 360 | 1000 | 320 | 0 | 40 |
| 1 | 22 | 1100 | 396 | 1100 | 352 | 0 | 44 |
| 1 | 24 | 1200 | 432 | 1200 | 384 | 0 | 48 |
| 1 | 26 | 1300 | 468 | 1300 | 416 | 0 | 52 |
| 1 | 28 | 1400 | 504 | 1400 | 448 | 0 | 56 |
| 1 | 30 | 1500 | 540 | 1500 | 480 | 0 | 60 |
| 1 | 32 | 1600 | 576 | 1600 | 512 | 0 | 64 |
| 1 | 34 | 1700 | 612 | 1700 | 544 | 0 | 68 |
| 1 | 36 | 1800 | 648 | 1800 | 576 | 0 | 72 |
| 1 | 38 | 1900 | 684 | 1900 | 608 | 0 | 76 |
| 1 | 40 | 2000 | 720 | 2000 | 640 | 0 | 80 |
| 2 | 2 | 200 | 68 | 200 | 64 | 0 | 4 |
| 2 | 4 | 400 | 136 | 400 | 128 | 0 | 8 |
| 2 | 6 | 600 | 204 | 600 | 192 | 0 | 12 |
| 2 | 8 | 800 | 272 | 800 | 256 | 0 | 16 |
| 2 | 10 | 1000 | 340 | 1000 | 320 | 0 | 20 |
| 2 | 12 | 1200 | 408 | 1200 | 384 | 0 | 24 |
| 2 | 14 | 1400 | 476 | 1400 | 448 | 0 | 28 |
| 2 | 16 | 1600 | 544 | 1600 | 512 | 0 | 32 |
| 2 | 18 | 1800 | 612 | 1800 | 576 | 0 | 36 |
| 2 | 20 | 2000 | 680 | 2000 | 640 | 0 | 40 |
| 2 | 22 | 2200 | 748 | 2200 | 704 | 0 | 44 |
| 2 | 24 | 2400 | 816 | 2400 | 768 | 0 | 48 |
| 2 | 26 | 2600 | 884 | 2600 | 832 | 0 | 52 |
| 2 | 28 | 2800 | 952 | 2800 | 896 | 0 | 56 |
| 2 | 30 | 3000 | 1020 | 3000 | 960 | 0 | 60 |
| 2 | 32 | 3200 | 1088 | 3200 | 1024 | 0 | 64 |
| 2 | 34 | 3400 | 1156 | 3400 | 1088 | 0 | 68 |
| 2 | 36 | 3600 | 1224 | 3600 | 1152 | 0 | 72 |
| 2 | 38 | 3800 | 1292 | 3800 | 1216 | 0 | 76 |
| 2 | 40 | 4000 | 1360 | 4000 | 1280 | 0 | 80 |
| 3 | 2 | 300 | 100 | 300 | 96 | 0 | 4 |
| 3 | 4 | 600 | 200 | 600 | 192 | 0 | 8 |
| 3 | 6 | 900 | 300 | 900 | 288 | 0 | 12 |
| 3 | 8 | 1200 | 400 | 1200 | 384 | 0 | 16 |
| 3 | 10 | 1500 | 500 | 1500 | 480 | 0 | 20 |
| 3 | 12 | 1800 | 600 | 1800 | 576 | 0 | 24 |
| 3 | 14 | 2100 | 700 | 2100 | 672 | 0 | 28 |
| 3 | 16 | 2400 | 800 | 2400 | 768 | 0 | 32 |
| 3 | 18 | 2700 | 900 | 2700 | 864 | 0 | 36 |
| 3 | 20 | 3000 | 1000 | 3000 | 960 | 0 | 40 |
| 3 | 22 | 3300 | 1100 | 3300 | 1056 | 0 | 44 |
| 3 | 24 | 3600 | 1200 | 3600 | 1152 | 0 | 48 |
| 3 | 26 | 3900 | 1300 | 3900 | 1248 | 0 | 52 |
| 3 | 28 | 4200 | 1400 | 4200 | 1344 | 0 | 56 |
| 3 | 30 | 4500 | 1500 | 4500 | 1440 | 0 | 60 |
| 3 | 32 | 4800 | 1600 | 4800 | 1536 | 0 | 64 |
| 3 | 34 | 5100 | 1700 | 5100 | 1632 | 0 | 68 |
| 3 | 36 | 5400 | 1800 | 5400 | 1728 | 0 | 72 |
| 3 | 38 | 5700 | 1900 | 5700 | 1824 | 0 | 76 |
| 3 | 40 | 6000 | 2000 | 6000 | 1920 | 0 | 80 |

### Variant: `SHARED_REPLICATE_SPECIFIC`

| k | B | Logical Intercept | Logical Slope | Solution Intercept | Solution Slope | Mutation Intercept | Mutation Slope |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 100 | 36 | 100 | 32 | 0 | 4 |
| 1 | 4 | 200 | 72 | 200 | 64 | 0 | 8 |
| 1 | 6 | 300 | 108 | 300 | 96 | 0 | 12 |
| 1 | 8 | 400 | 144 | 400 | 128 | 0 | 16 |
| 1 | 10 | 500 | 180 | 500 | 160 | 0 | 20 |
| 1 | 12 | 600 | 216 | 600 | 192 | 0 | 24 |
| 1 | 14 | 700 | 252 | 700 | 224 | 0 | 28 |
| 1 | 16 | 800 | 288 | 800 | 256 | 0 | 32 |
| 1 | 18 | 900 | 324 | 900 | 288 | 0 | 36 |
| 1 | 20 | 1000 | 360 | 1000 | 320 | 0 | 40 |
| 1 | 22 | 1100 | 396 | 1100 | 352 | 0 | 44 |
| 1 | 24 | 1200 | 432 | 1200 | 384 | 0 | 48 |
| 1 | 26 | 1300 | 468 | 1300 | 416 | 0 | 52 |
| 1 | 28 | 1400 | 504 | 1400 | 448 | 0 | 56 |
| 1 | 30 | 1500 | 540 | 1500 | 480 | 0 | 60 |
| 1 | 32 | 1600 | 576 | 1600 | 512 | 0 | 64 |
| 1 | 34 | 1700 | 612 | 1700 | 544 | 0 | 68 |
| 1 | 36 | 1800 | 648 | 1800 | 576 | 0 | 72 |
| 1 | 38 | 1900 | 684 | 1900 | 608 | 0 | 76 |
| 1 | 40 | 2000 | 720 | 2000 | 640 | 0 | 80 |
| 2 | 2 | 200 | 72 | 200 | 64 | 0 | 8 |
| 2 | 4 | 400 | 144 | 400 | 128 | 0 | 16 |
| 2 | 6 | 600 | 216 | 600 | 192 | 0 | 24 |
| 2 | 8 | 800 | 288 | 800 | 256 | 0 | 32 |
| 2 | 10 | 1000 | 360 | 1000 | 320 | 0 | 40 |
| 2 | 12 | 1200 | 432 | 1200 | 384 | 0 | 48 |
| 2 | 14 | 1400 | 504 | 1400 | 448 | 0 | 56 |
| 2 | 16 | 1600 | 576 | 1600 | 512 | 0 | 64 |
| 2 | 18 | 1800 | 648 | 1800 | 576 | 0 | 72 |
| 2 | 20 | 2000 | 720 | 2000 | 640 | 0 | 80 |
| 2 | 22 | 2200 | 792 | 2200 | 704 | 0 | 88 |
| 2 | 24 | 2400 | 864 | 2400 | 768 | 0 | 96 |
| 2 | 26 | 2600 | 936 | 2600 | 832 | 0 | 104 |
| 2 | 28 | 2800 | 1008 | 2800 | 896 | 0 | 112 |
| 2 | 30 | 3000 | 1080 | 3000 | 960 | 0 | 120 |
| 2 | 32 | 3200 | 1152 | 3200 | 1024 | 0 | 128 |
| 2 | 34 | 3400 | 1224 | 3400 | 1088 | 0 | 136 |
| 2 | 36 | 3600 | 1296 | 3600 | 1152 | 0 | 144 |
| 2 | 38 | 3800 | 1368 | 3800 | 1216 | 0 | 152 |
| 2 | 40 | 4000 | 1440 | 4000 | 1280 | 0 | 160 |
| 3 | 2 | 300 | 108 | 300 | 96 | 0 | 12 |
| 3 | 4 | 600 | 216 | 600 | 192 | 0 | 24 |
| 3 | 6 | 900 | 324 | 900 | 288 | 0 | 36 |
| 3 | 8 | 1200 | 432 | 1200 | 384 | 0 | 48 |
| 3 | 10 | 1500 | 540 | 1500 | 480 | 0 | 60 |
| 3 | 12 | 1800 | 648 | 1800 | 576 | 0 | 72 |
| 3 | 14 | 2100 | 756 | 2100 | 672 | 0 | 84 |
| 3 | 16 | 2400 | 864 | 2400 | 768 | 0 | 96 |
| 3 | 18 | 2700 | 972 | 2700 | 864 | 0 | 108 |
| 3 | 20 | 3000 | 1080 | 3000 | 960 | 0 | 120 |
| 3 | 22 | 3300 | 1188 | 3300 | 1056 | 0 | 132 |
| 3 | 24 | 3600 | 1296 | 3600 | 1152 | 0 | 144 |
| 3 | 26 | 3900 | 1404 | 3900 | 1248 | 0 | 156 |
| 3 | 28 | 4200 | 1512 | 4200 | 1344 | 0 | 168 |
| 3 | 30 | 4500 | 1620 | 4500 | 1440 | 0 | 180 |
| 3 | 32 | 4800 | 1728 | 4800 | 1536 | 0 | 192 |
| 3 | 34 | 5100 | 1836 | 5100 | 1632 | 0 | 204 |
| 3 | 36 | 5400 | 1944 | 5400 | 1728 | 0 | 216 |
| 3 | 38 | 5700 | 2052 | 5700 | 1824 | 0 | 228 |
| 3 | 40 | 6000 | 2160 | 6000 | 1920 | 0 | 240 |

### Variant: `SEPARATE_BLOCK_SHARED`

| k | B | Logical Intercept | Logical Slope | Solution Intercept | Solution Slope | Mutation Intercept | Mutation Slope |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 116 | 36 | 116 | 32 | 0 | 4 |
| 1 | 4 | 232 | 72 | 232 | 64 | 0 | 8 |
| 1 | 6 | 348 | 108 | 348 | 96 | 0 | 12 |
| 1 | 8 | 464 | 144 | 464 | 128 | 0 | 16 |
| 1 | 10 | 580 | 180 | 580 | 160 | 0 | 20 |
| 1 | 12 | 696 | 216 | 696 | 192 | 0 | 24 |
| 1 | 14 | 812 | 252 | 812 | 224 | 0 | 28 |
| 1 | 16 | 928 | 288 | 928 | 256 | 0 | 32 |
| 1 | 18 | 1044 | 324 | 1044 | 288 | 0 | 36 |
| 1 | 20 | 1160 | 360 | 1160 | 320 | 0 | 40 |
| 1 | 22 | 1276 | 396 | 1276 | 352 | 0 | 44 |
| 1 | 24 | 1392 | 432 | 1392 | 384 | 0 | 48 |
| 1 | 26 | 1508 | 468 | 1508 | 416 | 0 | 52 |
| 1 | 28 | 1624 | 504 | 1624 | 448 | 0 | 56 |
| 1 | 30 | 1740 | 540 | 1740 | 480 | 0 | 60 |
| 1 | 32 | 1856 | 576 | 1856 | 512 | 0 | 64 |
| 1 | 34 | 1972 | 612 | 1972 | 544 | 0 | 68 |
| 1 | 36 | 2088 | 648 | 2088 | 576 | 0 | 72 |
| 1 | 38 | 2204 | 684 | 2204 | 608 | 0 | 76 |
| 1 | 40 | 2320 | 720 | 2320 | 640 | 0 | 80 |
| 2 | 2 | 232 | 68 | 232 | 64 | 0 | 4 |
| 2 | 4 | 464 | 136 | 464 | 128 | 0 | 8 |
| 2 | 6 | 696 | 204 | 696 | 192 | 0 | 12 |
| 2 | 8 | 928 | 272 | 928 | 256 | 0 | 16 |
| 2 | 10 | 1160 | 340 | 1160 | 320 | 0 | 20 |
| 2 | 12 | 1392 | 408 | 1392 | 384 | 0 | 24 |
| 2 | 14 | 1624 | 476 | 1624 | 448 | 0 | 28 |
| 2 | 16 | 1856 | 544 | 1856 | 512 | 0 | 32 |
| 2 | 18 | 2088 | 612 | 2088 | 576 | 0 | 36 |
| 2 | 20 | 2320 | 680 | 2320 | 640 | 0 | 40 |
| 2 | 22 | 2552 | 748 | 2552 | 704 | 0 | 44 |
| 2 | 24 | 2784 | 816 | 2784 | 768 | 0 | 48 |
| 2 | 26 | 3016 | 884 | 3016 | 832 | 0 | 52 |
| 2 | 28 | 3248 | 952 | 3248 | 896 | 0 | 56 |
| 2 | 30 | 3480 | 1020 | 3480 | 960 | 0 | 60 |
| 2 | 32 | 3712 | 1088 | 3712 | 1024 | 0 | 64 |
| 2 | 34 | 3944 | 1156 | 3944 | 1088 | 0 | 68 |
| 2 | 36 | 4176 | 1224 | 4176 | 1152 | 0 | 72 |
| 2 | 38 | 4408 | 1292 | 4408 | 1216 | 0 | 76 |
| 2 | 40 | 4640 | 1360 | 4640 | 1280 | 0 | 80 |
| 3 | 2 | 348 | 100 | 348 | 96 | 0 | 4 |
| 3 | 4 | 696 | 200 | 696 | 192 | 0 | 8 |
| 3 | 6 | 1044 | 300 | 1044 | 288 | 0 | 12 |
| 3 | 8 | 1392 | 400 | 1392 | 384 | 0 | 16 |
| 3 | 10 | 1740 | 500 | 1740 | 480 | 0 | 20 |
| 3 | 12 | 2088 | 600 | 2088 | 576 | 0 | 24 |
| 3 | 14 | 2436 | 700 | 2436 | 672 | 0 | 28 |
| 3 | 16 | 2784 | 800 | 2784 | 768 | 0 | 32 |
| 3 | 18 | 3132 | 900 | 3132 | 864 | 0 | 36 |
| 3 | 20 | 3480 | 1000 | 3480 | 960 | 0 | 40 |
| 3 | 22 | 3828 | 1100 | 3828 | 1056 | 0 | 44 |
| 3 | 24 | 4176 | 1200 | 4176 | 1152 | 0 | 48 |
| 3 | 26 | 4524 | 1300 | 4524 | 1248 | 0 | 52 |
| 3 | 28 | 4872 | 1400 | 4872 | 1344 | 0 | 56 |
| 3 | 30 | 5220 | 1500 | 5220 | 1440 | 0 | 60 |
| 3 | 32 | 5568 | 1600 | 5568 | 1536 | 0 | 64 |
| 3 | 34 | 5916 | 1700 | 5916 | 1632 | 0 | 68 |
| 3 | 36 | 6264 | 1800 | 6264 | 1728 | 0 | 72 |
| 3 | 38 | 6612 | 1900 | 6612 | 1824 | 0 | 76 |
| 3 | 40 | 6960 | 2000 | 6960 | 1920 | 0 | 80 |

### Variant: `SEPARATE_REPLICATE_SPECIFIC`

| k | B | Logical Intercept | Logical Slope | Solution Intercept | Solution Slope | Mutation Intercept | Mutation Slope |
|---|---|---|---|---|---|---|---|
| 1 | 2 | 116 | 36 | 116 | 32 | 0 | 4 |
| 1 | 4 | 232 | 72 | 232 | 64 | 0 | 8 |
| 1 | 6 | 348 | 108 | 348 | 96 | 0 | 12 |
| 1 | 8 | 464 | 144 | 464 | 128 | 0 | 16 |
| 1 | 10 | 580 | 180 | 580 | 160 | 0 | 20 |
| 1 | 12 | 696 | 216 | 696 | 192 | 0 | 24 |
| 1 | 14 | 812 | 252 | 812 | 224 | 0 | 28 |
| 1 | 16 | 928 | 288 | 928 | 256 | 0 | 32 |
| 1 | 18 | 1044 | 324 | 1044 | 288 | 0 | 36 |
| 1 | 20 | 1160 | 360 | 1160 | 320 | 0 | 40 |
| 1 | 22 | 1276 | 396 | 1276 | 352 | 0 | 44 |
| 1 | 24 | 1392 | 432 | 1392 | 384 | 0 | 48 |
| 1 | 26 | 1508 | 468 | 1508 | 416 | 0 | 52 |
| 1 | 28 | 1624 | 504 | 1624 | 448 | 0 | 56 |
| 1 | 30 | 1740 | 540 | 1740 | 480 | 0 | 60 |
| 1 | 32 | 1856 | 576 | 1856 | 512 | 0 | 64 |
| 1 | 34 | 1972 | 612 | 1972 | 544 | 0 | 68 |
| 1 | 36 | 2088 | 648 | 2088 | 576 | 0 | 72 |
| 1 | 38 | 2204 | 684 | 2204 | 608 | 0 | 76 |
| 1 | 40 | 2320 | 720 | 2320 | 640 | 0 | 80 |
| 2 | 2 | 232 | 72 | 232 | 64 | 0 | 8 |
| 2 | 4 | 464 | 144 | 464 | 128 | 0 | 16 |
| 2 | 6 | 696 | 216 | 696 | 192 | 0 | 24 |
| 2 | 8 | 928 | 288 | 928 | 256 | 0 | 32 |
| 2 | 10 | 1160 | 360 | 1160 | 320 | 0 | 40 |
| 2 | 12 | 1392 | 432 | 1392 | 384 | 0 | 48 |
| 2 | 14 | 1624 | 504 | 1624 | 448 | 0 | 56 |
| 2 | 16 | 1856 | 576 | 1856 | 512 | 0 | 64 |
| 2 | 18 | 2088 | 648 | 2088 | 576 | 0 | 72 |
| 2 | 20 | 2320 | 720 | 2320 | 640 | 0 | 80 |
| 2 | 22 | 2552 | 792 | 2552 | 704 | 0 | 88 |
| 2 | 24 | 2784 | 864 | 2784 | 768 | 0 | 96 |
| 2 | 26 | 3016 | 936 | 3016 | 832 | 0 | 104 |
| 2 | 28 | 3248 | 1008 | 3248 | 896 | 0 | 112 |
| 2 | 30 | 3480 | 1080 | 3480 | 960 | 0 | 120 |
| 2 | 32 | 3712 | 1152 | 3712 | 1024 | 0 | 128 |
| 2 | 34 | 3944 | 1224 | 3944 | 1088 | 0 | 136 |
| 2 | 36 | 4176 | 1296 | 4176 | 1152 | 0 | 144 |
| 2 | 38 | 4408 | 1368 | 4408 | 1216 | 0 | 152 |
| 2 | 40 | 4640 | 1440 | 4640 | 1280 | 0 | 160 |
| 3 | 2 | 348 | 108 | 348 | 96 | 0 | 12 |
| 3 | 4 | 696 | 216 | 696 | 192 | 0 | 24 |
| 3 | 6 | 1044 | 324 | 1044 | 288 | 0 | 36 |
| 3 | 8 | 1392 | 432 | 1392 | 384 | 0 | 48 |
| 3 | 10 | 1740 | 540 | 1740 | 480 | 0 | 60 |
| 3 | 12 | 2088 | 648 | 2088 | 576 | 0 | 72 |
| 3 | 14 | 2436 | 756 | 2436 | 672 | 0 | 84 |
| 3 | 16 | 2784 | 864 | 2784 | 768 | 0 | 96 |
| 3 | 18 | 3132 | 972 | 3132 | 864 | 0 | 108 |
| 3 | 20 | 3480 | 1080 | 3480 | 960 | 0 | 120 |
| 3 | 22 | 3828 | 1188 | 3828 | 1056 | 0 | 132 |
| 3 | 24 | 4176 | 1296 | 4176 | 1152 | 0 | 144 |
| 3 | 26 | 4524 | 1404 | 4524 | 1248 | 0 | 156 |
| 3 | 28 | 4872 | 1512 | 4872 | 1344 | 0 | 168 |
| 3 | 30 | 5220 | 1620 | 5220 | 1440 | 0 | 180 |
| 3 | 32 | 5568 | 1728 | 5568 | 1536 | 0 | 192 |
| 3 | 34 | 5916 | 1836 | 5916 | 1632 | 0 | 204 |
| 3 | 36 | 6264 | 1944 | 6264 | 1728 | 0 | 216 |
| 3 | 38 | 6612 | 2052 | 6612 | 1824 | 0 | 228 |
| 3 | 40 | 6960 | 2160 | 6960 | 1920 | 0 | 240 |

## 5. Prior Figure Reconciliation

1. **`24kB`**:
   - **Scope**: Confirmatory evaluation subset only ($3 \text{ arms} \times 8 \text{ tasks} \times k = 24k$ per block).
   - **Omission**: Omits all search starting baselines ($8k$ or $16k$), mutant evaluations ($16Mk$), holdout evaluations ($18k$), and mutation calls ($2M$ or $2kM$).
2. **`(98k + 6)B`**:
   - **Assumption**: Assumes $M = 3$, `SHARED` search baseline ($8k$), and `BLOCK_SHARED` mutation topology ($2M = 6$).
   - **Evaluation**: $(8k + 16(3)k + 24k + 18k) + 6 = 98k + 6$ per block.
3. **`(106k + 6)B`**:
   - **Assumption**: Assumes $M = 3$, `SEPARATE` search baselines ($16k$), and `BLOCK_SHARED` mutation topology ($2M = 6$).
   - **Evaluation**: $(16k + 16(3)k + 24k + 18k) + 6 = 106k + 6$ per block.
4. **Existing P2 Reference (`(58 + 18M)kB`)**:
   - **Assumption**: Corresponds to `SEPARATE` search baselines ($16k$) and `REPLICATE_SPECIFIC` mutation topology ($2kM$).
   - **Evaluation**: $(16 + 16M + 24 + 18)k + 2kM = (58 + 18M)k$ per block; equals $112kB$ when $M=3$.

> [!IMPORTANT]
> None of the above figures is authoritative until the operator officially decides the design axes and parameter registers.

## 6. Work Order Traceability Matrix (SE-3R)

| Clause ID | Title | Implementation Location | Exact Test Suite / Verification |
|---|---|---|---|
| `SE3R-1` | Cost Four Variants; Select None | `campaign3/cost_model.py:69-80 (parse_variant), campaign3/cost_model.py:82-135 (get_per_block_symbolic_formulas)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r1_task_counts_and_four_variants_definition` |
| `SE3R-2` | Exact Derivation Across Two Unresolved Design Axes | `campaign3/cost_model.py:82-135 (get_per_block_symbolic_formulas), campaign3/cost_model.py:137-185 (get_affine_coefficients), campaign3/cost_model.py:187-238 (evaluate_cost_breakdown)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r2_hand_computed_fixtures_all_four_variants` |
| `SE3R-3` | Call Classes, Fail-Closed Validation & Symbolic Retry Multiplier | `campaign3/cost_model.py:34-38 (validate_k), campaign3/cost_model.py:40-44 (validate_b), campaign3/cost_model.py:46-50 (validate_m), campaign3/cost_model.py:52-56 (validate_r), campaign3/cost_model.py:58-67 (validate_tokens), campaign3/cost_model.py:187-238 (evaluate_cost_breakdown)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r3_fail_closed_input_validation` |
| `SE3R-4` | Machine-Generated Affine Coefficient Tables (k in {1,2,3}, even B in [2,40]) | `campaign3/cost_model.py:240-257 (generate_all_coefficient_tables), campaign3/cost_model.py:137-185 (get_affine_coefficients)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r4_coefficient_tables_generation` |
| `SE3R-5` | Reconciliation of Prior Historical and Reference Figures | `campaign3/cost_model.py:259-318 (reconcile_prior_figures)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r5_reconcile_prior_figures` |
| `SE3R-6` | Tests, Isolation, Artifact Emission & Non-Selection Assertions | `campaign3/cost_model.py:370-420 (build_cost_model_report), campaign3/cost_model.py:422-508 (generate_cost_model_markdown), campaign3/cost_model.py:510-530 (emit_cost_model_artifacts)` | `test_campaign3_cost_model.TestCampaign3CostModel.test_se3r6_isolated_artifact_emission_byte_exact` |

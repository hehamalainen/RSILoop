# Discovery Probe

This is an experimental, text-only sidecar benchmark inspired by
[DiG-bench](https://arxiv.org/abs/2608.12593). It is not part of RSILoop's
frozen optimization, validation, holdout, or promotion protocol.

An episode chooses a deterministic hidden transformation. The player can run a
bounded number of experiments on any short string. Requesting the challenges
irreversibly closes exploration; only exact predictions on those committed
holdout inputs count toward the score.

```bash
python3 -m discovery_probe --seed 42 --tier 2
```

Difficulty tiers compose one, two, or three unfamiliar operations. The session
prints a SHA-256 commitment before the first probe and reveals the complete rule
and expected answers in the final report. This makes a transcript auditable
without leaking the answer during play.

For a diagnostic control, run the same seed with the rules supplied:

```bash
python3 -m discovery_probe --seed 42 --tier 2 --reveal-rules
```

`score_conditions()` compares hidden-rule and rules-given episode results. Its
`discovery_fraction` is the share of the rules-given execution ceiling achieved
without being told the rule; `rule_information_gain` is the absolute solve-rate
increase when rule discovery is removed from the task.

The next integration step would expose a `DiscoverySession` through the same
contained tool boundary used by a candidate agent, then preregister separate
seeds for prompt search and confirmation. Until that protocol exists, this is a
playable instrumentation prototype, not evidence of RSI.

# Contributing to RSILoop

Thank you for your interest in contributing to **RSILoop**. Because RSILoop is a containment-hardened research prototype with cryptographically sealed historical evidence manifests, contributors should follow the guidelines below.

---

## 1. Environment Setup

1. Clone the repository on **macOS** (required for `/usr/bin/sandbox-exec` containment tests).
2. Create a Python 3.11+ virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Restore read-only (`0444`) modes on verified historical artifacts (also run automatically by `run_tests.py`):
   ```bash
   .venv/bin/python scripts/reseal_readonly_artifacts.py
   ```

---

## 2. Running Verification & Hygiene Checks

Before opening a pull request, ensure both the strict test suite and the release hygiene gate pass with zero warnings and zero skips:

```bash
.venv/bin/python run_tests.py
.venv/bin/python scripts/check_release_hygiene.py
```

For fast iteration on specific modules:

- **Discovery Probe sidecar**: `.venv/bin/python -m unittest test_discovery_probe.py -v`
- **Campaign 2 containment & manifests**: `.venv/bin/python -m unittest test_campaign2.py -v`
- **Campaign 2 orchestration & durability**: `.venv/bin/python -m unittest test_campaign2_orchestration.py -v`
- **Campaign 3 cost model & simulation**: `.venv/bin/python -m unittest test_campaign3_cost_model.py test_campaign3_sim_model.py -v`

---

## 3. Working with Sealed Artifacts and Manifests

- **Do not modify frozen historical evidence** under `history/`, `campaign2_archive/`, `campaign3/*.json`, or `benchmarks/campaign2/`. These files are anchored to historical Git commits and SHA-256 manifests (`campaign2_evidence_hashes.json`, `campaign2_parity_matrix.json`, `campaign3/verifier.py`, `campaign3/p1_verifier.py`).
- **Category 1 Source Inventory (`campaign2_preflight_hashes.json`)**:
  - `campaign2/manifest.py` authenticates 145 Category 1 files (including `README.md`, `rsi_loop.py`, `run_tests.py`, `docs/walkthrough.md`, and `campaign2/*.py`).
  - If your change legitimately modifies a Category 1 file, regenerate the preflight and archive manifests before running the test suite:
    ```bash
    .venv/bin/python scripts/reseal_readonly_artifacts.py --regenerate-manifests
    ```
- **Release Allowlist (`RELEASE_ALLOWLIST.json`)**:
  - Any new root file or top-level directory must be explicitly added to `RELEASE_ALLOWLIST.json` so `scripts/check_release_hygiene.py` can authenticate the repository layout.
- **No Personal Paths or Live Credentials**:
  - Never commit absolute local paths (e.g., `/Users/...`) or live API credentials. `scripts/check_release_hygiene.py` enforces this automatically.

---

## 4. Licensing

By contributing to RSILoop, you agree that your contributions will be licensed under the project's [MIT License](LICENSE).

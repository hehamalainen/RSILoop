# RSILoop Implementation Walkthrough

This document details the recent architectural improvements, containment validation fixes, and strict testing practices introduced to the RSILoop recursive self-improvement project.

## 🎯 Key Achievements

### 1. Robust and Secure Sandboxing
- Implemented `candidate_runner.py` to enforce irreversible CPU time, file size, and file descriptor limits using `resource.setrlimit`.
- Executed the runner inside a strict sandbox profile (`candidate.sb.template`), which prevents network access, process forking, and writing files outside the temporary directory.

### 2. Precise RPC Enveloping & Two-Pass Export Decoding
- Cleanly redirected the candidate's `stdout` to `/dev/null` inside `candidate_runner.py` while reserving a separate file descriptor for framed JSON IPC communication.
- Implemented a two-pass exports analyzer inside `candidate_runner.py` that discovers all classes first, guaranteeing that complex object structures (e.g. `Matrix` operations) serialize cleanly regardless of alphabetical ordering.

### 3. Fail-Closed Private Holdout Engine
- Reinforced holdout validation logic to require verified files and hashes under `rsi_loop_private/benchmarks/simple_regex_matcher`.
- Enforced a fail-closed check: if the private holdout directory is missing or malformed, the run immediately aborts with `ExperimentInvalidError` unless `ALLOW_NO_HOLDOUT` is explicitly authorized in the environment and recorded as `no_holdout_mode_active: true` in the run manifest.

### 4. Strict Token Schema Parsing
- Implemented strict type validation for Codex output token fields: they must be non-boolean JSON integers greater than or equal to zero. Any malformed values trigger a fail-closed transition to `unknown_token_state = true`.
- Ensured token ceiling breaches update the final consumed token counter to the exact breaching value (e.g., `50001` or greater) in the invalid run manifest.

### 5. Codex Executable Portability
- Integrated dynamic lookup utility `_get_codex_bin_path()` to locate Codex binary on the system (supporting `/usr/local/bin/codex` and `/opt/homebrew/bin/codex`), eliminating hardcoded command execution failures.

### 6. Strict Exception Verification & Shared Hook Collector
- Configured a centralized `sys.unraisablehook` collector inside `run_tests.py`.
- Redesigned individual test modules (`test_evaluator.py` and `test_rsi_loop.py`) to share the central collector list during test discovery, ensuring all unraisable warnings and resource leaks are tracked without uninstalling the runner hook.
- Added a controlled unraisable event test to verify that the active hook collects and propagates all unexpected warnings.

---

## 🧪 Verification Results

The entire regression suite has been verified on the host machine:
- **Discovered/Executed Tests**: 44 tests
- **Skips/Warnings**: 0
- **Unraisable Exceptions**: 0
- **Overall Result**: All tests passed cleanly in under 5.0 seconds.

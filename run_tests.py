#!/usr/bin/env python3
import sys
import os
import unittest
import warnings
import gc

# 1. Enable universal warning promotion to errors across all categories
warnings.simplefilter("error")

# 2. Unraisable exception hook collector
unraisable_exceptions = []
def unraisable_hook(unraisable):
    unraisable_exceptions.append(unraisable)

sys.unraisablehook = unraisable_hook

def main():
    workspace_root = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, workspace_root)

    # STRICTLY READ-ONLY Verification of preflight, evidence, parity matrix, and tier assignment artifacts
    from campaign2.manifest import (
        verify_campaign2_manifest,
        verify_campaign2_evidence_manifest,
        verify_campaign2_parity_matrix
    )
    from campaign2.tier_assignment import verify_family_blocked_assignment

    ok, manifest_count = verify_campaign2_manifest(workspace_root)
    if not ok or manifest_count != 145:
        sys.stderr.write(f"FATAL: Campaign 2 preflight manifest verification failed (ok={ok}, count={manifest_count})\n")
        sys.exit(1)

    verify_campaign2_evidence_manifest(workspace_root)
    verify_campaign2_parity_matrix(workspace_root)
    verify_family_blocked_assignment(workspace_root)

    # 3. Load test suite
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=workspace_root, pattern="test_*.py")

    runner = unittest.TextTestRunner(verbosity=2)

    sys.stdout.write("Discovering tests...\n")
    discovered_count = suite.countTestCases()
    sys.stdout.write(f"Discovered test cases: {discovered_count}\n")

    MIN_TESTS = 171
    if discovered_count < MIN_TESTS:
        sys.stderr.write(f"FATAL: Discovered test count {discovered_count} is less than minimum {MIN_TESTS}\n")
        sys.exit(1)

    result = runner.run(suite)

    # 4. Force GC collect to trigger any pending unraisable hooks
    gc.collect()

    success = True

    if not result.wasSuccessful():
        sys.stderr.write("FATAL: Test suite failed or errored\n")
        success = False

    # Zero-skip enforcement
    if result.skipped:
        sys.stderr.write(f"FATAL: {len(result.skipped)} test(s) skipped:\n")
        for test_case, reason in result.skipped:
            sys.stderr.write(f"  - {test_case}: {reason}\n")
        success = False

    if unraisable_exceptions:
        sys.stderr.write(f"FATAL: {len(unraisable_exceptions)} unraisable exception(s) caught:\n")
        for exc in unraisable_exceptions:
            sys.stderr.write(f"  - {exc}\n")
        success = False

    if not success:
        sys.exit(1)

    sys.stdout.write(f"Executed test cases: {discovered_count}\n")
    sys.stdout.write("All tests passed cleanly with zero skips, zero warnings, and zero unraisable exceptions!\n")
    sys.exit(0)

if __name__ == "__main__":
    main()

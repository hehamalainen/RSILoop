#!/usr/bin/env python3
import sys
import os
import unittest
import warnings
import gc

# 1. Enable ResourceWarning promotion to errors
warnings.simplefilter("error", ResourceWarning)

# 2. Unraisable exception hook collector
unraisable_exceptions = []
def unraisable_hook(unraisable):
    unraisable_exceptions.append(unraisable)

sys.unraisablehook = unraisable_hook

def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

    # 3. Load test suite
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=os.path.dirname(os.path.abspath(__file__)), pattern="test_*.py")

    # Custom test runner to track results strictly
    runner = unittest.TextTestRunner(verbosity=2)

    sys.stdout.write("Discovering tests...\n")
    discovered_count = suite.countTestCases()
    sys.stdout.write(f"Discovered test cases: {discovered_count}\n")

    # Enforce the approved host-boundary regression floor.
    MIN_TESTS = 139
    if discovered_count < MIN_TESTS:
        sys.stderr.write(f"FATAL: Discovered test count {discovered_count} is less than minimum {MIN_TESTS}\n")
        sys.exit(1)

    result = runner.run(suite)

    # 4. Force GC collect to trigger any pending unraisable hooks
    gc.collect()

    success = True

    # Check for test failures or errors
    if not result.wasSuccessful():
        sys.stderr.write("FATAL: Test suite failed or errored\n")
        success = False

    # 5. Fail on skipped tests
    if result.skipped:
        sys.stderr.write(f"FATAL: Skipped tests are forbidden. Skipped count: {len(result.skipped)}\n")
        for test, reason in result.skipped:
            sys.stderr.write(f"  - {test}: {reason}\n")
        success = False

    # 6. Check for unraisable exceptions
    if unraisable_exceptions:
        sys.stderr.write(f"FATAL: Collected {len(unraisable_exceptions)} unraisable exceptions/ResourceWarnings:\n")
        for ue in unraisable_exceptions:
            sys.stderr.write(f"  - {ue.exc_type.__name__}: {ue.exc_value}\n")
        success = False

    # Check if executed count matches expected
    executed_count = result.testsRun
    sys.stdout.write(f"Executed test cases: {executed_count}\n")

    # Assert that the active hook is still our unraisable_hook
    if sys.unraisablehook is not unraisable_hook:
        sys.stderr.write("FATAL: sys.unraisablehook was overwritten during test discovery!\n")
        success = False

    # Test with a controlled unraisable exception
    import collections
    UnraisableArgs = collections.namedtuple('UnraisableArgs', ['exc_type', 'exc_value', 'exc_traceback', 'err_msg', 'object'])
    dummy_args = UnraisableArgs(exc_type=ValueError, exc_value=ValueError("Controlled test exception"), exc_traceback=None, err_msg="Controlled test", object=None)

    initial_len = len(unraisable_exceptions)
    sys.unraisablehook(dummy_args)
    if len(unraisable_exceptions) <= initial_len or unraisable_exceptions[-1].exc_value is not dummy_args.exc_value:
        sys.stderr.write("FATAL: sys.unraisablehook failed to collect the controlled unraisable exception!\n")
        success = False
    else:
        # Remove the controlled exception from the list so it doesn't trigger "unraisable exception detected" failure
        unraisable_exceptions.pop()

    if not success:
        sys.exit(1)

    sys.stdout.write("All tests passed cleanly with zero skips, zero warnings, and zero unraisable exceptions!\n")
    sys.exit(0)

if __name__ == "__main__":
    main()

import subprocess
import os
import sys

def evaluate_benchmark(benchmark_dir):
    test_harness_path = os.path.join(benchmark_dir, "test_harness.py")
    if not os.path.exists(test_harness_path):
        return {"success": False, "error": "test_harness.py not found"}
    
    solution_path = os.path.join(benchmark_dir, "solution.py")
    if not os.path.exists(solution_path):
        return {"success": False, "error": "solution.py not found"}
        
    try:
        # Run python -m unittest test_harness.py in the directory
        res = subprocess.run(
            [sys.executable, "-m", "unittest", "test_harness.py"],
            cwd=benchmark_dir,
            capture_output=True,
            text=True,
            timeout=3.0 # strict timeout to prevent infinite loops
        )
        
        passed = (res.returncode == 0)
        output = res.stdout + "\n" + res.stderr
        
        return {
            "success": passed,
            "error": None if passed else output.strip(),
            "code": res.returncode
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "TimeoutExpired (potential infinite loop)"
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evaluator.py <benchmark_directory>")
        sys.exit(1)
        
    result = evaluate_benchmark(sys.argv[1])
    print(f"Success: {result['success']}")
    if result['error']:
        print(f"Error:\n{result['error']}")

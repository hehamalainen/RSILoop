#!/usr/bin/env python3
"""
Environment verification utility for RSILoop.

Supports two modes:
- `python3 verify_env.py --offline`: Verifies offline research-prototype requirements
  (Python >= 3.11, `numpy`, macOS `/usr/bin/sandbox-exec`, and sealed artifact/manifest
  integrity) without reading `~/.env`, requiring API keys, or importing optional provider SDKs.
- `python3 verify_env.py`: Performs the offline checks plus optional live-provider checks
  (`~/.env` API keys, CLI tools, and optional provider SDK packages).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def check_offline_environment(workspace_root: str = BASE_DIR) -> Tuple[bool, Dict[str, Any]]:
    """
    Verify offline environment requirements without reading ~/.env or optional SDKs.
    Returns (ok, details_dict).
    """
    print("=== Offline Environment & Integrity Check ===")
    errors: List[str] = []
    details: Dict[str, Any] = {
        "mode": "offline",
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "python_ok": False,
        "numpy_ok": False,
        "numpy_version": None,
        "sandbox_exec_ok": False,
        "sandbox_exec_path": "/usr/bin/sandbox-exec",
        "sealed_artifacts_ok": False,
        "verified_artifact_count": 0,
        "manifest_ok": False,
        "manifest_file_count": 0,
        "errors": errors,
    }

    if sys.version_info >= (3, 11):
        details["python_ok"] = True
        print(f"[+] Python version {details['python_version']} (>= 3.11)")
    else:
        msg = f"Python 3.11+ required, found {details['python_version']}"
        errors.append(msg)
        print(f"[-] {msg}")

    try:
        import numpy as np

        details["numpy_ok"] = True
        details["numpy_version"] = getattr(np, "__version__", "unknown")
        print(f"[+] Required package 'numpy' is installed (version {details['numpy_version']})")
    except ImportError as exc:
        msg = f"Required package 'numpy' is NOT installed: {exc}"
        errors.append(msg)
        print(f"[-] {msg}")

    sandbox_path = "/usr/bin/sandbox-exec"
    if sys.platform == "darwin" and os.path.isfile(sandbox_path) and os.access(sandbox_path, os.X_OK):
        details["sandbox_exec_ok"] = True
        print(f"[+] Found macOS Seatbelt binary at {sandbox_path}")
    else:
        msg = f"macOS Seatbelt binary not available at {sandbox_path} (platform={sys.platform})"
        errors.append(msg)
        print(f"[-] {msg}")

    try:
        from scripts.reseal_readonly_artifacts import ensure_readonly_artifact_modes
        from campaign2.manifest import verify_campaign2_manifest

        verified_count, _updated_count = ensure_readonly_artifact_modes(workspace_root)
        details["sealed_artifacts_ok"] = True
        details["verified_artifact_count"] = verified_count
        print(f"[+] Verified {verified_count} sealed read-only artifacts")

        manifest_ok, manifest_count = verify_campaign2_manifest(workspace_root)
        details["manifest_ok"] = bool(manifest_ok)
        details["manifest_file_count"] = manifest_count
        print(f"[+] Verified release & historical preflight manifests ({manifest_count} Category 1 files)")
    except Exception as exc:
        msg = f"Artifact/manifest verification failed: {exc}"
        errors.append(msg)
        print(f"[-] {msg}")

    ok = len(errors) == 0
    return ok, details


def check_env_file() -> bool:
    print("=== Environment Variables Check ===")
    env_path = os.path.expanduser("~/.env")
    if not os.path.exists(env_path):
        print(f"[-] ~/.env file not found at {env_path}")
        return False

    print("[+] Found ~/.env file")
    keys_to_check = ["GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
    for key in keys_to_check:
        found = False
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{key}="):
                        found = True
                        break
        except Exception as e:
            print(f"  [-] Error reading ~/.env: {e}")
            return False
        if found:
            print(f"  [+] {key} is defined in ~/.env")
        else:
            print(f"  [-] {key} is NOT defined in ~/.env")
    return True


def check_cli_tools() -> None:
    print("\n=== CLI Tools Check ===")
    clis = {
        "gh": "GitHub CLI (gh)",
        "claude": "Anthropic Claude CLI (claude)",
    }
    for cli, desc in clis.items():
        path = shutil.which(cli)
        if path:
            print(f"[+] Found {desc} at: {path}")
            if cli == "gh":
                try:
                    res = subprocess.run(["gh", "--version"], capture_output=True, text=True, timeout=5)
                    print(f"    Version: {res.stdout.splitlines()[0]}")
                except Exception as e:
                    print(f"    [-] Error running gh: {e}")
        else:
            print(f"[-] {desc} is NOT available on PATH")


def check_python_packages() -> None:
    print("\n=== Python Packages Check ===")
    packages = ["openai", "anthropic", "dotenv"]
    for pkg in packages:
        try:
            __import__(pkg)
            print(f"[+] Package '{pkg}' is installed")
        except ImportError:
            print(f"[-] Package '{pkg}' is NOT installed")

    try:
        from google import genai  # noqa: F401

        print("[+] Package 'google-genai' is installed")
    except ImportError:
        try:
            import google.generativeai  # noqa: F401

            print("[+] Package 'google-generativeai' is installed")
        except ImportError:
            print("[-] Neither 'google-genai' nor 'google-generativeai' is installed")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Verify RSILoop environment and artifact integrity.")
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Verify offline requirements (Python >= 3.11, numpy, sandbox-exec, sealed artifacts) without checking ~/.env or optional LLM SDKs.",
    )
    args = parser.parse_args(argv)

    if args.offline:
        ok, _details = check_offline_environment(BASE_DIR)
        return 0 if ok else 1

    ok, _details = check_offline_environment(BASE_DIR)
    print()
    check_env_file()
    check_cli_tools()
    check_python_packages()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

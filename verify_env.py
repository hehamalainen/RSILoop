import os
import shutil
import subprocess
import sys

def check_env_file():
    print("=== Environment Variables Check ===")
    env_path = os.path.expanduser("~/.env")
    if not os.path.exists(env_path):
        print(f"[-] ~/.env file not found at {env_path}")
        return False

    print(f"[+] Found ~/.env file")
    keys_to_check = ["GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"]
    for key in keys_to_check:
        found = False
        try:
            with open(env_path, "r") as f:
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

def check_cli_tools():
    print("\n=== CLI Tools Check ===")
    clis = {
        "gh": "GitHub CLI (gh)",
        "claude": "Anthropic Claude CLI (claude)"
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

def check_python_packages():
    print("\n=== Python Packages Check ===")
    packages = ["openai", "anthropic", "dotenv"]
    for pkg in packages:
        try:
            __import__(pkg)
            print(f"[+] Package '{pkg}' is installed")
        except ImportError:
            print(f"[-] Package '{pkg}' is NOT installed")

    # Try importing google-genai or google-generativeai
    try:
        from google import genai
        print("[+] Package 'google-genai' is installed")
    except ImportError:
        try:
            import google.generativeai
            print("[+] Package 'google-generativeai' is installed")
        except ImportError:
            print("[-] Neither 'google-genai' nor 'google-generativeai' is installed")

if __name__ == "__main__":
    check_env_file()
    check_cli_tools()
    check_python_packages()

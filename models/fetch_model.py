"""
External GGUF Model Artifact Fetcher & Verification Utility.

Downloads and verifies the exact GGUF model weights artifact.
Enforces exact file size, exact SHA256 checksum, regular file status, and no symlinks.
"""

import os
import json
import hashlib
import urllib.request

MODELS_DIR = os.path.dirname(os.path.abspath(__file__))
DESCRIPTOR_PATH = os.path.join(MODELS_DIR, "model_descriptor.json")


def load_descriptor() -> dict:
    if not os.path.exists(DESCRIPTOR_PATH):
        raise FileNotFoundError(f"Missing model descriptor: {DESCRIPTOR_PATH}")
    with open(DESCRIPTOR_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_model_artifact(model_path: str, expected_sha256: str, expected_size: int) -> bool:
    """Verifies that model_path is a regular file, has exact size and SHA256, and contains no symlink components."""
    if not os.path.exists(model_path):
        return False
    if os.path.islink(model_path):
        raise ValueError(f"Model artifact path must not be a symbolic link: {model_path}")
    st = os.stat(model_path)
    if st.st_size != expected_size:
        return False

    sha = hashlib.sha256()
    with open(model_path, "rb") as f:
        while chunk := f.read(8192 * 1024):
            sha.update(chunk)
    return sha.hexdigest() == expected_sha256


def fetch_and_verify_model() -> str:
    """Ensure GGUF model artifact exists and is valid. Downloads if missing or corrupted."""
    desc = load_descriptor()
    model_path = os.path.join(os.path.dirname(MODELS_DIR), desc["relative_path"])
    expected_sha = desc["expected_sha256"]
    expected_size = desc["expected_size_bytes"]

    if verify_model_artifact(model_path, expected_sha, expected_size):
        return model_path

    print(f"Downloading model artifact {desc['filename']} from {desc['download_url']}...")
    os.makedirs(os.path.dirname(model_path), exist_ok=True)
    tmp_path = model_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    urllib.request.urlretrieve(desc["download_url"], tmp_path)
    if not verify_model_artifact(tmp_path, expected_sha, expected_size):
        if os.path.exists(tmp_path): os.remove(tmp_path)
        raise ValueError(f"Downloaded model artifact failed SHA256/size verification: expected {expected_sha}")

    os.rename(tmp_path, model_path)
    return model_path


if __name__ == "__main__":
    p = fetch_and_verify_model()
    print(f"Model artifact verified successfully at: {p}")

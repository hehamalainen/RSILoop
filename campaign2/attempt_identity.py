"""Sealed, explicit identity and lookup for resumable Campaign 2 attempts."""

import hashlib
import json
import os
import re
import stat
from typing import Optional


IDENTITY_FILE = "campaign2_attempt_identity.json"
IDENTITY_ANCHOR = "campaign2_attempt_identity.sha256"
ATTEMPT_ID_RE = re.compile(r"^run_[A-Za-z0-9_-]+$")


class AttemptIdentityError(RuntimeError):
    pass


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _seal(path: str, content: bytes) -> None:
    if os.path.lexists(path):
        raise AttemptIdentityError(f"Refusing to overwrite sealed attempt identity state: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, content)
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_sealed(path: str) -> bytes:
    if not os.path.lexists(path) or os.path.islink(path) or not os.path.isfile(path):
        raise AttemptIdentityError(f"Missing or invalid sealed attempt identity state: {path}")
    if stat.S_IMODE(os.stat(path).st_mode) != 0o444:
        raise AttemptIdentityError(f"Attempt identity state must have mode 0o444: {path}")
    with open(path, "rb") as handle:
        return handle.read()


def create_attempt_identity(
    run_dir: str,
    run_id: str,
    stage: str,
    suite: str,
    is_mock: bool,
    registration_sha256: str,
    *,
    require_transport_evidence: bool = True,
) -> dict:
    run_dir = os.path.realpath(run_dir)
    if not ATTEMPT_ID_RE.fullmatch(run_id) or os.path.basename(run_dir) != run_id:
        raise AttemptIdentityError("Attempt identity must equal its canonical run directory name")
    data = {
        "schema_version": "1",
        "attempt_id": run_id,
        "stage": stage,
        "suite": suite,
        "is_mock": bool(is_mock),
        "canonical_run_dir": run_dir,
        "registration_sha256": registration_sha256,
        "require_transport_evidence": bool(require_transport_evidence),
        "checkpoint_file": f"campaign2_{stage}_checkpoint.json",
        "ledger_file": f"campaign2_{stage}_ledger.jsonl",
    }
    content = _canonical(data)
    _seal(os.path.join(run_dir, IDENTITY_FILE), content)
    _seal(
        os.path.join(run_dir, IDENTITY_ANCHOR),
        (hashlib.sha256(content).hexdigest() + "\n").encode("ascii"),
    )
    return data


def verify_attempt_identity(
    run_dir: str,
    expected_attempt_id: Optional[str] = None,
    expected_stage: Optional[str] = None,
    expected_mock: Optional[bool] = None,
    expected_registration_sha256: Optional[str] = None,
    *,
    expected_require_transport_evidence: Optional[bool] = None,
) -> dict:
    run_dir = os.path.realpath(run_dir)
    content = _read_sealed(os.path.join(run_dir, IDENTITY_FILE))
    anchor = _read_sealed(os.path.join(run_dir, IDENTITY_ANCHOR)).decode("ascii").strip()
    if hashlib.sha256(content).hexdigest() != anchor:
        raise AttemptIdentityError("Attempt identity hash anchor mismatch")
    try:
        data = json.loads(content.decode("utf-8"))
    except Exception as exc:
        raise AttemptIdentityError(f"Invalid attempt identity JSON: {exc}") from exc
    if content != _canonical(data) or data.get("schema_version") != "1":
        raise AttemptIdentityError("Attempt identity is not canonical schema 1")
    attempt_id = data.get("attempt_id", "")
    if not ATTEMPT_ID_RE.fullmatch(attempt_id):
        raise AttemptIdentityError("Attempt identity contains an invalid attempt_id")
    if data.get("canonical_run_dir") != run_dir or os.path.basename(run_dir) != attempt_id:
        raise AttemptIdentityError("Attempt identity is not bound to this canonical run directory")
    if "require_transport_evidence" in data and type(data["require_transport_evidence"]) is not bool:
        raise AttemptIdentityError("Attempt identity require_transport_evidence must be a boolean")
    checks = (
        ("attempt_id", expected_attempt_id),
        ("stage", expected_stage),
        ("is_mock", expected_mock),
        ("registration_sha256", expected_registration_sha256),
        ("require_transport_evidence", expected_require_transport_evidence),
    )
    for field, expected in checks:
        if expected is not None and data.get(field) != expected:
            raise AttemptIdentityError(
                f"Attempt identity {field} mismatch: expected {expected!r}, got {data.get(field)!r}"
            )
    return data


def resolve_resume_attempt(
    history_dir: str,
    attempt_id: str,
    stage: str,
    is_mock: bool,
    *,
    expected_require_transport_evidence: Optional[bool] = True,
) -> tuple[str, dict]:
    """Resolve exactly one named attempt.  No mtime or latest-run fallback exists."""
    if not ATTEMPT_ID_RE.fullmatch(attempt_id or ""):
        raise AttemptIdentityError("--resume-attempt-id must name a sealed run_<id> attempt")
    history_real = os.path.realpath(history_dir)
    run_dir = os.path.realpath(os.path.join(history_real, attempt_id))
    if os.path.dirname(run_dir) != history_real or not os.path.isdir(run_dir):
        raise AttemptIdentityError(f"Named resume attempt does not exist: {attempt_id}")
    data = verify_attempt_identity(
        run_dir,
        expected_attempt_id=attempt_id,
        expected_stage=stage,
        expected_mock=is_mock,
        expected_require_transport_evidence=expected_require_transport_evidence,
    )
    return run_dir, data

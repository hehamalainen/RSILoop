#!/usr/bin/env python3
"""
Release hygiene, allowlist, license, and metadata boundary verifier for RSILoop.

Checks:
1. Required release documentation and licensing files exist (`LICENSE`, `THIRD_PARTY_NOTICES.md`,
   `RELEASE_ALLOWLIST.json`).
2. Every Git-tracked file in the repository belongs to either `allowed_root_files` or one of
   `allowed_top_level_directories` in `RELEASE_ALLOWLIST.json`.
3. Excluded outer-workspace review archives and private directories are absent from the working tree.
4. No active unsealed source, test, or documentation file contains maintainer-local `/Users/`
   filesystem paths (historical cryptographically anchored evidence files are explicitly enumerated).
5. No tracked file contains live credential patterns outside synthetic unit-test fixtures.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Set, Tuple

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# Historical cryptographically anchored evidence records that intentionally preserve
# pre-release provenance paths and cannot be altered without breaking Git/SHA-256 anchors.
HISTORICAL_SEALED_PATH_ALLOWLIST: Set[str] = {
    "campaign2_launch_lock.json",
    "campaign_archive_manifest.json",
    "campaign3/historical_roots_initial_snapshot.json",
    "campaign3/plan_v1_3_adoption.json",
    "campaign3/project_plan_v1_3.md",
    "campaign3/root_discovery.json",
    "campaign3/root_registry.json",
    "history/campaign2_launch_checklist.json",
    "history/legacy/best_manifest.json.archive_4a68892a14e74064998a9cf01b9710c6.backup",
    "history/run_1786134848_cfb514d2/campaign2_attempt_identity.json",
    "history/run_1786134848_cfb514d2/campaign2_pilot_report.json",
}

LIVE_SECRET_PATTERNS: List[Tuple[str, re.Pattern[str]]] = [
    ("OpenAI Project Key", re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}")),
    ("Anthropic API Key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("GitHub Personal Access Token", re.compile(r"ghp_[A-Za-z0-9]{30,}")),
    ("GitHub Fine-Grained Token", re.compile(r"github_pat_[A-Za-z0-9_]{30,}")),
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("AWS Access Key ID", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("Private Key PEM Header", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
]


def run_hygiene_checks(workspace_root: str = BASE_DIR) -> Tuple[bool, List[str]]:
    errors: List[str] = []

    allowlist_path = os.path.join(workspace_root, "RELEASE_ALLOWLIST.json")
    if not os.path.isfile(allowlist_path):
        return False, ["Missing RELEASE_ALLOWLIST.json"]

    with open(allowlist_path, "r", encoding="utf-8") as f:
        allowlist_data: Dict = json.load(f)

    allowed_root_files = set(allowlist_data.get("allowed_root_files", []))
    allowed_top_dirs = set(allowlist_data.get("allowed_top_level_directories", []))
    excluded_outer = set(
        allowlist_data.get("source_repository_lineage", {}).get("excluded_outer_workspace_artifacts", [])
    )

    # 1. Verify LICENSE and THIRD_PARTY_NOTICES.md
    for req_file in ("LICENSE", "THIRD_PARTY_NOTICES.md", "RELEASE_ALLOWLIST.json"):
        abs_p = os.path.join(workspace_root, req_file)
        if not os.path.isfile(abs_p):
            errors.append(f"Missing required release file: {req_file}")

    license_path = os.path.join(workspace_root, "LICENSE")
    if os.path.isfile(license_path):
        with open(license_path, "r", encoding="utf-8") as f:
            lic_text = f.read()
        if "MIT License" not in lic_text:
            errors.append("LICENSE does not contain expected 'MIT License' header")

    # 2. Verify excluded outer workspace artifacts are absent
    for excl in excluded_outer:
        cleaned = excl.rstrip("/")
        if os.path.lexists(os.path.join(workspace_root, cleaned)):
            errors.append(f"Excluded outer-workspace artifact present in release checkout: {excl}")

    # 3. Inspect Git-tracked files against allowlist
    tracked_out = subprocess.check_output(
        ["git", "ls-files"], cwd=workspace_root, text=True
    ).strip()
    tracked_files = [line for line in tracked_out.splitlines() if line]

    for rel_p in tracked_files:
        if "/" not in rel_p:
            if rel_p not in allowed_root_files:
                errors.append(f"Tracked root file not in RELEASE_ALLOWLIST.json: {rel_p}")
        else:
            top_dir = rel_p.split("/", 1)[0]
            if top_dir not in allowed_top_dirs:
                errors.append(f"Tracked top-level directory not in RELEASE_ALLOWLIST.json: {top_dir} ({rel_p})")

        abs_p = os.path.join(workspace_root, rel_p)
        if not os.path.isfile(abs_p):
            continue

        try:
            with open(abs_p, "r", encoding="utf-8") as f:
                content = f.read()
        except UnicodeDecodeError:
            errors.append(f"Tracked file is not valid UTF-8 text: {rel_p}")
            continue

        # 4. Check for maintainer-local /Users/ paths outside sealed historical records
        if "/Users/" in content and rel_p not in HISTORICAL_SEALED_PATH_ALLOWLIST:
            # Allow hygiene script/test themselves to reference the literal token "/Users/"
            if rel_p not in ("scripts/check_release_hygiene.py", "test_release_hygiene.py"):
                errors.append(f"Unsealed tracked file contains local '/Users/' path: {rel_p}")

        # 5. Check for live secret patterns
        for label, pattern in LIVE_SECRET_PATTERNS:
            if pattern.search(content):
                errors.append(f"Potential live credential ({label}) detected in {rel_p}")

    return len(errors) == 0, errors


def main() -> int:
    ok, errors = run_hygiene_checks(BASE_DIR)
    if not ok:
        sys.stderr.write("FATAL: Release hygiene verification failed:\n")
        for err in errors:
            sys.stderr.write(f"  - {err}\n")
        return 1
    sys.stdout.write("Release hygiene verification passed: allowlist, license, and metadata boundaries clean.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

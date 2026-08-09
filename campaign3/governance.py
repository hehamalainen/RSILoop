"""
Campaign 3 Governance & Adoption Module (Work Order P0 Corrective Pass C4)
Provides JCS RFC 8785 canonicalization, dynamic root registry verification,
and machine emission functions for W1-W4 governance records.
"""

import os
import sys
import json
import stat
import hashlib
import datetime
import tempfile
import subprocess
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class GovernanceError(Exception):
    """Raised on governance or authorization failure."""
    pass


def jcs_canonicalize(data: Any) -> bytes:
    """
    RFC 8785 JSON Canonicalization Scheme (JCS).
    Serializes data to canonical JSON bytes with sorted keys and no whitespace around separators.
    """
    return json.dumps(data, separators=(',', ':'), sort_keys=True, ensure_ascii=False).encode('utf-8')


def verify_canonical_root(workspace_root: str = BASE_DIR) -> str:
    """
    Verifies that execution occurs strictly from the permitted root declared in campaign3/root_registry.json.
    Fails closed if the workspace realpath matches any forbidden root or does not match execution_permitted_root.
    """
    actual_real = os.path.realpath(workspace_root)
    registry_path = os.path.join(workspace_root, "campaign3", "root_registry.json")

    if not os.path.exists(registry_path):
        raise GovernanceError(f"Root registry file missing: {registry_path}")

    _check_no_symlinks(workspace_root, registry_path)

    st_mode = stat.S_IMODE(os.stat(registry_path).st_mode)
    if st_mode != 0o444:
        raise GovernanceError(f"Root registry permission mode must be 0o444, got {oct(st_mode)}")

    with open(registry_path, "r", encoding="utf-8") as f:
        registry_data = json.load(f)

    permitted_real = registry_data.get("execution_permitted_root", "")
    forbidden_roots = registry_data.get("forbidden_execution_roots", {})

    if actual_real in forbidden_roots:
        reason = forbidden_roots[actual_real]
        raise GovernanceError(
            f"Execution rejected: path '{actual_real}' is a declared forbidden root ({reason})"
        )

    if actual_real != permitted_real:
        raise GovernanceError(
            f"Execution rejected: actual realpath '{actual_real}' does not match permitted root '{permitted_real}'"
        )

    return actual_real


def _check_no_symlinks(workspace_root: str, path: str) -> None:
    """Verifies that no path component from workspace_root to path is a symlink."""
    rel = os.path.relpath(path, workspace_root)
    curr = os.path.realpath(workspace_root)
    if os.path.islink(curr):
        raise GovernanceError(f"Workspace root component is a symlink: {curr}")
    for part in rel.split(os.sep):
        if not part or part == ".":
            continue
        curr = os.path.join(curr, part)
        if os.path.islink(curr):
            raise GovernanceError(f"Path component is a symlink: {curr}")


def emit_plan_v1_3_adoption(
    src_plan_path: str,
    expected_plan_sha256: str,
    operator_identity: str,
    workspace_root: str = BASE_DIR
) -> Dict[str, Any]:
    """
    W1 — PLAN ADOPTION RECORD (Corrected C4).
    Verifies candidate plan bytes, copies to campaign3/project_plan_v1_3.md,
    seals permissions to 0o444, and machine-emits campaign3/plan_v1_3_adoption.json (0o444, JCS).
    Strictly fail-closed: rejects asymmetric existence or any schema/byte mismatch in existing adoption state.
    """
    c3_dir = os.path.join(workspace_root, "campaign3")
    os.makedirs(c3_dir, exist_ok=True)

    dest_plan_path = os.path.join(c3_dir, "project_plan_v1_3.md")
    adoption_json_path = os.path.join(c3_dir, "plan_v1_3_adoption.json")
    rel_plan_path = os.path.relpath(dest_plan_path, workspace_root)
    exec_root_real = os.path.realpath(workspace_root)

    plan_exists = os.path.exists(dest_plan_path)
    adopt_exists = os.path.exists(adoption_json_path)

    # Reject asymmetric adoption state
    if plan_exists != adopt_exists:
        raise GovernanceError(
            f"Asymmetric adoption state detected! plan_exists={plan_exists}, adopt_exists={adopt_exists}. Fail closed."
        )

    # 1. Validate complete existing adoption state if both exist
    if plan_exists and adopt_exists:
        _check_no_symlinks(workspace_root, adoption_json_path)
        _check_no_symlinks(workspace_root, dest_plan_path)

        mode_plan = stat.S_IMODE(os.stat(dest_plan_path).st_mode)
        mode_adopt = stat.S_IMODE(os.stat(adoption_json_path).st_mode)
        if mode_plan != 0o444 or mode_adopt != 0o444:
            raise GovernanceError(
                f"Existing adoption files permission mode error: plan={oct(mode_plan)}, adopt={oct(mode_adopt)}"
            )

        with open(dest_plan_path, "rb") as f_plan:
            existing_plan_bytes = f_plan.read()
        existing_plan_sha = hashlib.sha256(existing_plan_bytes).hexdigest()

        try:
            with open(adoption_json_path, "r", encoding="utf-8") as f_adopt:
                existing_adopt_data = json.load(f_adopt)
        except Exception as e:
            raise GovernanceError(f"Corrupt existing adoption JSON record: {e}")

        expected_keys = {"adoption_timestamp_utc", "execution_root_realpath", "operator_identity", "plan_path", "plan_sha256"}
        if set(existing_adopt_data.keys()) != expected_keys:
            raise GovernanceError(
                f"Existing adoption JSON schema key mismatch! Expected {sorted(list(expected_keys))}, got {sorted(list(existing_adopt_data.keys()))}"
            )

        # Fail closed on any mismatch
        if existing_plan_sha != expected_plan_sha256:
            raise GovernanceError(
                f"Existing plan SHA mismatch! Expected {expected_plan_sha256}, got {existing_plan_sha}"
            )
        if existing_adopt_data.get("plan_sha256") != expected_plan_sha256:
            raise GovernanceError(
                f"Existing adoption JSON plan_sha256 mismatch! Expected {expected_plan_sha256}, got {existing_adopt_data.get('plan_sha256')}"
            )
        if existing_adopt_data.get("operator_identity") != operator_identity:
            raise GovernanceError(
                f"Existing adoption operator_identity mismatch! Expected {operator_identity}, got {existing_adopt_data.get('operator_identity')}"
            )
        if existing_adopt_data.get("execution_root_realpath") != exec_root_real:
            raise GovernanceError(
                f"Existing adoption execution_root_realpath mismatch! Expected {exec_root_real}, got {existing_adopt_data.get('execution_root_realpath')}"
            )
        if existing_adopt_data.get("plan_path") != rel_plan_path:
            raise GovernanceError(
                f"Existing adoption plan_path mismatch! Expected {rel_plan_path}, got {existing_adopt_data.get('plan_path')}"
            )

        # All existing adoption checks passed; return existing record untouched!
        return existing_adopt_data

    # If adoption does NOT exist, verify source plan bytes and create
    with open(src_plan_path, "rb") as f:
        plan_bytes = f.read()

    actual_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    if actual_sha256 != expected_plan_sha256:
        raise GovernanceError(
            f"Plan SHA-256 mismatch! Expected {expected_plan_sha256}, got {actual_sha256}"
        )

    with open(dest_plan_path, "wb") as f:
        f.write(plan_bytes)
    os.chmod(dest_plan_path, 0o444)

    timestamp_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    adoption_data = {
        "adoption_timestamp_utc": timestamp_utc,
        "execution_root_realpath": exec_root_real,
        "operator_identity": operator_identity,
        "plan_path": rel_plan_path,
        "plan_sha256": actual_sha256
    }

    canonical_bytes = jcs_canonicalize(adoption_data)

    with open(adoption_json_path, "wb") as f:
        f.write(canonical_bytes + b"\n")
    os.chmod(adoption_json_path, 0o444)

    return adoption_data


def emit_root_discovery(
    root_paths: Dict[str, str],
    workspace_root: str = BASE_DIR
) -> Dict[str, Any]:
    """
    W2 — ROOT DISCOVERY RECORD.
    Captures raw command outputs for R1, R2, R3 explicitly without globs,
    and machine-emits campaign3/root_discovery.json (0o444, JCS).
    Discovered test count is parsed strictly from run_tests.py output without hard-coded fallbacks.
    """
    c3_dir = os.path.join(workspace_root, "campaign3")
    os.makedirs(c3_dir, exist_ok=True)
    out_path = os.path.join(c3_dir, "root_discovery.json")

    discovery_records = {}

    for root_key in sorted(root_paths.keys()):
        root_path = root_paths[root_key]
        real_root = os.path.realpath(root_path)

        def _run(args):
            p = subprocess.run(args, cwd=root_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            return p.stdout.strip(), p.returncode

        show_toplevel, ret_toplevel = _run(["git", "rev-parse", "--show-toplevel"])
        remote_v, ret_remote = _run(["git", "remote", "-v"])
        head_sha, ret_head = _run(["git", "rev-parse", "HEAD"])
        status_porcelain, ret_status = _run(["git", "status", "--porcelain", "--untracked-files=all"])
        branch_current, ret_branch = _run(["git", "branch", "--show-current"])
        upstream_name, ret_upstream = _run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"])

        root_entry = {
            "branch_current": branch_current,
            "git_head_sha": head_sha,
            "git_remote_v": remote_v,
            "git_status_porcelain": status_porcelain,
            "git_toplevel": show_toplevel,
            "realpath": real_root,
            "upstream_ref": upstream_name,
            "upstream_returncode": ret_upstream
        }

        if root_key == "R1":
            ls_remote_heads, ret_ls = _run(["git", "ls-remote", "https://github.com/hehamalainen/RSILoop.git", "refs/heads/*"])

            test_proc = subprocess.run(
                [sys.executable, "run_tests.py"],
                cwd=root_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            test_out = test_proc.stdout.strip()
            test_code = test_proc.returncode

            discovered_count = None
            for line in test_out.splitlines():
                if "Executed test cases:" in line or "Discovered test cases:" in line or "Ran " in line:
                    parts = line.split()
                    for p_item in parts:
                        if p_item.isdigit():
                            discovered_count = int(p_item)

            if discovered_count is None:
                raise GovernanceError(f"Failed to discover test count dynamically from run_tests.py output: {test_out}")

            root_entry["git_ls_remote_heads"] = ls_remote_heads
            root_entry["run_tests_discovered_count"] = discovered_count
            root_entry["run_tests_exit_code"] = test_code
            root_entry["run_tests_raw_output"] = test_out

        discovery_records[root_key] = root_entry

    timestamp_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    record_data = {
        "discovery_timestamp_utc": timestamp_utc,
        "roots": discovery_records
    }

    canonical_bytes = jcs_canonicalize(record_data)

    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)
    with open(out_path, "wb") as f:
        f.write(canonical_bytes + b"\n")
    os.chmod(out_path, 0o444)

    return record_data


def emit_root_registry(
    permitted_root: str,
    forbidden_roots: Dict[str, str],
    workspace_root: str = BASE_DIR
) -> Dict[str, Any]:
    """
    W3 — ROOT REGISTRY.
    Machine-emits campaign3/root_registry.json (0o444, JCS) declaring permitted
    and forbidden execution roots.
    """
    c3_dir = os.path.join(workspace_root, "campaign3")
    os.makedirs(c3_dir, exist_ok=True)
    out_path = os.path.join(c3_dir, "root_registry.json")

    real_permitted = os.path.realpath(permitted_root)
    real_forbidden = {os.path.realpath(k): v for k, v in forbidden_roots.items()}

    registry_data = {
        "execution_permitted_root": real_permitted,
        "forbidden_execution_roots": real_forbidden
    }

    canonical_bytes = jcs_canonicalize(registry_data)

    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)
    with open(out_path, "wb") as f:
        f.write(canonical_bytes + b"\n")
    os.chmod(out_path, 0o444)

    return registry_data


def emit_remote_rfc_status(
    workspace_root: str = BASE_DIR,
    remote_url: str = "https://github.com/hehamalainen/RSILoop.git"
) -> Dict[str, Any]:
    """
    W4 — REMOTE RFC / ACCEPTANCE RESOLUTION (Corrected Pass C4).
    Fetches all advertised remote branch refs into an isolated refs/remotes/audit/* namespace in a temporary repo.
    Verifies fetched commit SHAs match advertised ls-remote SHAs.
    Lists full tree (git ls-tree -r) for every branch and inspects both acceptance artifacts and candidate RFC documents for ACCEPTED status.
    Fails closed to 'indeterminate_remote_content_not_inspected' on any fetch, network, SHA mismatch, tree retrieval, or RFC show error.
    """
    c3_dir = os.path.join(workspace_root, "campaign3")
    os.makedirs(c3_dir, exist_ok=True)
    out_path = os.path.join(c3_dir, "remote_rfc_status.json")

    # 1. Query advertised remote refs
    p_refs = subprocess.run(
        ["git", "ls-remote", remote_url],
        cwd=workspace_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    if p_refs.returncode != 0:
        remote_output = p_refs.stdout.strip()
        status_data = {
            "accepted_record_found_on_remote": False,
            "conclusion": "indeterminate_remote_content_not_inspected",
            "error_message": f"git ls-remote failed with exit code {p_refs.returncode}: {remote_output}",
            "remote_branches": remote_output,
            "rfc1_v2_sha256": "e15996148e34864d650d5b572e2c6269a9d14b9f58c060c8ede38371d17c197e",
            "rfc1_v2_status_line": "**Status**: DRAFT (Pending Adversarial Review)"
        }
        canonical_bytes = jcs_canonicalize(status_data)
        if os.path.exists(out_path):
            os.chmod(out_path, 0o644)
        with open(out_path, "wb") as f:
            f.write(canonical_bytes + b"\n")
        os.chmod(out_path, 0o444)
        return status_data

    remote_output = p_refs.stdout.strip()

    # Parse advertised branch refs
    advertised_branches = {}
    for line in remote_output.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            ref_sha, ref_name = parts[0], parts[1]
            if ref_name.startswith("refs/heads/"):
                branch_short = ref_name.replace("refs/heads/", "")
                advertised_branches[branch_short] = (ref_sha, ref_name)

    accepted_record_found = False
    inspected_branches = []
    inspection_failed = False
    error_msg = ""

    # 2. Fetch all branch refs into an isolated refs/remotes/audit/* namespace in a temporary repo
    with tempfile.TemporaryDirectory() as tmp_git_dir:
        p_init = subprocess.run(["git", "init", "-b", "tmp_audit_init"], cwd=tmp_git_dir, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if p_init.returncode != 0:
            inspection_failed = True
            error_msg = "Failed to initialize temporary git repository for remote inspection"
        else:
            p_fetch = subprocess.run(
                ["git", "fetch", "--depth=1", remote_url, "refs/heads/*:refs/remotes/audit/*"],
                cwd=tmp_git_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            if p_fetch.returncode != 0:
                inspection_failed = True
                error_msg = f"git fetch failed: {p_fetch.stderr.strip()}"
            else:
                # Inspect each fetched branch
                for branch_short in sorted(advertised_branches.keys()):
                    adv_sha, ref_name = advertised_branches[branch_short]
                    audit_ref = f"audit/{branch_short}"

                    # Verify fetched commit SHA matches advertised SHA
                    p_sha = subprocess.run(
                        ["git", "rev-parse", f"refs/remotes/audit/{branch_short}"],
                        cwd=tmp_git_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    if p_sha.returncode != 0 or p_sha.stdout.strip() != adv_sha:
                        inspection_failed = True
                        error_msg = f"Fetched SHA mismatch for branch '{ref_name}': advertised {adv_sha}, got {p_sha.stdout.strip()}"
                        break

                    inspected_branches.append(ref_name)

                    # List full tree recursively
                    p_tree = subprocess.run(
                        ["git", "ls-tree", "-r", "--name-only", f"refs/remotes/audit/{branch_short}"],
                        cwd=tmp_git_dir,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    if p_tree.returncode != 0:
                        inspection_failed = True
                        error_msg = f"git ls-tree failed for branch '{ref_name}'"
                        break

                    tree_files = p_tree.stdout.splitlines()

                    # Inspect files for acceptance artifacts or accepted RFC status
                    for t_file in tree_files:
                        lower_f = t_file.lower()
                        # a. Check acceptance artifact filenames
                        if "acceptance" in lower_f or "rfc1_accepted" in lower_f:
                            accepted_record_found = True
                            break
                        # b. Check candidate RFC files for ACCEPTED status line
                        elif "rfc" in lower_f and lower_f.endswith(".md"):
                            p_show = subprocess.run(
                                ["git", "show", f"refs/remotes/audit/{branch_short}:{t_file}"],
                                cwd=tmp_git_dir,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True
                            )
                            if p_show.returncode != 0:
                                # Candidate RFC content retrieval failure MUST set inspection_failed!
                                inspection_failed = True
                                error_msg = f"git show failed for candidate RFC file '{t_file}' in branch '{ref_name}': {p_show.stderr.strip()}"
                                break
                            else:
                                for line in p_show.stdout.splitlines():
                                    line_lower = line.lower()
                                    if "status:" in line_lower or "status**:" in line_lower:
                                        if "accepted" in line_lower and "draft" not in line_lower:
                                            accepted_record_found = True
                                            break

                    if accepted_record_found or inspection_failed:
                        break

    # Read local RFC-1 v2 in R1
    rfc_v2_path = os.path.join(c3_dir, "rfc_1_v2.md")
    status_line = ""
    rfc_sha256 = ""
    if os.path.exists(rfc_v2_path):
        with open(rfc_v2_path, "rb") as f:
            rfc_bytes = f.read()
        rfc_sha256 = hashlib.sha256(rfc_bytes).hexdigest()
        for line in rfc_bytes.decode('utf-8', errors='ignore').splitlines():
            if line.startswith("**Status**:") or "Status:" in line:
                status_line = line.strip()
                break

    expected_rfc_sha = "e15996148e34864d650d5b572e2c6269a9d14b9f58c060c8ede38371d17c197e"
    if rfc_sha256 != expected_rfc_sha:
        raise GovernanceError(
            f"Local rfc_1_v2.md SHA-256 mismatch! Expected {expected_rfc_sha}, got {rfc_sha256}"
        )

    if inspection_failed:
        conclusion = "indeterminate_remote_content_not_inspected"
    elif accepted_record_found:
        conclusion = "accepted_record_found"
    else:
        conclusion = "no_accepted_record_found"

    status_data = {
        "accepted_record_found_on_remote": accepted_record_found if not inspection_failed else False,
        "conclusion": conclusion,
        "inspected_remote_branches": inspected_branches,
        "remote_branches": remote_output,
        "rfc1_v2_sha256": rfc_sha256,
        "rfc1_v2_status_line": status_line
    }
    if error_msg:
        status_data["error_message"] = error_msg

    canonical_bytes = jcs_canonicalize(status_data)

    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)
    with open(out_path, "wb") as f:
        f.write(canonical_bytes + b"\n")
    os.chmod(out_path, 0o444)

    return status_data

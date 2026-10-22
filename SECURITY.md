# Security Policy

## Supported Versions

RSILoop is published as a research prototype for studying containment-hardened evaluation and statistical design of Recursive Self-Improvement (RSI) loops. Security updates apply to the active public release branch (`release/public-prototype-rc1` / `main`).

| Version | Supported |
|---|---|
| `0.3.x` (`release/public-prototype-rc1` / `main`) | Yes |
| Historical archive snapshots (`< 0.3.0`) | No (retained read-only for reproducibility) |

---

## Reporting a Vulnerability Privately

If you discover a security vulnerability—particularly a sandbox escape in `candidate.sb.template`, `generator.sb.template`, or `campaign2/proxy.py`, an IPC privilege escalation between the sandboxed worker and host supervisor, or unintended credential exposure—**please do not open a public GitHub issue**.

Instead, report the vulnerability privately using **GitHub Private Vulnerability Reporting**:

1. Navigate to the repository's **Security** tab on GitHub:
   `https://github.com/hehamalainen/RSILoop/security/advisories/new`
2. Provide a minimal, self-contained reproduction script or proof-of-concept demonstrating the boundary violation, along with your macOS version (`sw_vers`) and Python version.
3. Never test against live third-party credentials or non-synthetic user data; use a synthetic sentinel file or local loopback listener in your reproduction.

 We aim to acknowledge private reports within **7 days** and coordinate remediation andregression test coverage before public disclosure.

---

## Scope & Known Platform Limitations

- **macOS `sandbox-exec` Containment**: The candidate evaluation boundary relies on macOS Seatbelt profiles (`/usr/bin/sandbox-exec`) and `setrlimit`. Bypass of filesystem read/write restrictions (such as reading repository files or unrelated temporary files under `/private/var/folders`) or outbound network restrictions is **in scope**.
- **Kernel Address-Space Ceiling (`RLIMIT_AS`)**: As documented in `README.md`, the macOS XNU kernel does not enforce `RLIMIT_AS` via `setrlimit`. Memory exhaustion by a candidate process before wall-clock timeout termination (`CandidateTimeoutError`) is a known OS-level availability limitation rather than a sandbox-policy bypass.

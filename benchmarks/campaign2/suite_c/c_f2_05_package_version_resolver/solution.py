def resolve_package_versions(required: dict[str, str], available: dict[str, list[str]]) -> dict[str, str]:
    if not required or not available: return {}
    res = {}
    for pkg, req_ver in required.items():
        if pkg not in available or not available[pkg]:
            return {}
        if req_ver in ("*", "latest"):
            res[pkg] = available[pkg][0]
        else:
            if req_ver in available[pkg]:
                res[pkg] = req_ver
            else:
                res[pkg] = available[pkg][0]
    return res

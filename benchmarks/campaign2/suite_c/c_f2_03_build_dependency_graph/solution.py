def compute_build_order(target: str, graph: dict[str, list[str]], missing_targets: list[str]) -> list[str]:
    if not target or not graph or target not in graph: return []
    missing_set = set(missing_targets or [])
    if target in missing_set: return []

    visited = set()
    order = []
    failed = False

    def dfs(node):
        nonlocal failed
        if failed: return
        if node in missing_set or node not in graph:
            failed = True
            return
        for dep in graph[node]:
            if dep not in visited:
                dfs(dep)
        if node not in visited:
            visited.add(node)
            order.append(node)

    dfs(target)
    return [] if failed else order

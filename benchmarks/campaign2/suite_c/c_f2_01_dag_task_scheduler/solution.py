def schedule_dag_tasks(tasks: list[dict], max_workers: int) -> list[list[str]]:
    if not tasks or max_workers <= 0: return []
    in_degree = {t["id"]: len(t.get("deps", [])) for t in tasks}
    graph = {t["id"]: [] for t in tasks}
    for t in tasks:
        for dep in t.get("deps", []):
            if dep in graph:
                graph[dep].append(t["id"])

    stages = []
    scheduled = 0
    total = len(tasks)

    while scheduled < total:
        ready = sorted([tid for tid, deg in in_degree.items() if deg == 0])
        if not ready: raise ValueError("Cycle detected in DAG")
        stage = ready[:max_workers]
        stages.append(stage)
        for tid in stage:
            in_degree[tid] = -1
            scheduled += 1
            for child in graph[tid]:
                if in_degree[child] > 0:
                    in_degree[child] -= 1
    return stages

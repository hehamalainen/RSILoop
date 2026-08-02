def schedule_pods(pods: list[dict], nodes: list[dict]) -> dict[str, str]:
    if not pods or not nodes: return {}
    assignments = {}
    node_state = []
    for n in nodes:
        node_state.append({
            "name": n["name"],
            "cpu": n["cpu"],
            "mem": n["mem"],
            "taints": set(n.get("taints", [])),
            "labels": n.get("labels", {})
        })

    for pod in pods:
        p_id = pod["id"]
        req_cpu = pod["cpu"]
        req_mem = pod["mem"]
        tolerations = set(pod.get("tolerations", []))
        selector = pod.get("selector", {})

        assigned_node = None
        for n in node_state:
            if n["cpu"] < req_cpu or n["mem"] < req_mem: continue
            if not n["taints"].issubset(tolerations): continue
            if any(n["labels"].get(k) != v for k, v in selector.items()): continue

            assigned_node = n["name"]
            n["cpu"] -= req_cpu
            n["mem"] -= req_mem
            break

        if assigned_node:
            assignments[p_id] = assigned_node

    return assignments

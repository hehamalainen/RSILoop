def plan_resource_batches(jobs: list[dict], max_cpu: int, max_ram: int) -> list[list[str]]:
    if not jobs or max_cpu <= 0 or max_ram <= 0: return []
    batches = []
    curr_batch = []
    curr_cpu, curr_ram = 0, 0
    for j in jobs:
        j_id = j["id"]
        j_cpu = j.get("cpu", 1)
        j_ram = j.get("ram", 1)
        if curr_batch and (curr_cpu + j_cpu > max_cpu or curr_ram + j_ram > max_ram):
            batches.append(curr_batch)
            curr_batch = [j_id]
            curr_cpu, curr_ram = j_cpu, j_ram
        else:
            curr_batch.append(j_id)
            curr_cpu += j_cpu
            curr_ram += j_ram
    if curr_batch:
        batches.append(curr_batch)
    return batches

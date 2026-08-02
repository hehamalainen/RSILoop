def schedule_pipeline(stages: list[dict], worker_pools: dict[str, int]) -> list[dict]:
    if not stages or not worker_pools: return []
    schedule = []
    clock = 0
    for stage_idx, stage in enumerate(stages):
        stage_tasks = stage.get("tasks", [])
        stage_start = clock
        pool_next = {pool: stage_start for pool in worker_pools}
        stage_max_end = stage_start
        for task in stage_tasks:
            t_id = task["id"]
            pool = task["pool"]
            dur = task.get("duration", 1)
            if pool not in worker_pools or worker_pools[pool] <= 0: continue
            start_time = pool_next[pool]
            end_time = start_time + dur
            pool_next[pool] = end_time
            stage_max_end = max(stage_max_end, end_time)
            schedule.append({"task_id": t_id, "stage": stage_idx, "start": start_time, "end": end_time})
        clock = stage_max_end
    return schedule

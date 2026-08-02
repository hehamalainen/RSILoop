def execute_workflow(tasks: list[dict], context: dict) -> dict:
    if not tasks: return context
    ctx = dict(context)
    for task in tasks:
        fn = task.get("func")
        on_fail = task.get("on_failure", "abort")
        try:
            if fn:
                res = fn(ctx)
                if isinstance(res, dict): ctx.update(res)
        except Exception as e:
            if on_fail == "continue": continue
            elif on_fail == "abort": break
    return ctx

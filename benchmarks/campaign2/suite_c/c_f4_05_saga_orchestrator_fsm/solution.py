class SagaOrchestrator:
    def __init__(self, steps: list[dict]):
        self.steps = steps

    def execute(self) -> dict:
        executed = []
        compensated = []
        for idx, step in enumerate(self.steps):
            name = step["name"]
            action = step.get("action")
            try:
                if action and action() is True:
                    executed.append(name)
                else:
                    raise RuntimeError(f"Step {name} failed")
            except Exception:
                for comp_step in reversed(self.steps[:idx]):
                    comp_name = comp_step["name"]
                    compensate_fn = comp_step.get("compensate")
                    if compensate_fn:
                        try: compensate_fn()
                        except Exception: pass
                    compensated.append(comp_name)
                return {"status": "COMPENSATED", "failed_at": name, "executed": executed, "compensated": compensated}
        return {"status": "SUCCESS", "executed": executed, "compensated": []}

def create_saga_orchestrator(steps: list[dict]): return SagaOrchestrator(steps)

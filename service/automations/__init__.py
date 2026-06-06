"""service/automations/ — P4 automation plugins, ONE file per automation.

Each module defines an `async def (account_id, payload, *, run_id) -> dict` and
registers it with `@register("<kind>")` from `automation_registry`. The executor
auto-imports every module in this package at startup
(`automation_registry.load_automation_plugins`), so dropping in a new file is all
it takes to add an automation — no shared registry edit, no merge collision when
several P4 automations are built in parallel.

Example — service/automations/gen_info.py::

    from automation_registry import register

    @register("gen_info")
    async def run(account_id: str, payload: dict, *, run_id: int) -> dict:
        # of_client only (no DOM); its OWN AsyncSession; llm_client for text;
        # acquire_fan_lease / account_spend_lock from automation_executor before
        # any send. Return a stats dict (lands in automation_runs.stats_json).
        ...
        return {"fans_processed": n}

The reference automation `scrape_chats` is decorated inline in
automation_executor.py (not here) to prove the pattern.
"""

"""service/automation_registry.py — the automation registry seam.

P4 automations live in their OWN file under `service/automations/<name>.py` and
self-register here with `@register("<kind>")`, so adding an automation NEVER
edits a shared file — several can be built in parallel terminals with no merge
collision on a central dict.

Kept in its own tiny module (NOT automation_executor.py) on purpose: a plugin
can `from automation_registry import register` without importing the heavy
executor, so there is no circular import. The executor imports THIS module, not
the other way around.

An automation is::

    async def run(account_id: str, payload: dict, *, run_id: int) -> dict

— the exact shape `automation_executor.run_once` calls. It uses of_client (no
DOM), its OWN AsyncSession, the lease/lock primitives in automation_executor,
and — if it generates text — llm_client. The reference automation
(`scrape_chats`) is decorated inline in automation_executor.py to prove the
pattern without needing a plugin file.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Awaitable, Callable

log = logging.getLogger("of-relay.automation.registry")

# kind -> automation coroutine: async def (account_id, payload, *, run_id) -> dict
Automation = Callable[..., Awaitable[dict]]
_REGISTRY: dict[str, Automation] = {}


def register(kind: str) -> Callable[[Automation], Automation]:
    """Decorator: register an automation coroutine under `kind`.

    Re-registering the same kind with a DIFFERENT function is logged (last
    writer wins) so a duplicate kind is visible, never silent.
    """
    def deco(fn: Automation) -> Automation:
        prev = _REGISTRY.get(kind)
        if prev is not None and prev is not fn:
            log.warning(
                "automation_kind_reregistered kind=%s old=%s new=%s",
                kind, getattr(prev, "__name__", prev), getattr(fn, "__name__", fn),
            )
        _REGISTRY[kind] = fn
        return fn
    return deco


def get_automation(kind: str) -> Automation | None:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


_PLUGINS_LOADED = False


def load_automation_plugins() -> None:
    """Import every module under `service/automations/` so its `@register` call
    runs. Idempotent (once per process), and one broken plugin is logged and
    skipped — a single bad P4 file can't take down the executor.

    The reference automation (`scrape_chats`) registers by importing
    automation_executor itself, so this only adds the external P4 plugins.
    """
    global _PLUGINS_LOADED
    if _PLUGINS_LOADED:
        return
    _PLUGINS_LOADED = True
    try:
        import automations  # service/automations/__init__.py
    except Exception:
        log.warning("automations_package_import_failed", exc_info=True)
        return
    for mod in pkgutil.iter_modules(automations.__path__, automations.__name__ + "."):
        try:
            importlib.import_module(mod.name)
            log.info("automation_plugin_loaded module=%s", mod.name)
        except Exception:
            log.warning("automation_plugin_import_failed module=%s", mod.name, exc_info=True)

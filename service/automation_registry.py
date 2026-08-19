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

# Retired kind → its live replacement. THE one home for kind renames: dispatch
# (get_automation), the executor's priority + job claim, the rules/transfer
# APIs' validation, and _common's config-key fallbacks all resolve through
# this map, so prod rows/blobs still carrying the old token keep working
# until the rename migration rewrites them — and deleting the entry (after
# that migration has run on prod) retires every shim at once.
LEGACY_KINDS: dict[str, str] = {
    "of_ai_chat": "welcome_chatter_for_info",  # renamed 2026-08-19 (0062)
}


def canonical_kind(kind: str) -> str:
    """Resolve a (possibly retired) kind name to its live registry name."""
    return LEGACY_KINDS.get(kind, kind)


def legacy_names(kind: str) -> tuple[str, ...]:
    """The retired names that resolve to `kind` (usually empty)."""
    return tuple(old for old, new in LEGACY_KINDS.items() if new == kind)


def kind_family(kind: str) -> tuple[str, ...]:
    """The live name plus every retired alias — the full token set a stored
    job/run row may carry for this kind until the rename migration rewrites
    it. For QUERYING rows (job claim/dedupe/badges); writes always mint
    `canonical_kind` only."""
    live = canonical_kind(kind)
    return (live,) + legacy_names(live)


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
    return _REGISTRY.get(canonical_kind(kind))


def registered_kinds() -> list[str]:
    """The LIVE kinds only — legacy names resolve via get_automation but are
    never listed, so the rules-editor catalog can't offer a retired token."""
    return sorted(_REGISTRY)


def is_registered(kind: str) -> bool:
    """THE kind-validity predicate (legacy names count as their live kind).

    `scrape_chats` is known BY FIAT: it registers from automation_executor's
    own import, not a plugin file, so a process that never imports the heavy
    executor (the rules/transfer APIs) must still validate it."""
    load_automation_plugins()
    return canonical_kind(kind) in _REGISTRY or kind == "scrape_chats"


# Module basenames whose import RAISED in load_automation_plugins(). "This
# engine failed to load" and "this engine was retired" both look like an
# unregistered kind; the executor must treat them opposite ways (loud vs
# inert), and this set is what tells them apart.
_FAILED_PLUGINS: set[str] = set()


def failed_plugins() -> frozenset[str]:
    """Basenames of automation modules that crashed on import this process."""
    return frozenset(_FAILED_PLUGINS)


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
            _FAILED_PLUGINS.add(mod.name.rsplit(".", 1)[-1])
            log.warning("automation_plugin_import_failed module=%s", mod.name, exc_info=True)

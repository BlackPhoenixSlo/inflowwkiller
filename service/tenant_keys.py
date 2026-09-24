"""service/tenant_keys.py — one LLM provider key per AGENCY, not per box.

The invariant: two agencies never spend on one credential.

An agency is a `users` row; the OF creator accounts it owns are its
`user_accounts` links. Every LLM call carries an `account_id`, so the key for a
call is "the key belonging to whoever owns this account". `owners_of` answers
the ownership half, `get_key` the storage half, and `llm_client._tenant_api_key`
owns the policy that joins them (what a gap MEANS is a money decision, so it
lives on the money path). The HTTP surface for editing your own keys is
`tenant_keys_api`.

Pure store: no web imports, and the provider registry it validates against sits
in `llm_providers` rather than `llm_client`. Both facts are the same fact —
`llm_client` imports this module on every LLM call, including from background
automations, so the dependencies have to run one way and the money path has no
business dragging FastAPI and the session layer in behind it.

Deliberately NOT here, and not anywhere yet: per-creator-account keys,
encryption at rest, rotation UX, key-health probes, per-owner spend caps. The
daily cost cap stays per (account, provider) and is independent of whose key
paid — an agency on its own key is still throttled by its accounts'
`daily_cost_cap_cents`, which is theirs to raise.

Clearing a key empties its row rather than deleting it, so a cleared key and a
never-set one read the same everywhere — both are "no key", both fail closed.

An account with SEVERAL billable owners bills the ONE of them holding a key for
the provider being called, when exactly one does. It used to be refused
outright, and on this deployment that kept a live model silent for weeks over a
duplicate link nobody had noticed: one owner had every key, the other had
none, and the relay still would not pick. There is nothing to pick when only
one owner CAN be billed — the invariant (never spend on the wrong agency's
credential) is untouched. Two owners BOTH holding a key for the same provider is
still refused: that is the case where a pick really would bill someone silently.

NOTHING EVER COPIES THE HOUSE KEY INTO AN AGENCY ROW. An upgrade seeds nobody;
each owner pastes their own key once (DEPLOY.md → "Per-agency AI keys"). A
boot-time seeder that did it for them was written and deleted: every version of
it turned out to be a way to hand the deployment's credential to an account that
should not have it — through a date window, a provider added later, a value
planted in the UI-writable key store, or a malformed timestamp. There is no
mechanism here to get that wrong any more; please do not add one back.

NO CACHE, on purpose. `chat()` already makes several SQLite round-trips per call
and this adds two indexed reads; a TTL cache would buy microseconds and cost
correctness the moment an account is transferred between owners — the new
owner's traffic would go out on the old owner's key until the entry aged out,
and cross-tenant credential use for sixty seconds is still cross-tenant
credential use. The two reads stay two: "who owns this" and "what is their key"
are separate questions with separate callers, and fusing them into one join
would trade a legible pair for a clever one.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.engine import get_session
from db.models import Account, AccountHealth, User, UserAccount, UserLlmKey
from llm_providers import PROVIDERS
from secrets_store import MASK_CHAR, mask


# ── Store ─────────────────────────────────────────────────────────────────

def billable_owners(links: list[tuple[str, bool]]) -> list[str]:
    """THE rule for who could be billed for one account, from its
    `(identifier, is_master)` links. The list IS the answer and its length is
    the state: none (orphan — no tenant to leak across), one (bill them),
    several (bill the one of them holding a key for the provider, if exactly
    one does — see `llm_client._tenant_api_key`).

    One definition on purpose. It was written out three times — once on the
    money path, once for the "two owners" warning, once for the founder's
    roster — and the copies had already drifted: the roster did not treat two
    MASTER links as a dispute, while `llm_client` refuses them. A screen whose
    job is to predict what the money path will do cannot re-derive its rule.

    Generic in the first element so a caller with usernames gets the same
    answer as one with ids.
    """
    non_master = [ident for ident, is_master in links if not is_master]
    return non_master or [ident for ident, _ in links]


async def owners_of(account_id: str) -> list[str]:
    """The agencies that could be billed for one OF account, best first.

    Ownership is read from the EXISTING `user_accounts` join — this module
    introduces no second notion of who owns an account. The LIST is the answer,
    and its length is the state the caller branches on: none (an orphan account
    — there is no tenant, so the house key is not a leak), exactly one (bill
    them), or several (bill the only one of them with a key, else refuse).

    A MASTER (`users.is_admin`) is dropped whenever a non-master link exists.
    That is not a guess about intent, it is what this deployment's data says:
    every account here that has two owners has the same shape — the agency
    captured it first, and the master's link was added days or weeks later for
    oversight, never the other way round. (Three live accounts at the time of
    writing; the query that finds them is in DEPLOY.md → Per-agency AI keys, so
    a future reader can re-check the claim instead of trusting this sentence.)
    Treating the master as a co-owner would bill the FOUNDER for those agencies'
    traffic, which is precisely the leak this module exists to stop. Refusing
    them instead would take live accounts dark for a conflict nobody has.

    Two or more NON-master owners — two agencies both linked to one creator —
    is a conflict only when more than one of them holds a key for the provider
    being called. Then it is REFUSED, not tie-broken: neither row order nor
    link age says whose credential should pay, and picking one bills the wrong
    agency SILENTLY, which is the one outcome this module exists to prevent.
    When exactly ONE of them has a key there is nothing to pick and that owner
    pays (`llm_client._tenant_api_key`); refusing that case took a live model
    dark for weeks over a duplicate owner with no key at all. The Setup card
    names the account, its owners and who pays per provider; a real conflict is
    resolved by transferring the account to whichever of them should keep it.

    The shape this rule gets wrong: a founder granting a friend access to a
    model the FOUNDER runs would bill the friend. That does not occur here (the
    master is the later link in every case), and when it does the friend simply
    has no key, so it fails closed with a message naming the account rather than
    quietly spending anyone's money.
    """
    if not account_id:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(UserAccount.user_id, User.is_admin)
            .join(User, User.id == UserAccount.user_id)
            .where(UserAccount.account_id == account_id)
        )).all()
    return billable_owners(list(rows))


def payer_for(owners: list[str], keyed: dict[str, set[str]],
              provider: str) -> str | None:
    """THE rule for which of several owners bills one provider: the ONLY one
    holding a key for it. `None` when none does (nobody can pay — add a key)
    and when more than one does (a genuine dispute — the relay refuses).

    One definition, used by the money path and by both screens that predict
    it, for the same reason `billable_owners` is one definition: a card that
    explains a refusal must not be able to disagree about what gets refused.
    """
    holders = [o for o in owners if provider in keyed.get(o, ())]
    return holders[0] if len(holders) == 1 else None


async def keyed_providers(user_ids) -> dict[str, set[str]]:
    """`{user_id: {provider, …}}` for the owners given — which providers each
    has a NON-EMPTY key for. The money path asks this for one account's owners
    on every shared-account call; the cards ask it for a whole roster."""
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return {}
    async with get_session() as s:
        rows = (await s.execute(
            select(UserLlmKey.user_id, UserLlmKey.provider)
            .where(UserLlmKey.user_id.in_(ids),
                   func.trim(UserLlmKey.api_key) != "")
        )).all()
    out: dict[str, set[str]] = {}
    for uid, prov in rows:
        out.setdefault(uid, set()).add(prov)
    return out


async def shared_accounts(account_ids) -> list[dict]:
    """The accounts in `account_ids` that more than one agency is linked to,
    with who they are and, per provider, whose key pays.

    Literally the same rule as `owners_of` and `payer_for` — this card exists
    to explain what the money path will do, so it must not be able to disagree
    with it. A master's oversight link is dropped whenever an agency link
    exists and so is never reported as shared, or the card cries wolf on every
    account the founder can see.

    `pays` is `{provider: username}` for every provider EXACTLY ONE owner holds
    a key for — what the relay bills. `contested` lists the providers MORE than
    one owner has set: those calls are refused until a link is removed. A
    provider in neither is one nobody has set, and a key from either owner
    fixes it.

    Both ways sharing happens are silent (a grant, or a second signed-in user
    completing a session capture), so this is surfaced in Setup → Your AI keys
    next to the keys: it is where someone looks when a silence surprises them.
    """
    ids = [a for a in (account_ids or []) if a]
    if not ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(UserAccount.account_id, User.id, User.username,
                   User.is_admin, Account.nickname)
            .join(User, User.id == UserAccount.user_id)
            .outerjoin(Account, Account.id == UserAccount.account_id)
            .where(UserAccount.account_id.in_(ids))
        )).all()
    keyed = await keyed_providers({uid for _, uid, _, _, _ in rows})
    name_of = {uid: username for _, uid, username, _, _ in rows}

    by_account: dict[str, dict] = {}
    for account_id, uid, username, is_admin, nickname in rows:
        entry = by_account.setdefault(
            account_id,
            {"account_id": account_id, "nickname": nickname or account_id,
             "links": []},
        )
        entry["links"].append((uid, bool(is_admin)))
    out = []
    for e in by_account.values():
        billable = billable_owners(e.pop("links"))
        if len(billable) < 2:
            continue
        pays: dict[str, str] = {}
        contested: list[str] = []
        for prov in sorted(PROVIDERS):
            payer = payer_for(billable, keyed, prov)
            if payer:
                pays[prov] = name_of[payer]
            elif sum(prov in keyed.get(o, ()) for o in billable) > 1:
                contested.append(prov)
        e["owners"] = sorted(name_of[o] for o in billable)
        e["pays"] = pays
        e["contested"] = contested
        out.append(e)
    out.sort(key=lambda e: e["nickname"].lower())
    return out


async def agency_exists(user_id: str) -> bool:
    """Is there a `users` row with this id?

    The founder screens edit an agency BY ID, so the key endpoints have to ask
    this before they answer: an unknown id otherwise reads back as a
    well-formed agency with every provider unset, which is EXACTLY what a real
    agency that hasn't pasted a key looks like — and the write behind it dies on
    the `user_llm_keys` foreign key as a 500. `auth.admin_list_user_accounts`
    404s first for the same reason ("rather than an empty pair of lists that
    look like 'no accounts'"); this is that rule for keys.

    Lives in the store, not the API module, so the web layer keeps its single
    dependency and no database session of its own.
    """
    if not user_id:
        return False
    async with get_session() as s:
        return await s.scalar(
            select(User.id).where(User.id == user_id)
        ) is not None


async def key_overview(registry: dict[str, bool],
                       required_providers: set[str]) -> list[dict]:
    """Every agency, with the two facts that decide whether it needs a key:
    how many of its models can currently TALK, and which providers it has set.

    `registry` is the account registry — `{account_id: has_a_captured_session}`,
    exactly what `accounts.list_accounts()` reports. REQUIRED, and passed in
    rather than read here, for two reasons that both bite:

      • The registry is a DIRECTORY. This module is a pure store that the LLM
        money path imports on every call; the route owns the filesystem read.
      • Deleting a model removes its registry entry and leaves every database
        row behind — the `accounts` row, the `user_accounts` link, and a
        `sessions` row still flagged `is_latest`. Counting from the tables
        alone reported a model deleted on 08-14 as live on 08-20, and its
        owner was keyed for nothing. An account absent from the registry is
        gone: not live, and not owned either, or "1 live of 3 models" keeps
        quoting models the operator cannot see.

    The `sessions` TABLE is deliberately NOT consulted. Its only writer is
    `db/import_legacy.py`, a one-shot importer, so on this deployment it is
    frozen at 2026-08-14 and knows nothing about any session captured since.
    Live sessions live on disk — which is what the registry reads.

    A dead session still counts as OWNED: `accounts` is what the agency has,
    `live_accounts` is what can talk today. `account_health` supplies the death
    certificate, and that flag LAGS (a probe sets it, not the failure), so read
    a live count as "worth a key", never as health.

    Counts are BILLED, not linked, through `billable_owners` and `payer_for` —
    the same rules the money path resolves with, because a screen that predicts
    what the relay will do must not re-derive them. An account two agencies are
    both linked to is counted ONCE, under the one of them that pays for a
    required provider (or, when nobody has any key, under the first of them —
    the row a missing-key badge should light up). One that two keyed owners
    both claim bills nobody and would vanish silently, so it comes back as
    `blocked_accounts`: no key fixes those, the fix is a transfer or a revoke,
    and a keys screen is exactly where someone would otherwise paste one and
    wonder why nothing changed.

    `required_providers` is what a live model actually needs in order to reply,
    and it is supplied by the caller rather than inferred here. Without it the
    only honest statement this could make is "has SOME key", and credentials
    resolve PER PROVIDER: an agency holding only a DeepInfra key reads as
    configured while every chat reply it makes fails closed. `missing_providers`
    is the difference, and it is what the screen should act on.

    Providers are NAMES only, never values or hints — this feeds a list of many
    agencies at once, and the per-agency card is where a masked hint belongs.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(
                User.id, User.username, User.is_admin,
                UserAccount.account_id,
                AccountHealth.session_dead_at,
            )
            .join(UserAccount, UserAccount.user_id == User.id, isouter=True)
            .join(
                AccountHealth,
                AccountHealth.account_id == UserAccount.account_id,
                isouter=True,
            )
        )).all()

        keyed = (await s.execute(
            select(UserLlmKey.user_id, UserLlmKey.provider)
            .where(func.trim(UserLlmKey.api_key) != "")
        )).all()

    providers: dict[str, list[str]] = {}
    for uid, prov in keyed:
        providers.setdefault(uid, []).append(prov)

    out: dict[str, dict] = {}
    links: dict[tuple[str, str], bool] = {}
    owners_by_account: dict[str, list[tuple[str, bool]]] = {}
    for uid, username, is_admin, account_id, dead_at in rows:
        out.setdefault(uid, {
            "user_id": uid, "username": username, "is_admin": bool(is_admin),
            "accounts": 0, "live_accounts": 0, "blocked_accounts": 0,
            "providers_set": sorted(providers.get(uid, [])),
            "missing_providers": sorted(
                required_providers - set(providers.get(uid, []))),
        })
        if not account_id or account_id not in registry:
            continue
        links[(uid, account_id)] = registry[account_id] and dead_at is None
        owners = owners_by_account.setdefault(account_id, [])
        if (uid, bool(is_admin)) not in owners:
            owners.append((uid, bool(is_admin)))

    keyed = {u: set(p) for u, p in providers.items()}
    for (uid, account_id), is_live in links.items():
        billable = billable_owners(owners_by_account.get(account_id, []))
        if not billable:
            continue
        payer = billable[0]
        if len(billable) > 1:
            # Shared. A required provider two keyed owners both hold is a
            # dispute the relay refuses: reported against both, counted for
            # neither. Otherwise the sole holder of the first required
            # provider anyone has set pays (they may split providers between
            # them; the headline count picks one row, `shared_accounts` shows
            # the split), and with no key anywhere the first owner is who
            # needs to add one.
            contested = any(
                sum(prov in keyed.get(o, ()) for o in billable) > 1
                for prov in required_providers)
            if contested:
                if uid in billable and is_live:
                    out[uid]["blocked_accounts"] += 1
                continue
            payer = next(
                (payer_for(billable, keyed, prov)
                 for prov in sorted(required_providers)
                 if payer_for(billable, keyed, prov)),
                billable[0])
        if payer != uid:
            continue
        out[uid]["accounts"] += 1
        if is_live:
            out[uid]["live_accounts"] += 1

    return sorted(
        out.values(),
        key=lambda e: (-e["live_accounts"], -e["blocked_accounts"],
                       -e["accounts"], e["username"].lower()),
    )


async def get_key(user_id: str, provider: str) -> str:
    """This agency's key for one provider, or "" if they haven't set one."""
    if not user_id or not provider:
        return ""
    async with get_session() as s:
        val = await s.scalar(
            select(UserLlmKey.api_key).where(
                UserLlmKey.user_id == user_id,
                UserLlmKey.provider == provider,
            )
        )
    return (val or "").strip()


async def set_keys(user_id: str, values: dict[str, str | None]) -> None:
    """Upsert this agency's keys. "" / None clears that provider's row.

    Raises ValueError on an unknown provider or a masked value — both are caller
    mistakes that must not be swallowed into a stored key, because a
    stored-but-wrong key looks IDENTICAL to a correct one until a fan gets no
    reply. Validation runs against the live provider registry, so a renamed
    provider surfaces as a 400 on save instead of silently orphaning a row the
    read path can no longer find.

    Everything is validated before anything is written: a bad second field must
    not leave the first one applied.

    Clearing a key EMPTIES the row, it does not delete it, so a cleared key and
    a never-set one read identically everywhere — both are "no key", both fail
    closed. One representation, one meaning.
    """
    known = sorted(PROVIDERS)
    cleaned: dict[str, str] = {}
    for prov, raw in values.items():
        if prov not in known:
            raise ValueError(f"unknown provider {prov!r}; known: {known}")
        if raw is not None and not isinstance(raw, str):
            # JSON permits numbers, lists and objects here. Reject at the
            # boundary rather than letting .strip() AttributeError into a 500.
            raise ValueError(f"{prov}: expected a string key, got {type(raw).__name__}")
        val = (raw or "").strip()
        if MASK_CHAR in val:
            raise ValueError(
                f"{prov}: that looks like the masked placeholder, not a key — "
                "leave the field blank to keep the stored value"
            )
        cleaned[prov] = val
    if not cleaned:
        return
    now = datetime.utcnow()
    async with get_session() as s:
        for prov, val in cleaned.items():
            stmt = sqlite_insert(UserLlmKey).values(
                user_id=user_id, provider=prov, api_key=val, updated_at=now,
            )
            await s.execute(stmt.on_conflict_do_update(
                index_elements=[UserLlmKey.user_id, UserLlmKey.provider],
                set_={"api_key": val, "updated_at": now},
            ))


async def status(user_id: str) -> dict[str, dict]:
    """UI-facing view: per provider, whether this agency has a key and a masked
    hint. The raw key never leaves this process."""
    async with get_session() as s:
        rows = (await s.execute(
            select(UserLlmKey.provider, UserLlmKey.api_key)
            .where(UserLlmKey.user_id == user_id)
        )).all()
    have = {prov: (val or "").strip() for prov, val in rows}
    return {
        prov: {"set": bool(have.get(prov)), "hint": mask(have.get(prov, ""))}
        for prov in sorted(PROVIDERS)
    }

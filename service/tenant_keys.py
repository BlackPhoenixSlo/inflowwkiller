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
    several (nobody can say whose money this is).

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
    them), or several (we cannot tell whose money this is).

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

    Two or more NON-master owners is the genuine conflict — two agencies both
    claiming one creator — and it is REFUSED, not tie-broken. Neither row order
    nor link age says whose credential should pay, and picking one bills the
    wrong agency SILENTLY, which is the one outcome this module exists to
    prevent. The caller errors, the Setup card names the account and its owners,
    and the operator resolves it by transferring the account to whichever of
    them should keep it.

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


async def shared_accounts(account_ids) -> list[dict]:
    """The accounts in `account_ids` whose LLM calls are blocked by a second
    agency, with who the two are.

    Same rule as `owners_of`, and it has to be: a master's oversight link is not
    a conflict and must not be reported as one, or the card cries wolf on every
    account the founder can see. Only two or more NON-master owners blocks a
    call.

    Both ways it happens are silent (a grant, or a second signed-in user
    completing a session capture), so without this the operator's only signal is
    a log line on a box they are not tailing. Surfaced in Setup → Your AI keys
    next to the keys, because "my models stopped replying" is the symptom that
    sends them there.
    """
    ids = [a for a in (account_ids or []) if a]
    if not ids:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            select(UserAccount.account_id, User.username, User.is_admin,
                   Account.nickname)
            .join(User, User.id == UserAccount.user_id)
            .outerjoin(Account, Account.id == UserAccount.account_id)
            .where(UserAccount.account_id.in_(ids))
        )).all()
    by_account: dict[str, dict] = {}
    for account_id, username, is_admin, nickname in rows:
        entry = by_account.setdefault(
            account_id,
            {"account_id": account_id, "nickname": nickname or account_id,
             "links": []},
        )
        entry["links"].append((username, bool(is_admin)))
    for e in by_account.values():
        e["owners"] = billable_owners(e.pop("links"))
    # More than one billable owner IS the refusal `llm_client` raises — same
    # rule, same source, so this card can never disagree with the money path.
    out = [e for e in by_account.values() if len(e["owners"]) > 1]
    for e in out:
        e["owners"].sort()
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

    Counts are BILLED, not linked, through `billable_owners` — the same rule
    the money path resolves with, because a screen that predicts what the relay
    will do must not re-derive it. That leaves accounts two agencies both claim
    billing nobody, which would vanish silently, so they come back as
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

    for (uid, account_id), is_live in links.items():
        billable = billable_owners(owners_by_account.get(account_id, []))
        if len(billable) > 1:
            # Two agencies claim it, so it bills nobody. Reported, not counted,
            # and only against the owners actually in the dispute.
            if uid in billable and is_live:
                out[uid]["blocked_accounts"] += 1
            continue
        if not billable or billable[0] != uid:
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

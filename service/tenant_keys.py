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
from db.models import Account, AccountHealth, Session, User, UserAccount, UserLlmKey
from llm_providers import PROVIDERS
from secrets_store import MASK_CHAR, mask


# ── Store ─────────────────────────────────────────────────────────────────

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
    non_master = [uid for uid, is_admin in rows if not is_admin]
    return non_master or [uid for uid, _ in rows]


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
             "owners": []},
        )
        if not is_admin:
            entry["owners"].append(username)
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


async def key_overview(registry_ids: set[str] | None = None) -> list[dict]:
    """Every agency, with the two facts that decide whether it needs a key:
    how many of its models can currently TALK, and which providers it has set.

    A dead session is the reason most rows here don't matter. Two thirds of
    this deployment's accounts have one, and an account whose session OF has
    rejected sends nothing at all — so pasting a key under its owner buys
    nothing until the session is re-captured. Sorting the founder's screen by
    "has a live model" is the difference between four rows to fill in and
    twenty to guess at.

    LIVE means: the account is IN THE REGISTRY, a latest captured session
    exists, and `account_health` has not flagged it dead. That flag LAGS — it
    is set by a probe, not by the failure — so a model counted live here can
    still be a few hours behind reality. Good enough for "who is worth a key",
    not a health dashboard.

    `registry_ids` is not optional in spirit. Deleting a model removes it from
    the registry and leaves every DB row behind — the `accounts` row, a
    `sessions` row still flagged `is_latest`, the `user_accounts` link — so a
    DB-only read counts models that were deleted days ago and sends the founder
    to buy a key for one of them. That happened: an account deleted on 08-14
    still read as live on 08-20 and got its owner keyed for nothing. The
    registry is what the operator sees and what they can act on, so it is what
    this counts. Passing None keeps every account, which is only right when the
    caller has already scoped them.

    Counts are BILLED, not linked — the same rule `owners_of` resolves with, and
    it has to be the same or the screen lies. A master holding oversight links
    on three agencies' models would otherwise read as "3 live" under its own
    row and be told it needs a key, when those calls bill the agencies and
    nothing would ever spend the master's credential. So: an account counts for
    its non-master owner when it has one, for the master when the master is the
    only link, and for NEITHER when two agencies claim it.

    That last case would vanish silently, so it comes back as `blocked_accounts`.
    Those models are refused outright and no key fixes them — the fix is a
    transfer or a revoke — and a screen about keys is exactly where someone will
    otherwise sit and wonder why a pasted key changed nothing.

    Providers are NAMES only, never values or hints — this feeds a list of many
    agencies at once, and the per-agency card is where a masked hint belongs.
    """
    async with get_session() as s:
        rows = (await s.execute(
            select(
                User.id, User.username, User.is_admin,
                UserAccount.account_id,
                Session.account_id.label("has_session"),
                AccountHealth.session_dead_at,
            )
            .join(UserAccount, UserAccount.user_id == User.id, isouter=True)
            .join(
                Session,
                (Session.account_id == UserAccount.account_id)
                & (Session.is_latest.is_(True)),
                isouter=True,
            )
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
    # One row per (user, account) before anything is counted: the Session join
    # can fan out if more than one row is flagged latest for an account, and
    # that is a bug to not double-count our way around.
    links: dict[tuple[str, str], bool] = {}
    owners_by_account: dict[str, list[tuple[str, bool]]] = {}
    for uid, username, is_admin, account_id, has_session, dead_at in rows:
        out.setdefault(uid, {
            "user_id": uid, "username": username, "is_admin": bool(is_admin),
            "accounts": 0, "live_accounts": 0, "blocked_accounts": 0,
            "providers_set": sorted(providers.get(uid, [])),
        })
        if not account_id:
            continue
        in_registry = registry_ids is None or account_id in registry_ids
        links[(uid, account_id)] = (
            in_registry and bool(has_session) and dead_at is None
        )
        owners = owners_by_account.setdefault(account_id, [])
        if (uid, bool(is_admin)) not in owners:
            owners.append((uid, bool(is_admin)))

    for (uid, account_id), is_live in links.items():
        owners = owners_by_account.get(account_id, [])
        agencies = [o for o, adm in owners if not adm]
        if len(agencies) > 1:
            # Two agencies claim it — refused by `owners_of`, so it bills
            # nobody. Reported, not counted, and only against the agencies
            # actually in the dispute.
            if uid in agencies and is_live:
                out[uid]["blocked_accounts"] += 1
            continue
        billed = agencies[0] if agencies else (owners[0][0] if owners else None)
        if billed != uid:
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

"""service/tip_ledger.py — the one spelling of the ledger tip row's body prefix.

A three-line leaf, on purpose. `transaction_ingest` WRITES this string (see
`tip_ledger_body` there) and three readers RECOGNISE it — `send_welcome`'s
`stop_on_reply` predicate, `_common.inbound_is_words`, and the SQL both of those
compile. Every one of them must match the writer exactly, so there can only be
one definition; a second hand-copied literal is how a cosmetic edit to the emoji
turns every bare tip into "he used words" and aborts every welcome burst, with
the suite green.

WHY IT IS NOT SIMPLY DEFINED IN `transaction_ingest`, which is where it was:
`automations/_common.py` is the base module every automation imports, and
`transaction_ingest` is an ingest ORCHESTRATOR — importing it there dragged 250+
modules (`of_client`, `fansly_client`, `curl_cffi`, `requests`, `urllib3`,
`client_pool`, `secrets_store` and its import-time filesystem probe) under the
base of the automation tree. No cycle today, but it puts a directed edge from
the bottom layer into an upper one, so the day `ownership` or `client_pool`
acquires a single top-level automation import, that edge closes a cycle
underneath eighty modules. A leaf with no imports of its own buys the same "one
definition" guarantee and cannot participate in a cycle at all.

Nothing may be added here that needs an import.
"""
from __future__ import annotations

# The body `transaction_ingest` writes for a chat tip that exists only in the
# ledger: "💸 Sent a $5.00 tip". Nobody TYPED it — it is our own bookkeeping
# wearing an inbound row, which is why the readers exclude it when they ask
# "did the fan say something".
TIP_LEDGER_PREFIX = "💸 Sent a $"

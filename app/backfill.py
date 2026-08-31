"""Load a signing-in user's own subscriptions into the warehouse.

The warehouse is populated by a background job that enumerates subscriptions with the *app's*
managed identity. Entitlement, though, is worked out per person from their own role assignments.
Those two sets are not the same, and where they differ the person loses: they sign in entitled
to a subscription the ingest never visited, the dashboard scopes correctly to a subscription
with no rows, and they are shown an empty estate with no indication that anything is missing.
Empty and not-yet-loaded look identical, and the second one is not a fact about their spend.

So on sign-in, compare what they are entitled to against what the warehouse actually holds and
backfill the difference in the background.

Two things this deliberately does not do:

  * **Pretend it can always succeed.** Cost is read with the app's identity. A subscription the
    *user* can see but the *app* cannot will fail with 403, and no amount of retrying changes
    that -- it needs a role assignment on the app's managed identity, or delegated ARM so the
    call carries the user's own token. The failure is recorded against the subscription with
    its reason, so the UI can say which subscription is missing and why, rather than showing a
    smaller number than the truth and looking confident about it.

  * **Block the sign-in.** A cost report takes minutes. Everything here runs detached, and the
    page renders immediately with whatever the warehouse already had.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

log = logging.getLogger("cloudlens.backfill")

# How long a subscription's outcome is remembered. Long enough that reloading the page does not
# re-run a report that just failed; short enough that a role assignment granted this morning is
# picked up without a restart.
REMEMBER_OK = 12 * 3600
REMEMBER_FAILED = 30 * 60

# One at a time. Each job is several minutes of Cost Management report generation, and a burst
# of sign-ins should not turn into a burst of concurrent report requests against the same
# billing account.
_gate = asyncio.Lock()

# subscription id -> {"status", "name", "detail", "at"}
_seen: dict[str, dict[str, Any]] = {}
_running: set[str] = set()
_tasks: set[asyncio.Task] = set()


def _fresh(entry: dict[str, Any]) -> bool:
    ttl = REMEMBER_OK if entry.get("status") == "loaded" else REMEMBER_FAILED
    return (time.time() - entry.get("at", 0)) < ttl


def present() -> set[str]:
    """Subscription ids the warehouse already holds rows for."""
    from .warehouse import warehouse

    try:
        with warehouse.reader() as r:
            rows = r.rows('SELECT DISTINCT "SubAccountId" AS id FROM costs '
                          'WHERE "SubAccountId" IS NOT NULL')
        return {str(x["id"]).lower() for x in rows if x.get("id")}
    except Exception as exc:  # noqa: BLE001 - a warehouse hiccup must not break sign-in
        log.warning("could not read loaded subscriptions: %s", str(exc)[:200])
        return set()


def status_for(entitled: list[dict] | None) -> dict[str, Any]:
    """What to tell the browser about the subscriptions this person is entitled to.

    `entitled` is the caller's own subscription list. Anything in it that the warehouse has no
    rows for is either loading, or has failed and should say why.
    """
    if not entitled:
        return {"pending": [], "failed": [], "loading": False}

    have = present()
    pending, failed = [], []
    for sub in entitled:
        sid = str(sub.get("id") or "")
        if not sid or sid.lower() in have:
            continue
        entry = _seen.get(sid.lower())
        name = sub.get("name") or sid
        if entry and entry.get("status") == "failed":
            failed.append({"id": sid, "name": name, "detail": entry.get("detail") or ""})
        else:
            pending.append({"id": sid, "name": name})

    return {"pending": pending, "failed": failed, "loading": bool(pending)}


def schedule(entitled: list[dict] | None, months: int = 3) -> None:
    """Queue a backfill for any entitled subscription the warehouse does not have.

    Fire-and-forget by design: called from a request handler that must return immediately.
    """
    if not entitled:
        return

    have = present()
    todo = []
    for sub in entitled:
        sid = str(sub.get("id") or "")
        key = sid.lower()
        if not sid or key in have or key in _running:
            continue
        entry = _seen.get(key)
        if entry and _fresh(entry):
            continue
        todo.append({"id": sid, "name": sub.get("name") or sid})

    if not todo:
        return

    for sub in todo:
        _running.add(sub["id"].lower())
    task = asyncio.create_task(_run(todo, months))
    # Without a strong reference the loop is free to garbage-collect a running task mid-flight,
    # and the backfill simply stops with nothing logged.
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _run(subs: list[dict], months: int) -> None:
    from .warehouse import warehouse

    async with _gate:
        for sub in subs:
            key = sub["id"].lower()
            try:
                log.info("backfilling %s (%s)", sub["name"], sub["id"])
                # One subscription per call. ingest() deletes and reloads only the periods it is
                # given for the subscription it is given, so this cannot disturb data already
                # loaded for anyone else.
                state = await _ingest_one(warehouse, sub, months)
                rows = int(state.get("rows") or 0)
                failed = int(state.get("failed") or 0)
                if rows:
                    _seen[key] = {"status": "loaded", "name": sub["name"],
                                  "detail": "", "at": time.time()}
                    log.info("backfilled %s: %d rows", sub["name"], rows)
                else:
                    _seen[key] = {
                        "status": "failed", "name": sub["name"], "at": time.time(),
                        "detail": (state.get("detail")
                                   or ("no cost rows returned"
                                       if not failed else "every period failed")),
                    }
                    log.warning("backfill for %s produced no rows: %s",
                                sub["name"], _seen[key]["detail"][:200])
            except Exception as exc:  # noqa: BLE001 - one subscription must not stop the rest
                log.warning("backfill failed for %s: %s", sub["name"], str(exc)[:200])
                _seen[key] = {"status": "failed", "name": sub["name"],
                              "detail": str(exc)[:200], "at": time.time()}
            finally:
                _running.discard(key)


async def _ingest_one(warehouse: Any, sub: dict, months: int) -> dict[str, Any]:
    """Ingest one subscription without disturbing the shared progress state.

    `warehouse.state` drives the Refresh button's progress readout. A backfill triggered by
    somebody signing in is not that job, and letting it overwrite the field would make a manual
    refresh appear to restart, or to finish early, for reasons the person watching cannot see.
    """
    before = dict(warehouse.state)
    try:
        return await warehouse.ingest([sub], months=months)
    finally:
        warehouse.state = before

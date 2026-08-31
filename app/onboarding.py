"""First-run setup: turn an empty deployment into one with data in it.

An empty warehouse and an estate that genuinely spent nothing render identically — a
dashboard of zeroes — and only one of those is a fact about someone's spend. The difference
matters most at the exact moment nobody has any context to tell them apart: the first time
they sign in.

So when there are no rows at all, offer to go and find some. The scan is the same work an
administrator would otherwise do by hand across two tabs and a form, in the order that gets
to data soonest:

  1. **Exports Azure is already writing.** Discovery ranks FOCUS ahead of amortized ahead of
     actual, and reading a blob someone else's schedule produced is seconds of work that also
     carries tags — which the Query API cannot return at all. If any are reachable this is
     both the fastest path and the most complete one.

  2. **Exports this app creates.** Where none exist, create one per subscription against the
     deployment's own archive container. This is the part a person cannot easily discover:
     it needs a destination, a managed identity, and a role assignment on the container, and
     `schedules.create` already does all three.

     Deliberately *not* waited for. Azure accepts the request in seconds and writes the first
     blob minutes later, so blocking on it would mean a progress bar that sits still for the
     entire time with nothing to show. It is set up for tomorrow and the scan moves on.

  3. **The Cost Details API.** Always available to anyone who can read cost, needs no storage
     and no rights to create anything. Slower per subscription, and it is what actually puts
     numbers on the screen today rather than after the first export lands.

Only step 3 is guaranteed, which is why it is last and why it always runs when the earlier
steps produced nothing. A scan that set up a beautiful export pipeline and left the dashboard
empty for a day would have answered a question nobody asked.

**Why the "only once" is mostly not a flag.** The offer appears when the warehouse is empty
and the question has not been settled. Once a scan lands rows the first condition is false
forever on its own, so the data is its own record and cannot disagree with a flag about it.
The stored fact only covers what the data cannot say: an estate that really is empty, and a
person who said no. A failed scan settles nothing — hiding the only affordance someone has
after a transient Azure error would be the opposite of helpful.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

log = logging.getLogger("cloudlens.onboarding")

# The stored answer to "has first-run setup been dealt with". Written once, read on every
# sign-in. Values: "done" (a scan landed rows) or "dismissed" (they said no thanks).
SETTLED_KEY = "onboarding_settled"

# How much history to pull. Three months is enough for the period comparisons the dashboard
# makes without turning a first run into an afternoon.
DEFAULT_MONTHS = 3

# One scan per deployment. It writes to the shared warehouse and creates Azure resources, so
# two at once would duplicate exports and race on the same rows.
_lock = asyncio.Lock()
_task: asyncio.Task | None = None

# What the scan is doing, for the card to poll. Reset at the start of each run.
_progress: dict[str, Any] = {"running": False, "steps": [], "started": 0.0, "finished": 0.0}


def _step(name: str, status: str, detail: str = "") -> None:
    """Record a step so the UI can show what is happening rather than just spinning."""
    for existing in _progress["steps"]:
        if existing["name"] == name:
            existing.update(status=status, detail=detail)
            break
    else:
        _progress["steps"].append({"name": name, "status": status, "detail": detail})
    log.info("onboarding: %s -- %s%s", name, status, f" ({detail})" if detail else "")


def settled() -> str | None:
    """Whether first-run setup has been answered, and how."""
    from .warehouse import warehouse

    return warehouse.get_meta(SETTLED_KEY)


def settle(how: str) -> None:
    """Record that the question has been answered, so it is never asked again."""
    from .warehouse import warehouse

    warehouse.set_meta(SETTLED_KEY, how)


def progress() -> dict[str, Any]:
    """A copy, so a caller iterating it cannot be tripped by the scan mutating it."""
    return {
        "running": bool(_progress["running"]),
        "steps": [dict(s) for s in _progress["steps"]],
        "started": _progress["started"],
        "finished": _progress["finished"],
    }


def state(rows: int, is_admin: bool, subscriptions: int,
          ingesting: bool = False) -> dict[str, Any]:
    """Whether to offer first-run setup, and if not, why not.

    Takes the row count rather than reading it, because every caller has already asked the
    warehouse something and this should not cost a second connection.

    `ingesting` matters more than it looks. A fresh deployment starts its own ingest on boot
    (AUTO_INGEST), which takes minutes and holds zero rows for most of them — so testing rows
    alone would put "there is no cost data, would you like to set it up?" in front of someone
    whose data is already on its way, and then take it away again when it lands. That is the
    same "empty and loading look identical" confusion this card exists to fix, inverted.
    """
    mark = settled()
    running = bool(_progress["running"])
    # Rows are the honest test. A deployment with data has been onboarded whether or not
    # anyone ever pressed the button — by an admin's manual refresh, by the background
    # backfill, or by a colleague who got there first.
    first_run = rows == 0 and mark is None

    out: dict[str, Any] = {
        "first_run": first_run,
        "running": running,
        # Somebody else's ingest, not this card's scan. Reported separately because the two
        # want different words: one is "we are setting this up for you", the other is "this is
        # already happening, sit tight".
        "ingesting": bool(ingesting) and not running,
        "settled": mark,
        "rows": rows,
        "subscriptions": subscriptions,
        "can_scan": first_run and is_admin and subscriptions > 0 and not ingesting,
        "progress": progress() if (running or _progress["steps"]) else None,
    }
    if first_run and ingesting and not running:
        out["blocked"] = ("Cost data is already being loaded for this deployment. It takes a "
                          "few minutes — this page will fill in as soon as it lands.")
    elif first_run and not is_admin:
        # A scan creates Azure resources, so it is admin-gated like every other write. Saying
        # so is better than showing a button that returns 403, and better than showing nothing
        # at all to someone looking at an empty dashboard wondering if it is broken.
        out["blocked"] = ("Setting up cost data creates Cost Management exports, which only "
                          "an administrator of this deployment can do. Ask one of them to "
                          "sign in and run the first-time setup.")
    elif first_run and subscriptions == 0:
        out["blocked"] = ("Your account cannot see any Azure subscriptions, so there is "
                          "nothing to load. Check your Azure access, then reload.")
    return out


def start(subs: list[dict[str, Any]], months: int = DEFAULT_MONTHS) -> bool:
    """Kick off the scan. Returns False if one is already in flight."""
    global _task

    if _progress["running"] or (_task and not _task.done()):
        return False
    _progress.update(running=True, steps=[], started=time.time(), finished=0.0)
    _task = asyncio.create_task(_run(subs, months))
    return True


async def _run(subs: list[dict[str, Any]], months: int) -> None:
    """The escalation ladder. Each rung is tried only because the one above found nothing."""
    from .warehouse import warehouse

    async with _lock:
        try:
            loaded = 0
            _step("Looking for cost exports Azure already writes", "running")
            existing = await _use_existing_exports()
            if existing["rows"]:
                loaded = existing["rows"]
                _step("Looking for cost exports Azure already writes", "done",
                      f"loaded {loaded:,} rows from {existing['source']}")
            else:
                _step("Looking for cost exports Azure already writes", "skipped",
                      existing["detail"])

                _step("Setting up a daily export for the future", "running")
                made = await _create_exports(subs)
                _step("Setting up a daily export for the future",
                      "done" if made["created"] else "skipped",
                      made["detail"])

            if not loaded:
                # The only rung that is always available, and the one that puts numbers on
                # screen today. A new export takes hours to produce its first blob.
                _step("Loading cost history", "running")
                loaded = await _load_from_api(subs, months)
                _step("Loading cost history",
                      "done" if loaded else "failed",
                      f"loaded {loaded:,} rows" if loaded
                      else "Azure returned no cost data for these subscriptions")

            if loaded:
                # Settled by the data, not by the attempt. A scan that failed leaves the offer
                # in place so it can be tried again once whatever blocked it is fixed.
                settle("done")
                await warehouse.publish_async()
        except Exception as exc:  # noqa: BLE001 - the card must report, not vanish
            log.warning("onboarding scan failed: %s", str(exc)[:300])
            _step("Setting up", "failed", str(exc)[:200])
        finally:
            _progress["running"] = False
            _progress["finished"] = time.time()


async def _use_existing_exports() -> dict[str, Any]:
    """Ingest whatever export Azure is already writing, if any is readable."""
    from .exports import discover_exports, reachable
    from .warehouse import warehouse

    try:
        found = await discover_exports()
    except Exception as exc:  # noqa: BLE001 - no exports is a normal answer, not a failure
        return {"rows": 0, "detail": f"could not list exports: {str(exc)[:160]}", "source": ""}

    # An export definition outlives the storage account it writes to, so one without a
    # destination can never be read however well it ranks.
    candidates = [e for e in (found.get("exports") or []) if e.get("container")]
    if not candidates:
        return {"rows": 0, "detail": "no existing exports found", "source": ""}

    # An export can exist and its storage still be unreadable — firewalled, or the app has no
    # data-plane role. Probing first turns a slow failure into a fast skip, which matters here
    # because there is another rung below to get on with.
    ok, _bad = await reachable(candidates)
    if not ok:
        return {"rows": 0, "source": "",
                "detail": f"found {len(candidates)} export(s), but none were readable"}

    before = warehouse.row_count()
    # The same loader the Refresh button uses, rather than a second implementation of it. It
    # loads a current-period export *and* a closed-months one, which single-pick logic gets
    # wrong: a FOCUS export covering June and July leaves August — the month anyone is
    # actually looking at — untouched.
    from .main import _load_export_pair

    result = await _load_export_pair(ok)
    if not result["loaded"]:
        return {"rows": 0, "source": "",
                "detail": f"none of the {len(ok)} readable export(s) could be loaded"}
    return {"rows": max(0, warehouse.row_count() - before),
            "detail": "", "source": ", ".join(result["loaded"])}


async def _create_exports(subs: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a daily FOCUS export per subscription, for tomorrow onwards.

    Best-effort by design. This needs archive storage, Cost Management Contributor, and the
    right to assign a role on the destination — a deployment can be perfectly usable without
    any of them, and the API path below does not care.
    """
    from . import archive, schedules

    account_id = os.getenv("ARCHIVE_ACCOUNT_ID", "").strip()
    if not account_id or not archive.enabled():
        return {"created": 0,
                "detail": "no archive storage is configured, so there is nowhere to write one"}

    created, failed = 0, []
    for sub in subs:
        try:
            await schedules.create(
                sub["id"], "FocusCost",
                account_id=account_id,
                container=archive.CONTAINER,
                recurrence="Daily",
                # The first run is triggered, but nothing waits for it: Azure writes the blob
                # minutes later, and the API path below is what fills the screen meanwhile.
                run_now=True,
            )
            created += 1
        except Exception as exc:  # noqa: BLE001 - one refusal must not stop the rest
            failed.append(f"{sub.get('name') or sub['id']}: {str(exc)[:120]}")

    if created and not failed:
        return {"created": created,
                "detail": f"created a daily export for {created} subscription(s)"}
    if created:
        return {"created": created,
                "detail": f"created {created}, could not create {len(failed)} "
                          f"({'; '.join(failed[:2])})"}
    return {"created": 0, "detail": "; ".join(failed[:2]) or "no exports could be created"}


async def _load_from_api(subs: list[dict[str, Any]], months: int) -> int:
    """Pull cost through the Cost Details API — the path that always works."""
    from .warehouse import warehouse

    try:
        result = await warehouse.ingest(subs, months=months)
        return int(result.get("rows") or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("onboarding API load failed: %s", str(exc)[:300])
        _step("Loading cost history", "failed", str(exc)[:200])
        return 0

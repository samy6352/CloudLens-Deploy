"""Scheduled Cost Management exports, created and owned by CloudLens.

Why this exists: refreshing through the Cost Details API means asking Azure to *generate* a
report and then waiting for it. One report is about forty seconds; a three-month refresh across
three subscriptions is nine of them, and the per-subscription submission limit forces them into
a queue. That is minutes of waiting for data Azure could just as easily have written to a blob
overnight.

A scheduled export inverts it. Azure writes the data on its own timetable, and a refresh becomes
a blob read -- seconds, not minutes, and no rate limit to queue behind.

The thing that makes this possible here is **managed identity**. A Cost Management export
normally authenticates to its destination with a storage account key, and this tenant forces
`allowSharedKeyAccess=false` on every account, so the obvious approach fails with "Key-based
authentication is currently disabled". Creating the export with `identity: SystemAssigned` and
granting *that* identity Storage Blob Data Contributor sidesteps keys entirely. Verified working
on 2026-08-28.

One Azure limit shapes what this module offers:

  * **Each export covers one subscription.** Scheduling across an estate is one export per
    subscription, which is why `ensure` takes a list and reports per-subscription results.

It used to record a second: that FocusCost was unavailable at subscription scope. That was
never true — it was the pinned API version refusing it. See EXPORT_API below.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("cloudlens.schedules")

# The version that accepts `identity` on an export. Older versions silently ignore it and fall
# back to key auth, which fails on a locked-down account -- so this is pinned deliberately.
#
# 2025-03-01 rather than 2023-08-01 for a second reason: FocusCost. The older version refuses
# it outright -- `Invalid definition type 'FocusCost'; valid values: 'ActualCost,
# 'AmortizedCost'` -- and because that 400 arrives on a subscription-scoped request, it read as
# "Azure does not allow FOCUS at subscription scope" and was written down here as such. It is
# not a scope limit, it is a version limit: the same body against 2025-03-01 creates the export
# and mints its identity. This version also *lists* FOCUS exports that 2023-08-01 silently
# omits, so exports created in the portal were invisible to the app.
EXPORT_API = "2025-03-01"
STORAGE_API = "2023-05-01"
ROLE_API = "2022-04-01"

# Storage Blob Data Contributor. The export writes and overwrites, so Reader is not enough.
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"

# What a schedule may produce. FOCUS first: it is the one schema that carries actual and
# amortized together, so it answers both questions from one file and is what the refresh
# prefers when it finds one.
METRICS = ("FocusCost", "AmortizedCost", "ActualCost")
RECURRENCE = ("Daily", "Weekly", "Monthly")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


class ScheduleError(RuntimeError):
    """Something the caller can act on, phrased for them."""


def _arm() -> str:
    from . import cost

    return cost.ARM


async def _request(method: str, url: str, *, body: dict | None = None,
                   params: dict | None = None) -> dict:
    from . import cost

    return await cost.azure.request(method, url, json_body=body, params=params, cache=False)


def export_name(metric: str, prefix: str = "cloudlens") -> str:
    """A stable name, so re-running setup updates the same export rather than making a second."""
    return f"{prefix}-{metric.replace('Cost', '').lower()}-daily"


def _destination(account_id: str, container: str, folder: str) -> dict[str, Any]:
    return {"destination": {"resourceId": account_id, "container": container,
                            "rootFolderPath": folder}}


def _body(metric: str, account_id: str, container: str, folder: str,
          recurrence: str = "Daily", years: int = 3) -> dict[str, Any]:
    """The export as Azure wants it.

    `OverwritePreviousReport` matters: without it every run writes a new folder and the
    container grows without bound, which is the same mistake the daily archive avoids by naming
    blobs after the date.
    """
    now = datetime.now(timezone.utc)
    # Start tomorrow. An export whose window opens in the past is rejected, and one starting in
    # the next few minutes races the caller's own clock skew.
    start = (now + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    return {
        "identity": {"type": "SystemAssigned"},
        # Required alongside identity, and must be a real region.
        "location": "centralindia",
        "properties": {
            "schedule": {
                "status": "Active",
                "recurrence": recurrence,
                "recurrencePeriod": {
                    "from": start.isoformat().replace("+00:00", "Z"),
                    "to": start.replace(year=start.year + years).isoformat().replace("+00:00", "Z"),
                },
            },
            "format": "Csv",
            "partitionData": True,
            "dataOverwriteBehavior": "OverwritePreviousReport",
            "deliveryInfo": _destination(account_id, container, folder),
            "definition": {
                "type": metric,
                "timeframe": "MonthToDate",
                "dataSet": {"granularity": "Daily"},
            },
        },
    }


async def _grant_storage(principal_id: str, account_id: str) -> str:
    """Give the export's own identity permission to write to the destination.

    The identity is created by Azure when the export is saved, so this cannot be done in
    advance -- and without it every run fails with a 403 that names nothing the operator has
    heard of. An existing assignment comes back 409, which is success.
    """
    import uuid

    url = f"{_arm()}{account_id}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}"
    body = {"properties": {
        "roleDefinitionId": ("/providers/Microsoft.Authorization/roleDefinitions/"
                             + BLOB_CONTRIBUTOR),
        "principalId": principal_id,
        "principalType": "ServicePrincipal",
    }}
    try:
        await _request("PUT", url, body=body, params={"api-version": ROLE_API})
        return "granted"
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if "RoleAssignmentExists" in text or "409" in text:
            return "already granted"
        if "AuthorizationFailed" in text or "403" in text:
            raise ScheduleError(
                "The export was created, but this account cannot grant it access to the storage "
                "container. Granting a role needs Owner or User Access Administrator on the "
                "storage account; without it the export will run and fail to write."
            ) from exc
        raise ScheduleError(f"Could not grant the export access to storage: {text[:200]}") from exc


async def create(subscription_id: str, metric: str, *, account_id: str, container: str,
                 folder: str = "exports", recurrence: str = "Daily",
                 run_now: bool = True) -> dict[str, Any]:
    """Create or update one scheduled export, and make it able to write.

    A PUT, so re-running is an update rather than a duplicate. The role grant and the first run
    follow, because an export that exists but cannot write, or that will not produce anything
    until 2am tomorrow, is not what someone asking for this actually wants.
    """
    if metric not in METRICS:
        raise ScheduleError(
            f"Scheduled exports support {', '.join(METRICS)}."
        )
    if recurrence not in RECURRENCE:
        raise ScheduleError(f"Recurrence must be one of {', '.join(RECURRENCE)}.")

    name = export_name(metric)
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}")
    body = _body(metric, account_id, container, folder, recurrence)

    try:
        made = await _request("PUT", url, body=body, params={"api-version": EXPORT_API})
    except Exception as exc:  # noqa: BLE001
        # Logged in full before it is turned into a sentence. The user-facing message has to be
        # short, and every time this failed the one thing nobody could get at was what Azure
        # actually said — which is where the answer was.
        log.warning("export PUT %s failed: %s", name, str(exc)[:800])
        raise ScheduleError(_explain_create(str(exc), subscription_id)) from exc

    principal = ((made.get("identity") or {}).get("principalId")) if isinstance(made, dict) else None
    grant = "no identity returned"
    if principal:
        grant = await _grant_storage(principal, account_id)

    started = False
    if run_now and principal:
        # Role assignments take a few seconds to reach the data plane. Running immediately
        # produces a 403 and a failed run in the history, which reads as a broken schedule.
        await asyncio.sleep(20)
        try:
            await _request("POST", f"{url}/run", params={"api-version": EXPORT_API})
            started = True
        except Exception as exc:  # noqa: BLE001 - the schedule is still valid either way
            log.warning("first run of %s failed: %s", name, str(exc)[:200])

    return {
        "name": name,
        "subscription": subscription_id,
        "metric": metric,
        "recurrence": recurrence,
        "destination": f"{account_id.rsplit('/', 1)[-1]}/{container}/{folder}",
        "identity": principal,
        "storage_access": grant,
        "first_run_started": started,
    }


def _explain_create(detail: str, subscription_id: str) -> str:
    """The refusals that actually happen, in words rather than as an ARM code."""
    low = detail.lower()
    if "key-based authentication" in low or "allow storage account key access" in low:
        return (
            "Azure tried to authenticate to storage with an account key, which this tenant "
            "disables. That happens when the export is created without a managed identity — "
            "this app always sets one, so if you are seeing this, the API version in use does "
            "not support it."
        )
    # The one that cost a long afternoon. Creating an export with `identity: SystemAssigned`
    # makes Azure mint an identity for the export and then grant it access to the destination
    # container *on the caller's behalf* — so the caller needs rights to hand out roles there,
    # not merely to write there. The refusal arrives as HTTP 401 with code RBACAccessDenied and
    # the message "The client does not have authorization to perform action", which names
    # neither the action nor the resource, and reads exactly like an expired token.
    if "roleassignments/write" in low.replace(" ", "") or "supplying identity in payload" in low:
        return (
            "The export could not be created because this app's identity cannot grant access "
            "to the archive storage account. Creating an export with a managed identity makes "
            "Azure assign that identity a role on the destination container on your behalf, so "
            "the caller needs Microsoft.Authorization/roleAssignments/write there — User Access "
            "Administrator scoped to the storage account is enough, and is narrower than it "
            "sounds because it applies to that one account. Writing to the container is not "
            "sufficient on its own. Note that a new role assignment can take a few minutes to "
            "take effect."
        )
    # Checked before the generic 403 branch: an RBAC-shaped word appears in both, and the
    # scope-lock refusal is the one that cannot be fixed by granting a role.
    if "rbacdisallowedoperation" in low or "disallowed by policy" in low:
        return (
            "Azure refused the write itself rather than the identity: this subscription's "
            "cost data is managed at a scope above it — a management group or billing account "
            "— and subscription-level exports are blocked there. Roles will not change this; "
            f"the export has to be created at the owning scope. Azure said: {detail[:400]}"
        )
    if "authorizationfailed" in low or "403" in low:
        return (f"Not allowed to create an export on {subscription_id}. This needs Cost "
                "Management Contributor or Contributor on the subscription.")
    if "invalid definition type" in low:
        # The message that caused the confusion this module used to encode: it names the type,
        # not the API version, so it reads as a scope restriction. It is neither — it means the
        # request went out on a version too old to know the type.
        return ("Azure does not recognise that report type on the API version this request "
                f"used. CloudLens asks for {EXPORT_API}, which supports FOCUS, Amortized and "
                f"Actual. Azure said: {detail[:300]}")
    return f"Azure refused the export: {detail[:400]}"


async def listing(subscription_ids: list[str] | None = None) -> dict[str, Any]:
    """Every export CloudLens can see, with its last run — the Cost exports tab's whole content."""
    from . import cost

    subs = await cost._resolve(subscription_ids)

    async def one(sub: dict) -> list[dict]:
        base = (f"{_arm()}/subscriptions/{sub['id']}"
                f"/providers/Microsoft.CostManagement/exports")
        try:
            payload = await _request("GET", base, params={"api-version": EXPORT_API})
        except Exception as exc:  # noqa: BLE001 - one unreadable subscription is not an outage
            log.info("could not list exports on %s: %s", sub["id"][:8], str(exc)[:120])
            return []

        out = []
        for e in payload.get("value", []):
            p = e.get("properties") or {}
            dest = (p.get("deliveryInfo") or {}).get("destination") or {}
            sched = p.get("schedule") or {}
            ours = (e.get("identity") or {}).get("type") == "SystemAssigned"
            out.append({
                "name": e.get("name"),
                "subscription": sub["name"],
                "subscription_id": sub["id"],
                "metric": (p.get("definition") or {}).get("type"),
                "recurrence": sched.get("recurrence"),
                "status": sched.get("status"),
                "account": (dest.get("resourceId") or "").rsplit("/", 1)[-1],
                "container": dest.get("container"),
                "folder": dest.get("rootFolderPath"),
                "identity": ours,
                # Two different times, and they were previously both wrong. `recurrencePeriod.
                # from` is when the schedule *starts* -- which for anything created here is
                # roughly when it was created, since `_body` sets it to tomorrow. It was called
                # `next_run`. `nextRunTimeEstimate` is the genuine next run, and it was called
                # `last_run` and then never used, so the one useful figure Azure returns about
                # a schedule's future was fetched and discarded.
                "starts": (sched.get("recurrencePeriod") or {}).get("from"),
                "next_run": p.get("nextRunTimeEstimate"),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
    found = [x for r in results if not isinstance(r, Exception) for x in r]
    # Newest first. Alphabetical order buried a schedule someone had just created among a dozen
    # others -- on this tenant the list is fourteen long, and "did that work?" is the question
    # being asked the moment the tab redraws. `starts` is the closest thing Azure gives to a
    # creation time; the name breaks ties so the order is stable between renders.
    found.sort(key=lambda e: (e["starts"] or "", e["name"] or ""), reverse=True)
    return {
        "schedules": found,
        "count": len(found),
        "managed": len([e for e in found if e["identity"]]),
        "metrics": list(METRICS),
        "subscriptions": [{"id": s["id"], "name": s["name"]} for s in subs],
    }


async def run_now(subscription_id: str, name: str) -> dict[str, Any]:
    """Ask Azure to produce this export immediately rather than waiting for its schedule."""
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}/run")
    try:
        await _request("POST", url, params={"api-version": EXPORT_API})
    except Exception as exc:  # noqa: BLE001
        raise ScheduleError(f"Could not start {name}: {str(exc)[:200]}") from exc
    return {"name": name, "started": True}


async def remove(subscription_id: str, name: str) -> dict[str, Any]:
    """Delete a schedule. The data it already wrote is left alone."""
    url = (f"{_arm()}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.CostManagement/exports/{name}")
    try:
        await _request("DELETE", url, params={"api-version": EXPORT_API})
    except Exception as exc:  # noqa: BLE001
        raise ScheduleError(f"Could not delete {name}: {str(exc)[:200]}") from exc
    return {"name": name, "deleted": True}

"""Service health advisories: what Azure is changing that affects your resources.

Retirements, deprecations, forced migrations and billing changes all arrive through the same
channel — Service Health events with `EventType == 'HealthAdvisory'`. Azure raises these against
the subscriptions actually affected, which makes it a better source than any curated list: there
is no ruleset here to maintain and no risk of the table going stale while a retirement passes.

The part that makes it actionable is the second table. `microsoft.resourcehealth/events` says
what is changing; `.../events/impactedresources` names *which of your resources* it hits. An
advisory saying "general-purpose v1 accounts are retiring" is a newsletter. The same advisory
naming three storage accounts of yours, with a date, is a task.

Everything here is read-only.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .cost import list_subscriptions
from .waste import _arg

log = logging.getLogger("cloudlens.health")

# Unlike every other Resource Graph table, servicehealthresources exposes its properties in
# PascalCase. Reading `properties.title` here returns null rather than erroring, which looks
# exactly like "no advisories" — worth stating plainly so nobody rediscovers it the hard way.
EVENTS_QUERY = """
    servicehealthresources
    | where type =~ 'microsoft.resourcehealth/events'
    | extend p = properties
    | where tostring(p.EventType) in ('HealthAdvisory', 'SecurityAdvisory')
    | project
        eventId     = name,
        subscriptionId,
        tracking    = tostring(p.TrackingId),
        headline    = tostring(p.Title),
        summary     = tostring(p.Summary),
        eventType   = tostring(p.EventType),
        level       = tostring(p.Level),
        status      = tostring(p.Status),
        startTicks  = tostring(p.ImpactStartTime),
        dueTicks    = tostring(p.ImpactMitigationTime),
        updatedTicks= tostring(p.LastUpdateTime),
        actions     = tostring(p.RecommendedActions)
"""

IMPACTED_QUERY = """
    servicehealthresources
    | where type =~ 'microsoft.resourcehealth/events/impactedresources'
    | extend p = properties
    | extend parts = split(id, '/impactedResources/')
    | project
        tracking     = tostring(split(tostring(parts[0]), '/events/')[1]),
        subscriptionId,
        resourceId   = tostring(p.targetResourceId),
        resourceType = tostring(p.targetResourceType),
        resourceName = tostring(p.targetResourceName)
"""

# Words that mark an advisory as "this is going away" rather than "this is changing". Used only
# to offer a filter — nothing is hidden on the strength of a keyword match.
RETIREMENT_WORDS = re.compile(
    r"\b(retir\w*|deprecat\w*|end[- ]of[- ](life|support)|sunset\w*|"
    r"discontinu\w*|migrate|migration|upgrade|no longer)\b",
    re.IGNORECASE,
)

_EPOCH = datetime(1, 1, 1, tzinfo=timezone.utc)


def _from_ticks(value: Any) -> str | None:
    """.NET ticks to an ISO date.

    Service Health returns times as 100-nanosecond intervals since 0001-01-01, not as ISO
    strings like the rest of ARM. Passed through untouched they render as an 18-digit number.
    """
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    try:
        return (_EPOCH + timedelta(microseconds=n // 10)).date().isoformat()
    except (OverflowError, OSError):
        return None


def _strip_html(text: str | None) -> str:
    """Advisory bodies are HTML. The dashboard renders text, so flatten it here."""
    if not text:
        return ""
    out = re.sub(r"<br\s*/?>|</p>|</li>", " ", text, flags=re.IGNORECASE)
    out = re.sub(r"<[^>]+>", "", out)
    out = (
        out.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#39;", "'")
        .replace("\u2019", "'").replace("\u2018", "'")
        .replace("\u201c", '"').replace("\u201d", '"')
        .replace("\u2013", "-").replace("\u2014", "-")
    )
    return re.sub(r"\s+", " ", out).strip()


async def advisories(subscription_ids: list[str] | None = None,
                     top: int = 100) -> dict[str, Any]:
    """Active Azure advisories, and which of your resources each one affects."""
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
    if not subs:
        return {"empty": True, "reason": "No subscriptions in scope."}

    events = await _arg(EVENTS_QUERY, subs, top=500)
    impacted = await _arg(IMPACTED_QUERY, subs, top=2000) if events else []

    # Keyed by tracking id, which is what both tables share. Azure names an impacted resource
    # with an opaque hash, so the join has to go through the id path rather than the name.
    by_tracking: dict[str, list[dict[str, Any]]] = {}
    for row in impacted:
        key = str(row.get("tracking") or "")
        if not key:
            continue
        entry = {
            "id": row.get("resourceId"),
            "name": row.get("resourceName") or str(row.get("resourceId", "")).split("/")[-1],
            "type": str(row.get("resourceType") or "").split("/")[-1],
        }
        bucket = by_tracking.setdefault(key, [])
        # The same resource can appear once per subscription that sees the advisory.
        if not any(x["id"] == entry["id"] for x in bucket):
            bucket.append(entry)

    today = date.today()
    seen: dict[str, dict[str, Any]] = {}
    for e in events:
        # Resolved advisories describe something that already finished; they are history, not
        # a task, and leaving them in buries the two things that still need doing.
        if str(e.get("status", "")).lower() != "active":
            continue

        due = _from_ticks(e.get("dueTicks"))
        days_left = None
        if due:
            try:
                days_left = (date.fromisoformat(due) - today).days
            except ValueError:
                days_left = None

        headline = e.get("headline") or ""
        tracking = str(e.get("tracking") or "")
        resources = by_tracking.get(tracking, [])
        item = {
            "tracking": tracking,
            "headline": _strip_html(headline),
            "summary": _strip_html(e.get("summary"))[:600],
            "actions": _strip_html(e.get("actions"))[:400],
            "type": e.get("eventType"),
            "level": e.get("level"),
            "started": _from_ticks(e.get("startTicks")),
            "due": due,
            "days_left": days_left,
            # A date in the past is not a countdown. Azure leaves advisories active past their
            # deadline, and rendering "-117 days left" reads as a bug rather than as "overdue".
            "overdue": bool(days_left is not None and days_left < 0),
            "updated": _from_ticks(e.get("updatedTicks")),
            "retirement": bool(RETIREMENT_WORDS.search(headline)),
            "resources": resources[:50],
            "resource_count": len(resources),
            "subscriptionId": e.get("subscriptionId"),
        }

        # One row per advisory. Azure raises the same tracking id once per affected
        # subscription, so without this an estate of three subscriptions shows everything three
        # times and the counts read as three times the work.
        prior = seen.get(tracking)
        if prior is None or item["resource_count"] > prior["resource_count"]:
            if prior is not None:
                item["resources"] = prior["resources"] or item["resources"]
            seen[tracking] = item

    out = list(seen.values())

    # Soonest deadline first; anything without one goes last. A date you can miss outranks an
    # advisory that is merely informative.
    out.sort(key=lambda a: (a["days_left"] is None, a["days_left"] if a["days_left"] is not None else 0))

    retirements = [a for a in out if a["retirement"]]
    urgent = [a for a in out if a["days_left"] is not None and 0 <= a["days_left"] <= 90]
    overdue = [a for a in out if a["overdue"]]

    return {
        "advisories": out[:top],
        "counts": {
            "active": len(out),
            "retirements": len(retirements),
            "due_90_days": len(urgent),
            "overdue": len(overdue),
            "impacted_resources": sum(a["resource_count"] for a in out),
        },
        # The next date still ahead of us — an overdue one is not a deadline to plan for.
        "next_deadline": next(
            (a["due"] for a in out if a["days_left"] is not None and a["days_left"] >= 0), None
        ),
        "scanned": {"subscriptions": len(subs)},
        "note": (
            "From Azure Service Health, which raises advisories against the subscriptions "
            "actually affected. Resolved advisories are left out — they describe work that is "
            "already finished. Azure names impacted resources for some advisory types and not "
            "others; where none are listed, the advisory applies to the subscription."
        ),
    }

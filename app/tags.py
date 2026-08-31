"""Cost grouped by the tags on the resources that incurred it.

The interesting question is not "what did tag X cost" but "what did the resources carrying
these tags cost, together" — and that is a set question, asked repeatedly as someone adds and
removes tags. Answering it server-side would mean a round trip per click.

So the server answers once, with the pieces the client needs to answer every later question
itself: each resource, its cost, and which tag keys it carries. Selecting a tag is then an
intersection over a few thousand small objects, which is instant and stays instant offline.

Two things worth knowing about Azure tag data:

  * Tags arrive as a JSON object per cost row, e.g. {"Environment":"prod","Owner":"ana"}. A
    resource appears on many rows, so cost has to be summed per resource before anything is
    counted, or a resource billed daily looks thirty times more expensive than one billed once.

  * Tag keys are not case-normalised by Azure: `Environment`, `environment` and `ENVIRONMENT`
    are three distinct keys on three resources, and treating them as one would silently merge
    what a governance report needs to keep apart. They are kept separate, and the UI shows them
    as they are.

  * **A tag only reaches cost data by riding on a usage record.** Tagging a resource does not
    backfill the usage it has already emitted, and a resource emitting no usage — a deallocated
    VM, a free tier, anything stopped — carries its tags nowhere. So a tag can be correct, live
    on the resource, and completely absent from cost. Showing only what cost data knows means
    someone who tagged something an hour ago sees nothing and concludes the feature is broken.
    `live_tag_keys` exists to close that gap: it asks Azure what tags actually exist so the tab
    can say "this tag is real, it just has no spend behind it in this window" rather than
    staying silent.
"""
from __future__ import annotations

import json
import logging
from typing import Any

log = logging.getLogger("cloudlens.tags")

# Enough to cover a large estate, small enough to stay comfortably inside a JSON response and
# a browser's memory. Beyond this the response says it is partial rather than quietly trimming.
MAX_RESOURCES = 8000

# Per-resource-per-day cells. A large estate over 90 days can reach seven figures, which is a
# response nobody wants to parse and a browser tab nobody wants to scroll. Past this the daily
# detail is dropped and the response says so, so the chart can fall back to estate-wide totals
# rather than drawing a series built from a silently truncated half of the data.
MAX_DAILY_CELLS = 200_000

# Tag keys currently on resources, whatever they cost. Counts resources, not usage rows.
LIVE_KEYS_QUERY = """
    Resources
    | where isnotnull(tags) and array_length(bag_keys(tags)) > 0
    | mv-expand kv = tags
    | extend k = tostring(bag_keys(kv)[0])
    | where isnotempty(k)
    | summarize n = count() by k
    | order by n desc
"""


async def live_tag_keys(scope: list[str] | None = None) -> list[dict[str, Any]]:
    """Tag keys that exist on resources right now, from Resource Graph.

    Fails soft. This is a supplement to a warehouse answer that is already complete on its own
    terms, so an ARM hiccup, a throttle or a missing permission should cost the caller the
    extra context and nothing else — never the tab.

    `None` means "everything the server can see", matching the convention the rest of the app
    uses. An explicitly empty list means the opposite — nothing is in scope — and must not
    widen to the whole estate, which is what `scope or <all>` alone would do.
    """
    from .cost import list_subscriptions
    from .waste import _arg

    if scope is not None and not scope:
        return []

    try:
        subs = scope or [s["id"] for s in (await list_subscriptions())["subscriptions"]]
        if not subs:
            return []
        rows = await _arg(LIVE_KEYS_QUERY, subs, top=500)
    except Exception as exc:  # noqa: BLE001 - context is optional, the tab is not
        log.info("live tag keys unavailable, continuing without them: %s", str(exc)[:200])
        return []

    out = []
    for row in rows:
        key = str(row.get("k") or "").strip()
        if key:
            out.append({"key": key, "resources": int(row.get("n") or 0)})
    return out


def merge_live_keys(payload: dict[str, Any], live: list[dict[str, Any]]) -> dict[str, Any]:
    """Add the keys Azure knows about that cost data has never seen.

    Kept separate from `keys` rather than merged into it. They are not comparable: every entry
    in `keys` has a cost behind it, and these have none, so folding them together would put
    zero-cost rows in a list someone reads as a spend breakdown.
    """
    if not live:
        return payload

    costed = {k["key"] for k in payload.get("keys", [])}
    payload["uncosted"] = [k for k in live if k["key"] not in costed]
    payload["live_keys"] = len(live)
    return payload



def cost_by_tag(scope: list[str] | None = None, days: int = 30,
                currency: str | None = None) -> dict[str, Any]:
    """Every tagged resource in the window, with its cost, its tag keys and its daily spend."""
    from .warehouse import warehouse

    with warehouse.reader(scope=scope, currency=currency) as r:
        # Anchored to the newest data, not to today. Cost lands a day or two late, so a window
        # ending at CURRENT_DATE quietly drops the most recent complete day -- and on a
        # warehouse that has not been refreshed this week it returns almost nothing, which
        # reads as "you have no tagged resources" rather than "this data is stale".
        edge = r.rows('SELECT max("ChargePeriodStart") AS hi FROM costs')
        hi = edge[0]["hi"] if edge and edge[0]["hi"] else None
        if hi is None:
            return _empty(days)

        window = f"\"ChargePeriodStart\" >= date '{hi}' - INTERVAL {int(days)} DAY"

        rows = r.rows(
            f"""
            SELECT
              COALESCE(NULLIF("ResourceId", ''), "ResourceName", '(unnamed)') AS id,
              MAX(COALESCE(NULLIF("ResourceName", ''), '(unnamed)'))          AS name,
              MAX(COALESCE("ServiceName", ''))                                AS service,
              MAX(COALESCE("SubAccountName", ''))                             AS subscription,
              MAX(COALESCE("ResourceGroup", ''))                              AS resource_group,
              COALESCE("BillingCurrency", 'unknown')                          AS currency,
              SUM(COALESCE("BilledCost", 0))                                  AS cost,
              MAX(COALESCE("Tags", ''))                                       AS tags
            FROM costs
            WHERE {window}
            GROUP BY 1, 6
            """,
            limit=MAX_RESOURCES + 1,
        )

        # Per resource per day. Needed for both the trend and the day drill-down, and both are
        # driven entirely client-side: the whole point of the tab is that changing the tag
        # selection re-answers instantly, and a daily series fetched per selection would put a
        # round trip behind every click.
        daily = r.rows(
            f"""
            SELECT
              COALESCE(NULLIF("ResourceId", ''), "ResourceName", '(unnamed)') AS id,
              "ChargePeriodStart"                                             AS day,
              COALESCE("BillingCurrency", 'unknown')                          AS currency,
              SUM(COALESCE("BilledCost", 0))                                  AS cost
            FROM costs
            WHERE {window}
            GROUP BY 1, 2, 3
            HAVING SUM(COALESCE("BilledCost", 0)) <> 0
            """,
            limit=MAX_DAILY_CELLS + 1,
        )

    truncated = len(rows) > MAX_RESOURCES
    if truncated:
        log.info("cost_by_tag: more than %d resources; response is partial", MAX_RESOURCES)
        rows = rows[:MAX_RESOURCES]

    # Adding EUR to USD produces a confident, meaningless number. Report in the currency that
    # dominates the estate and say so, rather than summing across them.
    currency, mixed = _dominant(rows)
    rows = [r for r in rows if (r.get("currency") or "unknown") == currency] if mixed else rows

    daily_dropped = len(daily) > MAX_DAILY_CELLS
    if daily_dropped:
        log.info("cost_by_tag: over %d daily cells; dropping per-resource detail", MAX_DAILY_CELLS)
        daily = []
    if mixed:
        daily = [c for c in daily if (c.get("currency") or "unknown") == currency]

    # Dates come from the data, not from a generated range. A day Azure never billed should not
    # appear as a zero on the chart -- that reads as "we spent nothing", when the truth is that
    # nothing was recorded. A gap stays a gap.
    dates = sorted({str(c["day"]) for c in daily})
    index = {d: i for i, d in enumerate(dates)}

    per_resource: dict[str, list[list[float]]] = {}
    daily_total = [0.0] * len(dates)
    for cell in daily:
        i = index[str(cell["day"])]
        cost = float(cell.get("cost") or 0.0)
        per_resource.setdefault(cell["id"], []).append([i, round(cost, 4)])
        daily_total[i] += cost

    resources: list[dict[str, Any]] = []
    key_totals: dict[str, dict[str, Any]] = {}
    tagged_cost = 0.0
    tagged_count = 0
    untagged_cost = 0.0
    untagged_count = 0

    # Values are interned. The same "raviverma@microsoft.com" or app-insights resource id can
    # sit on hundreds of resources, and repeating the string each time is most of the response
    # for none of the information.
    pool: list[str] = []
    pool_index: dict[str, int] = {}

    def intern(value: str) -> int:
        idx = pool_index.get(value)
        if idx is None:
            idx = len(pool)
            pool_index[value] = idx
            pool.append(value)
        return idx

    for row in rows:
        pairs = _pairs(row.get("tags"))
        keys = [k for k, _ in pairs]
        cost = float(row.get("cost") or 0.0)

        # Untagged resources are carried too, with an empty key list.
        #
        # They are not filler. The daily chart is estate-wide when no tag is selected, and the
        # day drill-down reads from this list -- so leaving them out made a bar and the table
        # underneath it disagree about the same day, with the table quietly the smaller of the
        # two. A resource with no tags still spent money on the 18th.
        resources.append({
            "id": row["id"],
            "name": row.get("name") or "(unnamed)",
            "service": row.get("service") or "",
            "subscription": row.get("subscription") or "",
            "group": row.get("resource_group") or "",
            "cost": round(cost, 2),
            "keys": keys,
            # Parallel to `keys`: v[i] indexes the value of keys[i] in `values`.
            "v": [intern(v) for _, v in pairs],
            "d": per_resource.get(row["id"], []),
        })

        if not keys:
            untagged_cost += cost
            untagged_count += 1
            continue

        tagged_cost += cost
        tagged_count += 1
        for k, v in pairs:
            slot = key_totals.setdefault(
                k, {"key": k, "cost": 0.0, "resources": 0, "distinct": set()})
            slot["cost"] += cost
            slot["resources"] += 1
            slot["distinct"].add(v)

    keys_out = sorted(key_totals.values(), key=lambda k: (-k["cost"], k["key"].lower()))
    for k in keys_out:
        k["cost"] = round(k["cost"], 2)
        # How many values sit under this key, so the picker can show which keys are worth
        # opening. A key with one value is a label; a key with nine is a breakdown.
        k["values"] = len(k.pop("distinct"))

    return {
        "keys": keys_out,
        "resources": resources,
        "values": pool,
        "currency": currency,
        "mixed": mixed,
        "tagged": {"cost": round(tagged_cost, 2), "resources": tagged_count},
        "untagged": {"cost": round(untagged_cost, 2), "resources": untagged_count},
        "total": round(tagged_cost + untagged_cost, 2),
        "days": days,
        "truncated": truncated,
        "dates": dates,
        "daily_total": [round(v, 2) for v in daily_total],
        "daily_dropped": daily_dropped,
        "latest": str(hi),
    }


def _empty(days: int) -> dict[str, Any]:
    return {"keys": [], "resources": [], "values": [], "currency": None, "mixed": False,
            "tagged": {"cost": 0.0, "resources": 0},
            "untagged": {"cost": 0.0, "resources": 0},
            "total": 0.0, "days": days, "truncated": False,
            "dates": [], "daily_total": [], "daily_dropped": False, "latest": None}


def _dominant(rows: list[dict]) -> tuple[str | None, bool]:
    totals: dict[str, float] = {}
    for r in rows:
        cur = r.get("currency") or "unknown"
        totals[cur] = totals.get(cur, 0.0) + float(r.get("cost") or 0.0)
    if not totals:
        return None, False
    if len(totals) == 1:
        return next(iter(totals)), False
    return max(totals, key=lambda c: totals[c]), True


def _pairs(raw: Any) -> list[tuple[str, str]]:
    """Tag key/value pairs from one cost row's tag column, sorted by key.

    Values matter as much as keys. `Owner` is not a cost centre — `Owner=ana` and `Owner=bob`
    are two cost centres that happen to share a key, and answering "what does Owner cost" by
    adding them together is a number nobody asked for.

    Azure has shipped this field in more than one shape over the years, and an export that
    predates the current one is still a perfectly good export. A row whose tags cannot be read
    is treated as untagged rather than crashing the tab — one malformed row should not cost
    someone the whole view.
    """
    if not raw:
        return []
    text = str(raw).strip()
    if not text or text in ("{}", "[]", "null"):
        return []

    out: dict[str, str] = {}

    # The usual case: a JSON object.
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return []
        if not isinstance(parsed, dict):
            return []
        for k, v in parsed.items():
            name = str(k).strip()
            if name:
                out[name] = "" if v is None else str(v).strip()
        return sorted(out.items())

    # Older exports emit `"key": "value"; "key2": "value2"` without the braces.
    for part in text.replace(";", ",").split(","):
        name, sep, value = part.partition(":")
        if not sep:
            continue
        name = name.strip().strip('"').strip()
        if name:
            out[name] = value.strip().strip('"').strip()
    return sorted(out.items())


def _keys(raw: Any) -> list[str]:
    """Just the key names. Kept for callers that do not care about values."""
    return [k for k, _ in _pairs(raw)]

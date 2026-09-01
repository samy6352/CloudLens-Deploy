"""Azure Cost Management queries, scoped to whatever the caller can see.

Deliberately narrow: this module answers money questions and nothing else. It runs as the
**signed-in person** when one is present — their delegated ARM token is carried on a ContextVar
and used in preference to the server's own credential — and as the server (az login / managed
identity) for background work. Either way "subscriptions I have access to" is simply whatever
Azure returns, so there is no separate permission model here to get wrong.

Everything here is a read. Nothing in this file can change an Azure resource.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

import httpx

log = logging.getLogger("cloudlens.cost")

ARM = "https://management.azure.com"
COST_API = "2024-08-01"
BUDGET_API = "2024-08-01"
SUBS_API = "2022-12-01"

# Cost Management throttles hard — a handful of queries a minute. Keep concurrency modest and
# always honour Retry-After, or multi-subscription questions become flaky for no good reason.
_CONCURRENCY = asyncio.Semaphore(4)

# Billed cost is recomputed a few times a day at most, so a short cache costs nothing in accuracy
# and removes whole seconds from follow-up questions ("...and by resource group?"), which
# otherwise re-run the identical query per subscription.
_CACHE_TTL = float(os.getenv("CACHE_TTL_SECONDS", "300"))
# (expiry, payload, url). The URL is carried because the key is a hash over the caller and the
# whole request, which cannot be searched: without it there is no way to drop the entries for
# one resource after writing to it, and the only alternatives are keeping a stale answer or
# throwing away everyone's cache.
_cache: dict[str, tuple[float, dict, str]] = {}


def cache_stats() -> dict[str, int]:
    live = sum(1 for exp, _, _ in _cache.values() if exp > time.time())
    return {"entries": live}


def clear_cache() -> None:
    _cache.clear()


def forget(url_fragment: str) -> int:
    """Drop cached reads whose URL contains `url_fragment`, across every caller.

    Called after a write. A cached list from before the write is not merely stale, it actively
    contradicts what the caller just did — reading back a budget they have just changed and
    being shown the old amount reads as the write having silently failed. Dropped for everyone
    because the resource changed for everyone, not just for whoever wrote it.
    """
    gone = [k for k, (_, _, url) in _cache.items() if url_fragment in url]
    for k in gone:
        del _cache[k]
    return len(gone)


class CostError(RuntimeError):
    """Something the caller can act on; surfaced to the model as text."""


# Whose Azure access a call runs under. Set per request when someone is signed in with Entra,
# unset for the server's own background work. A ContextVar rather than a parameter because it
# has to reach every live Azure call in this module and in waste.py without threading an
# argument through two dozen signatures — and because asyncio copies the context into tasks,
# so the fan-out inside cost_summary() inherits it automatically.
_caller: ContextVar[Caller | None] = ContextVar("caller", default=None)
_caller_id: ContextVar[str] = ContextVar("caller_id", default="server")

# A subscription id in an ARM URL, which is how a call is matched to the tenant that can
# authorise it. Every ARM path that is scoped to a subscription starts this way.
_SUB_IN_URL = re.compile(r"/subscriptions/([0-9a-fA-F-]{36})\b")


@dataclass
class Caller:
    """One signed-in person's Azure access, which may span several tenants.

    An ARM token is issued by one tenant and refused by every other, so a person with access in
    three directories genuinely holds three tokens. Keeping a single token here would work for
    whichever tenant happened to issue it and quietly 401 on the rest — indistinguishable, from
    the outside, from not having access at all.

    `by_subscription` maps each subscription to the token that can actually read it. `fallback`
    is the home-tenant token, used for tenant-wide calls like listing subscriptions.
    """

    fallback: str
    by_subscription: dict[str, str] = field(default_factory=dict)
    # The union across tenants, when it is already known. GET /subscriptions answers for the
    # tenant that issued the token and no other, so for a multi-tenant caller it is not the
    # question we mean to ask — whoever assembled the token map has the real answer.
    subscriptions: list[dict[str, Any]] | None = None

    def token_for(self, url: str) -> str:
        match = _SUB_IN_URL.search(url)
        if match:
            token = self.by_subscription.get(match.group(1).lower())
            if token:
                return token
        return self.fallback


def act_as(token: str | Caller, caller_id: str) -> tuple[Any, Any]:
    """Run subsequent Azure calls in this context as the holder of `token`.

    A bare token is still accepted: single-tenant callers, and the tests, have exactly one.
    """
    caller = token if isinstance(token, Caller) else Caller(fallback=token)
    return _caller.set(caller), _caller_id.set(caller_id)


def stop_acting(reset: tuple[Any, Any]) -> None:
    _caller.reset(reset[0])
    _caller_id.reset(reset[1])


def acting_as_caller() -> bool:
    """Whether live Azure calls here carry the signed-in person's own token.

    Without delegated ARM the call runs as the app's managed identity instead, which makes
    "you need this role" the wrong sentence: the principal Azure refused is the app, not the
    person reading the message.
    """
    return _caller.get() is not None


def acting_as() -> str:
    return _caller_id.get()


def caller_token(url: str = "") -> str | None:
    """The signed-in person's ARM token for this URL, or None if we are not acting as anyone.

    A subscription-shaped URL picks the token issued by the tenant that owns that subscription;
    anything else gets the home-tenant one. Callers that hold a single token are unaffected —
    `Caller.token_for` returns the fallback in that case.
    """
    caller = _caller.get()
    if caller is None:
        return None
    return caller.token_for(url) if url else caller.fallback


class Azure:
    """Minimal ARM client using the caller's credential, or the server's when there isn't one."""

    def __init__(self) -> None:
        self._credential: Any | None = None
        self._token: tuple[str, float] | None = None

    async def _bearer(self, url: str = "") -> str:
        import time

        # A signed-in person's own token wins, so Azure applies their RBAC rather than ours.
        # Which token depends on the URL: the one issued by the tenant that owns the
        # subscription being read.
        caller = _caller.get()
        if caller:
            return caller.token_for(url)

        if self._token and self._token[1] > time.time() + 120:
            return self._token[0]

        if self._credential is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

        tok = await self._credential.get_token(f"{ARM}/.default")
        self._token = (tok.token, float(tok.expires_on))
        return tok.token

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def request(self, method: str, url: str, *, json_body: dict | None = None,
                      params: dict | None = None, attempts: int = 5, cache: bool = True) -> dict:
        key = ""
        if cache and _CACHE_TTL > 0:
            # The caller is part of the key. Two people can ask for the same URL and be entitled
            # to different answers, so a cache shared across identities would leak across RBAC.
            blob = json.dumps([_caller_id.get(), method, url, json_body, params],
                              sort_keys=True, default=str)
            key = hashlib.sha256(blob.encode()).hexdigest()
            hit = _cache.get(key)
            if hit and hit[0] > time.time():
                return hit[1]

        headers = {"Authorization": f"Bearer {await self._bearer(url)}",
                   "Content-Type": "application/json"}

        async with _CONCURRENCY:
            async with httpx.AsyncClient(timeout=90) as client:
                for attempt in range(attempts):
                    r = await client.request(method, url, json=json_body, params=params, headers=headers)

                    if r.status_code in (429, 503):
                        if attempt == attempts - 1:
                            raise CostError(
                                "Azure is still rate-limiting cost queries after several retries. "
                                "Try a narrower question, or wait a minute."
                            )
                        await asyncio.sleep(_retry_after(r, attempt))
                        continue

                    if r.status_code in (401, 403):
                        # 401 and 403 are different problems and were reported as the same one.
                        #
                        # 403 is "this principal is not allowed to do that" — a role to grant.
                        # 401 is "Azure would not accept the token at all", which happens when
                        # the token is expired, issued for the wrong tenant, or missing a claim
                        # the operation needs. Telling someone to grant Cost Management Reader
                        # when they already hold Cost Management Contributor sends them to check
                        # a role assignment that was never the problem — which is exactly what
                        # happened with scheduled exports: every GET succeeded on the same
                        # token, the PUT came back 401, and the message blamed a missing role.
                        #
                        # Azure's own explanation is in the body and in WWW-Authenticate. It was
                        # being discarded, so the one piece of evidence that could distinguish
                        # the two cases never reached anybody.
                        detail = (r.text or "").strip()[:400]
                        hint = r.headers.get("WWW-Authenticate", "")
                        if r.status_code == 403:
                            raise CostError(
                                "Azure refused this on that scope: the identity making the call "
                                "does not have the necessary role. Cost reads need Cost "
                                "Management Reader; creating or running an export needs Cost "
                                "Management Contributor. "
                                + (f"Azure said: {detail}" if detail else "")
                            )
                        raise CostError(
                            f"Azure rejected the credential for {method} on this resource "
                            f"(401). This is the token being refused rather than a missing "
                            f"role, so check the tenant it was issued for and whether it has "
                            f"expired. "
                            + (f"Azure said: {detail}" if detail else "")
                            + (f" Challenge: {hint[:200]}" if hint else "")
                        )
                    if r.status_code >= 400:
                        raise CostError(f"Azure returned {r.status_code}: {r.text[:300]}")

                    payload = r.json() if r.content else {}
                    if key:
                        _cache[key] = (time.time() + _CACHE_TTL, payload, url)
                    return payload
        raise CostError("Azure request failed after retries.")


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    for h in ("Retry-After", "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after"):
        v = resp.headers.get(h)
        if v:
            try:
                return min(float(v), 60.0)
            except ValueError:
                pass
    return min(2.0 * (2**attempt), 30.0)


azure = Azure()


# --------------------------------------------------------------------------- helpers
def month_window(months_back: int) -> tuple[str, str]:
    """0 = this month to date, 1 = last complete month, 2 = the month before that."""
    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    if months_back <= 0:
        return start.isoformat(), today.isoformat()
    for _ in range(months_back):
        start = (start - timedelta(days=1)).replace(day=1)
    last = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start.isoformat(), last.isoformat()


def _rows(payload: dict) -> tuple[list[dict], str | None, str | None]:
    props = payload.get("properties", {})
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = [dict(zip(cols, r)) for r in props.get("rows", [])]
    cost_col = next((c for c in cols if c and c.lower() in ("totalcost", "cost", "pretaxcost")), None)
    currency = next((r.get("Currency") for r in rows if r.get("Currency")), None)
    return rows, cost_col, currency


async def _query(sub: str, body: dict) -> dict:
    return await azure.request(
        "POST", f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/query?api-version={COST_API}",
        json_body=body,
    )


# --------------------------------------------------------------------------- tools
async def list_subscriptions() -> dict[str, Any]:
    """Every subscription the signed-in user can see.

    For a caller whose access spans tenants this cannot be a single ARM call: /subscriptions is
    answered by the tenant that issued the token, so asking once would silently drop every
    subscription in every other directory. Where that enumeration has already been done, use it.
    """
    caller = _caller.get()
    if caller is not None and caller.subscriptions is not None:
        subs = [s for s in caller.subscriptions if s.get("state", "Enabled") == "Enabled"]
        return {"subscriptions": subs, "count": len(subs)}

    payload = await azure.request("GET", f"{ARM}/subscriptions", params={"api-version": SUBS_API})
    subs = [
        {"id": s["subscriptionId"], "name": s.get("displayName"), "state": s.get("state")}
        for s in payload.get("value", [])
        if s.get("state") == "Enabled"
    ]
    return {"subscriptions": subs, "count": len(subs)}


async def list_tenants(token: str) -> list[dict[str, Any]]:
    """Every Entra directory this token's holder can reach.

    The starting point for cross-tenant access: a home-tenant token is enough to *list* the
    other directories, though not to read anything in them — each needs its own token, which is
    what the caller does next with this list.
    """
    reset = act_as(token, acting_as())
    try:
        payload = await azure.request(
            "GET", f"{ARM}/tenants", params={"api-version": "2022-12-01"}, cache=False)
    finally:
        stop_acting(reset)
    return [
        {"id": t["tenantId"],
         "name": t.get("displayName") or t.get("defaultDomain") or t["tenantId"]}
        for t in payload.get("value", [])
        if t.get("tenantId")
    ]


async def subscriptions_with(token: str) -> list[dict[str, Any]]:
    """The subscriptions a single tenant's token is good for."""
    reset = act_as(token, acting_as())
    try:
        payload = await azure.request(
            "GET", f"{ARM}/subscriptions", params={"api-version": SUBS_API}, cache=False)
    finally:
        stop_acting(reset)
    return [
        {"id": s["subscriptionId"], "name": s.get("displayName"), "state": s.get("state"),
         "tenant": s.get("tenantId", "")}
        for s in payload.get("value", [])
        if s.get("state") == "Enabled"
    ]


async def subscriptions_for_principal(object_id: str) -> list[dict[str, Any]]:
    """Which subscriptions a given person has any role on, asked of Azure itself.

    This is how "what may they see" is answered when the app cannot hold a delegated token for
    them — a tenant that requires administrator approval for Azure Resource Manager delegation
    leaves no other honest way to find out. The `assignedTo` filter is transitive, so access
    granted through a group counts exactly as Azure counts it, and none of it is a list we
    maintain and get wrong.

    Bounded by what this app's own identity can read: a subscription it has no Reader on cannot
    be inspected, so it is never offered to anyone.
    """
    if not object_id:
        return []

    visible = (await list_subscriptions())["subscriptions"]

    async def has_access(sub: dict) -> dict | None:
        url = f"{ARM}/subscriptions/{sub['id']}/providers/Microsoft.Authorization/roleAssignments"
        try:
            payload = await azure.request(
                "GET", url,
                params={"api-version": "2022-04-01", "$filter": f"assignedTo('{object_id}')"},
            )
        except CostError as exc:
            # One unreadable subscription must not hide the rest, or a single missing Reader
            # role would silently empty somebody's picker.
            log.warning("could not read role assignments on %s: %s", sub["id"], str(exc)[:150])
            return None
        return sub if payload.get("value") else None

    found = await asyncio.gather(*(has_access(s) for s in visible))
    return [s for s in found if s]


async def _resolve(subscription_ids: list[str] | None) -> list[dict]:
    """Turn a possibly-empty selection into concrete subscriptions."""
    known = (await list_subscriptions())["subscriptions"]
    if not subscription_ids:
        return known
    wanted = {s.lower() for s in subscription_ids}
    picked = [s for s in known if s["id"].lower() in wanted or (s["name"] or "").lower() in wanted]
    if not picked:
        raise CostError(
            f"None of {subscription_ids} match a subscription you can see. "
            f"Available: {', '.join(s['name'] or s['id'] for s in known[:10])}"
        )
    return picked


async def cost_summary(
    subscription_ids: list[str] | None = None,
    months_back: int = 1,
    group_by: Literal["ServiceName", "ResourceGroupName", "ResourceId", "ResourceLocation",
                      "MeterCategory", "None"] = "ServiceName",
    metric: Literal["ActualCost", "AmortizedCost"] = "ActualCost",
    top: int = 15,
) -> dict[str, Any]:
    """Spend for a period, broken down by a dimension, aggregated across subscriptions."""
    subs = await _resolve(subscription_ids)
    start, end = month_window(months_back)

    dataset: dict[str, Any] = {
        "granularity": "None",
        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
    }
    if group_by != "None":
        dataset["grouping"] = [{"type": "Dimension", "name": group_by}]

    body = {
        "type": metric,
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": dataset,
    }

    results = await asyncio.gather(*(_query(s["id"], body) for s in subs), return_exceptions=True)

    combined: dict[str, float] = {}
    per_sub: list[dict] = []
    currency: str | None = None
    errors: list[str] = []

    for sub, res in zip(subs, results):
        if isinstance(res, Exception):
            errors.append(f"{sub['name']}: {res}")
            continue
        rows, cost_col, cur = _rows(res)
        currency = currency or cur
        total = 0.0
        for r in rows:
            amount = float(r.get(cost_col) or 0)
            key = str(r.get(group_by, "(ungrouped)")) if group_by != "None" else "Total"
            combined[key] = combined.get(key, 0.0) + amount
            total += amount
        per_sub.append({"subscription": sub["name"], "id": sub["id"], "total": round(total, 2)})

    ranked = sorted(combined.items(), key=lambda kv: kv[1], reverse=True)

    # If every subscription query failed we have no data at all. Returning a total of 0 here
    # would be reported by the model as "you spent nothing", which is a confidently wrong answer
    # — the exact failure mode this app must not have. Fail loudly instead.
    if errors and not per_sub:
        raise CostError(
            "Could not retrieve cost data for any subscription, so there is no total to report. "
            + " | ".join(errors[:3])
        )

    return {
        "period": {"start": start, "end": end,
                   "label": "month to date" if months_back == 0 else f"{months_back} month(s) back"},
        "metric": metric,
        "currency": currency,
        "grand_total": round(sum(combined.values()), 2),
        "grouped_by": group_by,
        "breakdown": [{"name": k, "cost": round(v, 2)} for k, v in ranked[:top]],
        "other_count": max(0, len(ranked) - top),
        "by_subscription": sorted(per_sub, key=lambda x: x["total"], reverse=True),
        "subscriptions_queried": len(subs),
        "subscriptions_returned": len(per_sub),
        # Partial data must be visible to the model so it can caveat the answer.
        "incomplete": bool(errors) or None,
        "errors": errors or None,
    }


async def cost_trend(subscription_ids: list[str] | None = None, months: int = 6) -> dict[str, Any]:
    """Monthly totals over the last N months, for spotting direction of travel."""
    months = max(2, min(int(months), 12))
    subs = await _resolve(subscription_ids)

    today = datetime.now(timezone.utc).date()
    start = today.replace(day=1)
    for _ in range(months - 1):
        start = (start - timedelta(days=1)).replace(day=1)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{start.isoformat()}T00:00:00Z", "to": f"{today.isoformat()}T23:59:59Z"},
        "dataset": {"granularity": "Monthly",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
    }

    results = await asyncio.gather(*(_query(s["id"], body) for s in subs), return_exceptions=True)

    monthly: dict[str, float] = {}
    currency = None
    errors: list[str] = []
    ok = 0
    for sub, res in zip(subs, results):
        if isinstance(res, Exception):
            errors.append(f"{sub['name']}: {res}")
            continue
        ok += 1
        rows, cost_col, cur = _rows(res)
        currency = currency or cur
        for r in rows:
            raw = str(r.get("BillingMonth") or r.get("UsageDate") or "")
            key = raw[:7] if "-" in raw else (raw[:6] if len(raw) >= 6 else raw)
            if len(key) == 6 and key.isdigit():
                key = f"{key[:4]}-{key[4:6]}"
            monthly[key] = monthly.get(key, 0.0) + float(r.get(cost_col) or 0)

    if errors and not ok:
        raise CostError("Could not retrieve trend data for any subscription. " + " | ".join(errors[:3]))

    series = [{"month": m, "cost": round(c, 2)} for m, c in sorted(monthly.items())]
    change = None
    if len(series) >= 2 and series[-2]["cost"]:
        change = round((series[-1]["cost"] - series[-2]["cost"]) / series[-2]["cost"] * 100, 1)

    return {
        "months": series,
        "currency": currency,
        "latest_vs_previous_pct": change,
        "subscriptions_returned": ok,
        "incomplete": bool(errors) or None,
        "errors": errors or None,
        "note": "The final month may be partial if it is the current month.",
    }


async def cost_changes(subscription_ids: list[str] | None = None, top: int = 10) -> dict[str, Any]:
    """What moved: last complete month vs the month before, by service.

    This is usually the real question behind "why has my bill gone up".
    """
    # Two full multi-subscription passes; run them together rather than one after the other.
    this_month, prior = await asyncio.gather(
        cost_summary(subscription_ids, months_back=1, group_by="ServiceName", top=200),
        cost_summary(subscription_ids, months_back=2, group_by="ServiceName", top=200),
    )

    now = {b["name"]: b["cost"] for b in this_month["breakdown"]}
    was = {b["name"]: b["cost"] for b in prior["breakdown"]}

    deltas = []
    for name in set(now) | set(was):
        a, b = was.get(name, 0.0), now.get(name, 0.0)
        deltas.append({
            "service": name,
            "previous": round(a, 2),
            "current": round(b, 2),
            "change": round(b - a, 2),
            "change_pct": round((b - a) / a * 100, 1) if a else None,
            "status": "new" if not a else ("stopped" if not b else "changed"),
        })

    deltas.sort(key=lambda d: d["change"], reverse=True)
    return {
        "comparing": {"current": this_month["period"], "previous": prior["period"]},
        "currency": this_month["currency"],
        "total_change": round(this_month["grand_total"] - prior["grand_total"], 2),
        "increases": [d for d in deltas if d["change"] > 0][:top],
        "decreases": [d for d in deltas if d["change"] < 0][-top:][::-1],
    }


async def cost_forecast(subscription_ids: list[str] | None = None, days_ahead: int = 30) -> dict[str, Any]:
    """Projected spend over the coming days, based on recent usage."""
    subs = await _resolve(subscription_ids)
    days_ahead = max(1, min(int(days_ahead), 90))
    today = datetime.now(timezone.utc).date()
    end = today + timedelta(days=days_ahead)

    body = {
        "type": "ActualCost",
        "timeframe": "Custom",
        "timePeriod": {"from": f"{today}T00:00:00Z", "to": f"{end}T23:59:59Z"},
        "dataset": {"granularity": "Daily",
                    "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}}},
        "includeActualCost": True,
        "includeFreshPartialCost": False,
    }

    async def one(sub: str) -> tuple[float, str | None]:
        payload = await azure.request(
            "POST",
            f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/forecast?api-version={COST_API}",
            json_body=body,
        )
        rows, cost_col, currency = _rows(payload)
        return sum(float(r.get(cost_col) or 0) for r in rows), currency

    results = await asyncio.gather(*(one(s["id"]) for s in subs), return_exceptions=True)
    answered = [r for r in results if not isinstance(r, Exception)]
    total = sum(amount for amount, _ in answered)
    failed = [s["name"] for s, r in zip(subs, results) if isinstance(r, Exception)]

    # Named only when every subscription that answered agrees on it. The total above is a bare
    # sum, so claiming a currency for a mixed estate would put one symbol in front of a number
    # that is not denominated in any single one of them. Unknown is the honest answer there, and
    # callers already have to handle it for the case where nothing answered at all.
    seen = {c for _, c in answered if c}
    currency = seen.pop() if len(seen) == 1 else None

    return {
        "period": {"start": today.isoformat(), "end": end.isoformat(), "days": days_ahead},
        "projected_total": round(total, 2),
        "currency": currency,
        "subscriptions_included": len(subs) - len(failed),
        "no_forecast_available": failed or None,
    }


async def budgets(subscription_ids: list[str] | None = None) -> dict[str, Any]:
    """Budgets and how spend is tracking against them.

    Reported per budget rather than as a bare list of amounts, because the question anyone
    opens this with is "am I over?" — which needs the percentage, the headroom left, and what
    the budget actually watches, all of which the raw ARM shape leaves to the caller.
    """
    subs = await _resolve(subscription_ids)

    async def one(sub: dict) -> list[dict]:
        payload = await azure.request(
            "GET", f"{ARM}/subscriptions/{sub['id']}/providers/Microsoft.Consumption/budgets",
            params={"api-version": BUDGET_API},
        )
        out = []
        for b in payload.get("value", []):
            p = b.get("properties", {})
            spend = p.get("currentSpend") or {}
            cur, amt = spend.get("amount"), p.get("amount")

            # Zero spend is a fact, not a missing one. Treating it as unknown — which a plain
            # truthiness check does — reported every brand-new budget as "—", the same as one
            # Azure genuinely has no figure for, so a budget that was working looked broken.
            known = cur is not None and amt
            pct = round(cur / amt * 100, 1) if known else None
            period = p.get("timePeriod") or {}
            notifications = p.get("notifications") or {}

            out.append({
                "subscription": sub["name"],
                "subscription_id": sub["id"],
                "name": b.get("name"),
                "amount": amt,
                "current_spend": round(cur, 2) if cur is not None else None,
                "remaining": round(amt - cur, 2) if known else None,
                "currency": spend.get("unit"),
                "time_grain": p.get("timeGrain"),
                "percent_used": pct,
                "status": _budget_status(pct),
                "start": (period.get("startDate") or "")[:10] or None,
                "end": (period.get("endDate") or "")[:10] or None,
                "filter": _filter_words(p.get("filter")),
                # Kept alongside the words so the trend can honour the same filter the budget
                # does. Without it a tag-scoped budget would be drawn against whole-subscription
                # spend: on this estate one such budget tracks $14 while its subscription spends
                # $466, and a chart showing the latter under a headline saying the former is the
                # kind of contradiction that makes a reader stop trusting the page.
                "filter_raw": p.get("filter") or None,
                "alerts": len(notifications),
                # De-duplicated: Azure holds separate notifications for an actual and a
                # forecast alert at the same percentage, and listing "125%, 125%" reads as a
                # mistake rather than as two rules that happen to share a threshold.
                "alerting": sorted({
                    n.get("threshold") for n in notifications.values()
                    if isinstance(n, dict) and n.get("threshold") is not None
                }),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs), return_exceptions=True)
    found = [b for r in results if not isinstance(r, Exception) for b in r]

    # Worst first. A list in Azure's arbitrary order buries the one budget that is blown behind
    # four that are fine, which is the opposite of what the page is for.
    found.sort(key=lambda b: (b["percent_used"] is None, -(b["percent_used"] or 0)))
    over = [b for b in found if b["status"] == "over"]
    return {
        "budgets": found,
        "count": len(found),
        "over": len(over),
        "near": len([b for b in found if b["status"] == "near"]),
        "note": "No budgets defined on these subscriptions." if not found else None,
    }


def _budget_status(percent: float | None) -> str:
    """Where a budget stands, as one word.

    The thresholds are the same ones the portal alerts on by default, so the colour on screen
    agrees with the email someone will have received rather than inventing a second opinion.
    """
    if percent is None:
        return "unknown"
    if percent >= 100:
        return "over"
    if percent >= 80:
        return "near"
    return "ok"


def _filter_words(node: Any) -> str | None:
    """An ARM budget filter in words.

    Budgets made here are tag filters; budgets made in the portal are usually `dimensions`
    (resource group, service name). Both have to read the same way in the list, otherwise the
    portal's budgets show up as untitled rows that appear to track nothing.
    """
    if not isinstance(node, dict) or not node:
        return None

    if "and" in node:
        parts = [_filter_words(c) for c in node.get("and") or []]
        return " AND ".join(p for p in parts if p) or None

    for kind in ("tags", "dimensions"):
        clause = node.get(kind)
        if isinstance(clause, dict):
            name = clause.get("name") or kind
            values = [str(v) if v != "" else "(no value)" for v in clause.get("values") or []]
            shown = ", ".join(values[:4])
            if len(values) > 4:
                shown += f" and {len(values) - 4} more"
            return f"{name} = {shown}" if shown else str(name)
    return None



async def top_resources(subscription_ids: list[str] | None = None, months_back: int = 1,
                        top: int = 15) -> dict[str, Any]:
    """The individual resources costing the most."""
    result = await cost_summary(subscription_ids, months_back=months_back,
                                group_by="ResourceId", top=top)
    for row in result["breakdown"]:
        rid = row["name"]
        parts = rid.split("/")
        row["resource"] = parts[-1] if len(parts) > 1 else rid
        row["resource_group"] = parts[4] if len(parts) > 4 else None
        row["type"] = "/".join(parts[6:8]) if len(parts) > 7 else None
    return result


async def overview(scope: list[str] | None = None) -> dict[str, Any]:
    """A small at-a-glance summary for the header strip.

    Historical totals come from the local warehouse when it has data — instant, and immune to the
    Cost Management throttling that intermittently blanked these tiles. Forecast and budgets are
    kept per-subscription so that narrowing the scope is a re-aggregation in memory rather than
    another round of slow API calls.
    """
    from .warehouse import warehouse

    mtd = last = None
    currency = None
    stored = warehouse.summary()
    wanted = {s.lower() for s in scope} if scope else None

    if stored["rows"]:
        today = datetime.now(timezone.utc).date()
        this_start = today.replace(day=1)
        prev_start = (this_start - timedelta(days=1)).replace(day=1)
        prev_end = this_start - timedelta(days=1)
        try:
            # Dates are computed here, never user input, so interpolation is safe.
            sql = (
                'SELECT '
                f'  sum(CASE WHEN "ChargePeriodStart" >= DATE \'{this_start}\' '
                '       THEN "BilledCost" ELSE 0 END) AS mtd, '
                f'  sum(CASE WHEN "ChargePeriodStart" BETWEEN DATE \'{prev_start}\' '
                f'       AND DATE \'{prev_end}\' THEN "BilledCost" ELSE 0 END) AS prev '
                'FROM costs'
            )
            rows = warehouse.query(sql, scope=scope)["rows"]
            if rows:
                mtd = round(rows[0]["mtd"] or 0, 2)
                last = round(rows[0]["prev"] or 0, 2)
                currency = stored["currency"]
        except Exception as exc:  # noqa: BLE001 - fall through to the live API
            log.warning("warehouse overview failed: %s", exc)

    tasks: dict[str, Any] = {
        "subs": list_subscriptions(),
        "fc": _forecast_by_subscription(scope),
        "buds": budgets(scope),
    }
    if mtd is None:
        tasks["mtd"] = cost_summary(scope, months_back=0, group_by="None")
        tasks["last"] = cost_summary(scope, months_back=1, group_by="None")

    keys = list(tasks)
    done = await asyncio.gather(*(tasks[k] for k in keys), return_exceptions=True)
    got = dict(zip(keys, done))

    def value(x: Any, *path: str, default: Any = None) -> Any:
        if isinstance(x, Exception):
            return default
        for p in path:
            x = x.get(p) if isinstance(x, dict) else None
            if x is None:
                return default
        return x

    if mtd is None:
        mtd = value(got.get("mtd"), "grand_total")
        last = value(got.get("last"), "grand_total")
        currency = value(got.get("mtd"), "currency") or value(got.get("last"), "currency")

    # The forecast is a separate call from the month-to-date pair, and throttling takes them
    # independently — so the forecast regularly survives while those two come back empty. The
    # currency above is read only from that pair, which leaves it unset in exactly the case
    # where a figure is still on display, and the default below would then put a dollar sign in
    # front of a number Azure billed in rupees. The forecast knows what it was quoted in.
    currency = currency or value(got.get("fc"), "currency")

    worst = None
    buds = got.get("buds")
    if not isinstance(buds, Exception) and buds:
        tracked = [b for b in buds.get("budgets", []) if b.get("percent_used") is not None]
        if tracked:
            worst = max(tracked, key=lambda b: b["percent_used"])

    all_subs = value(got.get("subs"), "subscriptions", default=[]) or []
    in_scope = [s for s in all_subs if not wanted
                or s["id"].lower() in wanted or (s["name"] or "").lower() in wanted]

    return {
        "subscriptions": len(in_scope) or stored["subscriptions"],
        "currency": currency or "USD",
        "month_to_date": mtd,
        "last_month": last,
        "forecast_30d": value(got.get("fc"), "projected_total"),
        "budget": {"name": worst["name"], "percent_used": worst["percent_used"],
                   "status": worst.get("status"),
                   "over": value(got.get("buds"), "over", default=0),
                   "tracked": value(got.get("buds"), "count", default=0),
                   "subscription": worst["subscription"]} if worst else None,
        "source": "warehouse" if stored["rows"] else "live",
        "scope": scope or None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


async def _forecast_by_subscription(scope: list[str] | None) -> dict[str, Any]:
    return await cost_forecast(scope, days_ahead=30)

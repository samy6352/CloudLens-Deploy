"""Showing costs in a currency other than the one Azure billed in.

The app has refused to do this until now, and the refusal was right: `warehouse.py` rejects any
query that sums money across currencies, because adding £100 to €100 produces 200 of nothing.
The fix is not to relax that rule. It is to convert first, using a rate that can be named.

Where the rate comes from, in order of preference:

  1. **Azure's own `CostInUsd`.** Every cost export carries the billed amount *and* the same
     amount in USD, converted by Microsoft at the rate they actually used for that invoice. It
     is not an approximation — it is the number on the bill. When it is present, nothing here
     needs to invent anything.

  2. **A published reference rate, fetched daily.** The European Central Bank via Frankfurter,
     falling back to exchangerate-api for the currencies the ECB does not publish (AED is
     pegged, so it appears in neither ECB list). Cached for a day and pinned to the date it was
     published, because a figure that silently changes between two people looking at it is worse
     than one that is a day old.

  3. **A rate the operator pinned**, via `FX_RATES`. Overrides the feed entirely — for an
     organisation that books at a fixed internal rate, a live rate is the *wrong* answer.

  4. **Nothing.** If none of those work the app shows the original currency and says why, rather
     than guessing. A number converted at an unknown rate looks authoritative and cannot be
     reproduced.

The display currency is a *view*, never storage. The warehouse keeps what Azure billed, so a
converted figure can always be traced back — and a change of rate does not rewrite history.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx

log = logging.getLogger("cloudlens.currency")

# Rates are published once a day, so there is nothing to gain from asking more often — and a
# figure that changes while someone is reading it is a support call.
TTL = 6 * 3600

# The ECB via Frankfurter: no key, no rate limit worth worrying about, and an authority anyone
# can check. It publishes on working days only, so the date it returns is the date to quote.
PRIMARY = "https://api.frankfurter.dev/v1/latest"

# The ECB does not publish pegged currencies — AED among them — so a second source covers the
# gap rather than leaving a currency permanently unavailable.
FALLBACK = "https://open.er-api.com/v6/latest/USD"

_cache: dict[str, Any] = {"at": 0.0, "rates": {}, "date": None, "source": None}
_lock = asyncio.Lock()

# Symbols are for display only. Formatting is done by Intl in the browser, which knows far more
# about placement and grouping than a lookup table does; this exists for server-side text —
# spreadsheet number formats and the PDF/Markdown builders, which have no Intl to call.
KNOWN = {
    "USD": "US dollar",
    "INR": "Indian rupee",
    "EUR": "Euro",
    "GBP": "Pound sterling",
    "AED": "UAE dirham",
    "AUD": "Australian dollar",
    "CAD": "Canadian dollar",
    "JPY": "Japanese yen",
    "SGD": "Singapore dollar",
    "CHF": "Swiss franc",
}

# One symbol table, because there were three: the XLSX, PDF and Markdown builders each carried
# their own four-entry copy, so six of the ten currencies the picker offers rendered with no
# symbol at all. An unknown code returns the code itself — "AED 1,200" reads correctly and is
# never ambiguous, which a bare number is not.
SYMBOLS = {
    "USD": "$", "INR": "₹", "EUR": "€", "GBP": "£", "JPY": "¥",
    "AUD": "A$", "CAD": "C$", "SGD": "S$", "AED": "AED ", "CHF": "CHF ",
}


def symbol(code: str) -> str:
    """The prefix to put in front of an amount. Falls back to the code, never to nothing."""
    code = (code or "").upper()
    if not code:
        return ""
    return SYMBOLS.get(code, f"{code} ")

# What the picker offers. Everything here must be obtainable from one of the two feeds.
OFFERED = ("USD", "INR", "EUR", "GBP", "AED", "AUD", "CAD", "JPY", "SGD", "CHF")


class CurrencyError(RuntimeError):
    """Something the caller can act on."""


def _pinned() -> dict[str, float]:
    """Operator-supplied rates, as units per USD. These win over the live feed.

    `FX_RATES='{"INR": 88.2}'` means one USD buys 88.2 rupees. An organisation that books
    internal transfers at a fixed rate needs its reports to match its ledger, not the market.
    """
    raw = os.getenv("FX_RATES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k).upper(): float(v) for k, v in parsed.items() if float(v) > 0}
    except (ValueError, TypeError, AttributeError) as exc:
        log.warning("FX_RATES could not be read (%s); falling back to the live feed", exc)
        return {}


async def _fetch() -> dict[str, Any]:
    """Today's published rates, from the ECB with a fallback for pegged currencies."""
    rates: dict[str, float] = {}
    date = None
    source = None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(PRIMARY, params={"base": "USD"})
            if r.status_code == 200:
                body = r.json()
                rates = {k.upper(): float(v) for k, v in (body.get("rates") or {}).items()}
                date = body.get("date")
                source = "European Central Bank"
    except Exception as exc:  # noqa: BLE001 - a missing rate is not an outage
        log.info("primary FX source failed: %s", str(exc)[:160])

    # Only ask the second source for what the first could not supply. It is a commercial free
    # tier, so there is no reason to lean on it for rates the ECB already published.
    missing = [c for c in OFFERED if c not in rates and c != "USD"]
    if missing:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(FALLBACK)
                if r.status_code == 200:
                    body = r.json()
                    extra = {k.upper(): float(v) for k, v in (body.get("rates") or {}).items()}
                    for code in missing:
                        if code in extra:
                            rates[code] = extra[code]
                    if not date:
                        date = (body.get("time_last_update_utc") or "")[:16]
                    source = f"{source} and exchangerate-api" if source else "exchangerate-api"
        except Exception as exc:  # noqa: BLE001
            log.info("fallback FX source failed: %s", str(exc)[:160])

    return {"rates": rates, "date": date, "source": source}


async def refresh(force: bool = False) -> dict[str, Any]:
    """The current rate table, fetching at most once every `TTL` seconds.

    Locked, so a burst of requests on a cold cache produces one fetch rather than twenty — and
    every one of them gets the same numbers, which is the point.
    """
    if not force and _cache["rates"] and time.time() - _cache["at"] < TTL:
        return _cache

    async with _lock:
        if not force and _cache["rates"] and time.time() - _cache["at"] < TTL:
            return _cache
        got = await _fetch()
        if got["rates"]:
            _cache.update(at=time.time(), **got)
            log.info("FX rates updated (%s, %d currencies)", got["date"], len(got["rates"]))
        elif _cache["rates"]:
            # Keep yesterday's rather than losing conversion entirely over one failed fetch.
            log.warning("FX refresh failed; keeping rates from %s", _cache["date"])
    return _cache


def rate_to(target: str) -> float | None:
    """How many units of `target` one USD buys, or None if we cannot say.

    Synchronous, and reads only what has already been fetched — it is called from inside SQL
    construction, which is not async, and a network call there would be the wrong shape entirely.
    """
    target = (target or "").upper()
    if target == "USD":
        return 1.0
    pinned = _pinned()
    if target in pinned:
        return pinned[target]
    rate = _cache["rates"].get(target)
    return float(rate) if rate else None


async def available() -> dict[str, Any]:
    """Which currencies this deployment can actually display, and on what authority.

    USD is always offered when the data carries `CostInUsd`, because that needs no rate at all.
    Everything else depends on a live or pinned rate, and the response says which is which so
    the UI never offers a choice that will fail — and so the figure can be quoted with its
    source and date rather than presented as a fact from nowhere.
    """
    await refresh()
    pinned = _pinned()
    live = _cache["rates"]

    supported = ["USD"] + [c for c in OFFERED if c != "USD" and (c in pinned or c in live)]
    sources = []
    if _cache["source"]:
        sources.append(f"{_cache['source']} ({_cache['date']})")
    if pinned:
        sources.append(f"pinned by this deployment: {', '.join(sorted(pinned))}")

    return {
        "base": "USD",
        "supported": supported,
        "pinned": sorted(pinned),
        "live": sorted(c for c in live if c in OFFERED),
        "rates": {c: (pinned.get(c) or live.get(c)) for c in supported if c != "USD"},
        "as_of": _cache["date"],
        "source": " · ".join(sources) if sources else "Azure CostInUsd only",
        "note": (
            "Only USD is available. Azure records the USD equivalent of every charge on the "
            "invoice itself, so it needs no exchange rate — but no live rate source could be "
            "reached for the others."
        ) if len(supported) == 1 else None,
    }


def convert_sql(column: str, target: str) -> str:
    """A SQL expression converting `column` into `target`, or the column unchanged.

    Uses `CostInUsd` as the pivot — Microsoft's own conversion, taken from the invoice — and
    falls back to the billed amount only where that column is empty, which happens on some older
    export schemas. Returning the column untouched for an unavailable target means a caller can
    always use this without branching.
    """
    target = (target or "").upper()
    if not target or target == "BILLED":
        return f'"{column}"'

    usd = f'coalesce(nullif("CostInUsd", 0), "{column}")'
    if target == "USD":
        return usd

    rate = rate_to(target)
    if rate is None:
        return f'"{column}"'
    return f"({usd} * {rate})"


# Tabs whose money never passes through the warehouse — Advisor savings, ESU list prices, the
# cost figures Resource Graph reports against idle resources — cannot be converted in SQL, so they
# are converted on the way out. Doing that safely needs an allowlist rather than a heuristic: a
# response is full of numbers that must *not* move (counts, percentages, day windows, CPU
# readings), and "looks like money" is not a property a number has. Every name here was taken from
# the live responses and checked against the code that emits it.
#
# Anything Azure gives us here is USD, which is the pivot `convert_sql` uses too, so the two paths
# agree on the same rate.
MONEY_KEYS = frozenset({
    # header tiles
    "month_to_date", "last_month", "forecast_30d",
    # stale/idle resources
    "cost", "total_cost", "monthly_cost", "estimated_cost",
    # commitment and Advisor recommendations
    "savings", "annual_savings", "estimated_annual_savings", "usage_saving",
    "best_commitment_saving", "saving", "monthly_saving", "estimated_monthly_savings",
    # extended security updates
    "estimated_monthly_cost", "annual_cost",
    # shutdown schedules
    "total_current", "total_saving", "current_monthly", "saved_monthly",
    # anomaly findings
    "baseline", "delta", "impact",
    # budgets — amount and spend must move together or the percentage stops meaning anything
    "amount", "current_spend", "forecast_spend", "remaining",
    # commitment coverage
    "committed", "compute_total", "on_demand", "saved", "spot", "spot_cost",
    "total_saved", "total_spot_cost", "would_have_cost",
})


def convert_money(payload: Any, target: str) -> Any:
    """Convert the money in an already-built response, leaving every other number alone.

    For tabs that read their figures from Azure rather than the warehouse. It walks the structure
    and multiplies only the keys named in `MONEY_KEYS`, so a count of 12 resources stays 12 and a
    percentage stays a percentage. Returns the payload unchanged when there is nothing to do,
    which lets callers apply it unconditionally.
    """
    target = (target or "").upper()
    if not target or target in ("BILLED", "USD"):
        return payload
    rate = rate_to(target)
    if rate is None:
        return payload

    def walk(node: Any, key: str = "") -> Any:
        if isinstance(node, dict):
            return {k: walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v, key) for v in node]
        # bool is an int in Python, and converting a flag would be silent nonsense.
        if isinstance(node, (int, float)) and not isinstance(node, bool) and key in MONEY_KEYS:
            return round(node * rate, 2)
        return node

    return walk(payload)


def describe(target: str) -> dict[str, Any]:
    """What a converted figure is, in words, for the disclosure line under it."""
    target = (target or "").upper()
    if target in ("", "BILLED"):
        return {"currency": None, "converted": False,
                "basis": "As billed by Azure, in each subscription's own currency."}
    if target == "USD":
        return {"currency": "USD", "converted": True,
                "basis": "Converted using the USD amount Azure records on the invoice itself, "
                         "at the rate Microsoft applied — not an estimate."}
    rate = rate_to(target)
    if rate is None:
        return {"currency": None, "converted": False,
                "basis": f"No rate is available for {target}, so figures stay as billed."}
    pinned = target in _pinned()
    return {
        "currency": target,
        "converted": True,
        "rate": rate,
        "as_of": None if pinned else _cache["date"],
        "basis": (
            f"Converted from Azure's own USD figure at a rate of {rate:g} {target} per USD, "
            + ("pinned by this deployment rather than fetched — so it matches your ledger "
               "rather than today's market."
               if pinned else
               f"published by {_cache['source'] or 'the rate feed'} on {_cache['date']}. "
               "Rates move daily, so this is a presentation of the billed amount, not a "
               "restatement of it.")
        ),
    }

"""Creating Azure budgets from a tag selection.

This is the one place in the app that *writes* to Azure. `cost.py` states in its first
paragraph that nothing in it can change a resource, and that guarantee is worth more than the
convenience of adding a PUT to it, so budget creation lives here instead.

A budget created from the Cost by Application / Tags tab has to survive the gap between what
that tab can express and what Azure's budget API can. The tab answers "what do these tags cost"
over a set of resources; a budget is a standing rule attached to one subscription. Three of
those differences are load-bearing, and each is refused rather than approximated:

  * **Only AND exists.** A Consumption budget filter is a conjunction. The tab's "Has any" mode
    across several keys has no equivalent, and quietly creating an AND budget for someone who
    asked for OR would produce a budget that tracks a *smaller* set than the number they were
    looking at — the failure would surface months later as an alert that never fired.

  * **Tag filters need explicit values.** There is no "any value of this key" operator, so a key
    selected without narrowing is expanded to the values actually seen in the window. That is
    the honest translation, but it is a snapshot: a value created tomorrow is not in the budget.
    The caller is told, rather than finding out from a missed alert.

  * **A budget belongs to one subscription.** A tag selection spanning three subscriptions is
    three budgets, not one. We create the one asked for and report what share of the selection
    it actually covers, so nobody reads a $200 budget as covering a $600 selection.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("cloudlens.budgets")

BUDGET_API = "2024-08-01"

# Azure's own rule for a budget name. Checked here so a bad name fails with a sentence instead
# of an ARM error that names a regex.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

TIME_GRAINS = ("Monthly", "Quarterly", "Annually")

# How many months a reset period covers, for scaling an observed daily rate into a suggestion.
GRAIN_MONTHS = {"Monthly": 1, "Quarterly": 3, "Annually": 12}

# A budget filter is a conjunction of at most this many tag clauses. Azure's documented ceiling
# is 10 entries in `and`; staying under it keeps the failure here, where it can be explained.
MAX_CLAUSES = 10

# Values per tag key. Azure caps the `values` array, and a key with hundreds of values is
# almost always the wrong thing to budget on anyway.
MAX_VALUES = 50

# "Budget can have up to five notifications" — the documented ceiling. Enforced here so six
# thresholds fail with a sentence rather than an ARM error naming a dictionary.
MAX_NOTIFICATIONS = 5


class BudgetError(ValueError):
    """Something the caller can fix, phrased for them rather than for a log."""


def _clean_values(values: Any) -> list[str]:
    """Tag values, de-duplicated, order preserved.

    Empty string is a legitimate tag value — a resource tagged `project` with no value — and is
    kept. `None` is not a value and is dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        if v is None:
            continue
        s = str(v)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_filter(tags: list[dict[str, Any]], mode: str = "all") -> dict[str, Any]:
    """The `properties.filter` for a budget covering these tag selections.

    `tags` is `[{"key": "project", "values": ["hdfc"]}, ...]`, which is the tab's selection with
    each key's value narrowing already resolved to a concrete list.

    Shape matters: one clause goes in bare, several go inside `and`. Azure rejects an `and` with
    a single entry, so the two cases are genuinely different rather than a special case of each
    other.
    """
    if mode not in ("all", "any"):
        raise BudgetError(f"Unknown combine mode {mode!r}.")

    clauses: list[dict[str, Any]] = []
    for entry in tags:
        key = str(entry.get("key") or "").strip()
        if not key:
            raise BudgetError("A tag filter needs a key.")
        values = _clean_values(entry.get("values"))
        if not values:
            raise BudgetError(
                f"No values to filter on for the tag “{key}”. Azure budgets match tag values "
                "explicitly — there is no 'any value' option — so a key with nothing behind it "
                "in this period cannot become a budget filter."
            )
        if len(values) > MAX_VALUES:
            raise BudgetError(
                f"The tag “{key}” has {len(values)} values in this period, more than the "
                f"{MAX_VALUES} a budget filter can carry. Narrow it to the values you want to "
                "track before creating the budget."
            )
        clauses.append({"tags": {"name": key, "operator": "In", "values": values}})

    if not clauses:
        raise BudgetError("Select at least one tag before creating a budget.")

    if len(clauses) > MAX_CLAUSES:
        raise BudgetError(
            f"A budget filter can combine at most {MAX_CLAUSES} tags; {len(clauses)} were "
            "selected."
        )

    # The refusal that matters. An Azure budget filter is a conjunction — there is no `or` —
    # so "has any of these tags" cannot be expressed at all once more than one key is involved.
    # Creating the AND version would track a strictly smaller set than the figure on screen.
    if mode == "any" and len(clauses) > 1:
        raise BudgetError(
            "Azure budgets can only combine tags with AND, so “Has any” cannot be turned into "
            "a budget when more than one tag is selected. Switch to “Has all”, or select a "
            "single tag and create one budget per tag."
        )

    if len(clauses) == 1:
        return clauses[0]
    return {"and": clauses}


def period_start(today: date | None = None) -> date:
    """First day of the current month, which is where a budget's first period begins."""
    today = today or datetime.now(timezone.utc).date()
    return today.replace(day=1)


def default_end(start: date, years: int = 2) -> date:
    """A default expiry far enough out to be useful, close enough to be reviewed.

    Azure requires an end date after the start; leaving it open is not an option, so the choice
    is between a date someone picked and a date we picked. Two years matches the portal.
    """
    try:
        return start.replace(year=start.year + years)
    except ValueError:  # 29 February
        return start.replace(year=start.year + years, day=28)


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        raise BudgetError(f"{label} is required.")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise BudgetError(f"{label} must be a date like 2026-08-01.") from None


def suggested_amount(daily_cost: float, time_grain: str = "Monthly") -> float | None:
    """A starting figure from the spend actually observed, scaled to the reset period.

    Deliberately a suggestion and not a default that gets accepted without thought: an average
    is exactly the wrong number for a budget, because half the periods exceed it. Rounded up to
    something a person would have typed, which also builds in the headroom.
    """
    if daily_cost <= 0:
        return None
    months = GRAIN_MONTHS.get(time_grain, 1)
    projected = daily_cost * 30.4 * months

    # Round up to 2 significant-ish figures: 163.2 -> 170, 1632 -> 1700, 8.4 -> 9.
    step = 1.0
    while projected / step >= 100:
        step *= 10
    return float(int(projected / step + 0.999) * step)


def build_body(*, amount: float, time_grain: str, start: date, end: date,
               tags: list[dict[str, Any]], mode: str = "all",
               thresholds: list[float] | None = None,
               emails: list[str] | None = None) -> dict[str, Any]:
    """The full request body for a PUT, validated.

    Everything Azure would reject is rejected here first, with a sentence explaining it. An ARM
    validation error arrives as a code and a property path, which is fine for a client library
    and useless in a toast.
    """
    if time_grain not in TIME_GRAINS:
        raise BudgetError(f"Reset period must be one of {', '.join(TIME_GRAINS)}.")
    if amount is None or amount <= 0:
        raise BudgetError("The budget amount must be greater than zero.")
    if end <= start:
        raise BudgetError("The expiry date must be after the start date.")

    # Monthly budgets reset on the 1st; Azure requires the start to line up with that. Silently
    # snapping the date would move a budget someone deliberately dated.
    if start.day != 1:
        raise BudgetError("A budget starts on the first day of a month.")

    thresholds = sorted({round(float(t), 2) for t in (thresholds or [])})
    for t in thresholds:
        if not 0 < t <= 1000:
            raise BudgetError("Alert thresholds are percentages between 0 and 1000.")
    if len(thresholds) > MAX_NOTIFICATIONS:
        raise BudgetError(
            f"A budget carries at most {MAX_NOTIFICATIONS} alerts; {len(thresholds)} thresholds "
            "were given."
        )

    emails = [e.strip() for e in (emails or []) if e and e.strip()]
    if thresholds and not emails:
        raise BudgetError(
            "An alert needs at least one email address to notify, otherwise the threshold is "
            "recorded and nobody hears about it."
        )

    notifications: dict[str, Any] = {}
    for t in thresholds:
        # The key is ours to choose but must be stable and unique per threshold, so re-running
        # the same request updates the same notification instead of stacking duplicates.
        label = f"Actual_GreaterThan_{str(t).replace('.', '_')}_Percent"
        notifications[label] = {
            "enabled": True,
            "operator": "GreaterThan",
            "threshold": t,
            "thresholdType": "Actual",
            "contactEmails": emails,
        }

    body: dict[str, Any] = {
        "properties": {
            "category": "Cost",
            "amount": round(float(amount), 2),
            "timeGrain": time_grain,
            "timePeriod": {
                "startDate": f"{start.isoformat()}T00:00:00Z",
                "endDate": f"{end.isoformat()}T00:00:00Z",
            },
            "filter": build_filter(tags, mode),
        }
    }
    if notifications:
        body["properties"]["notifications"] = notifications
    return body


def describe(tags: list[dict[str, Any]], mode: str = "all") -> str:
    """The filter in words, for a confirmation line and for the audit log."""
    parts = []
    for entry in tags:
        key = entry.get("key")
        values = _clean_values(entry.get("values"))
        shown = ", ".join(v if v != "" else "(no value)" for v in values[:4])
        if len(values) > 4:
            shown += f" and {len(values) - 4} more"
        parts.append(f"{key} = {shown}")
    joiner = " AND " if mode == "all" else " OR "
    return joiner.join(parts)


async def create(*, subscription_id: str, name: str, amount: float,
                 time_grain: str = "Monthly", start: Any = None, end: Any = None,
                 tags: list[dict[str, Any]], mode: str = "all",
                 thresholds: list[float] | None = None,
                 emails: list[str] | None = None) -> dict[str, Any]:
    """Create (or update) one tag-filtered budget on one subscription.

    A PUT, so re-submitting the same name is an update rather than a duplicate — which is what
    someone correcting an amount expects, and what a retry after a timeout must not turn into
    two budgets.
    """
    from . import cost

    name = str(name or "").strip()
    if not NAME_RE.match(name):
        raise BudgetError(
            "A budget name starts with a letter or digit and can contain letters, digits, "
            "hyphens, underscores and dots (up to 63 characters)."
        )
    subscription_id = str(subscription_id or "").strip()
    if not subscription_id:
        raise BudgetError("Choose the subscription the budget belongs to.")

    first = period_start()
    start_d = _parse_date(start, "Start date") if start else first
    end_d = _parse_date(end, "Expiry date") if end else default_end(start_d)

    body = build_body(amount=amount, time_grain=time_grain, start=start_d, end=end_d,
                      tags=tags, mode=mode, thresholds=thresholds, emails=emails)

    url = (f"{cost.ARM}/subscriptions/{subscription_id}"
           f"/providers/Microsoft.Consumption/budgets/{name}")

    log.info("creating budget %s on %s for %s", name, subscription_id, describe(tags, mode))
    try:
        # No caching: a write is not a question, and a cached 200 would make the second attempt
        # of a failed create look like it worked.
        payload = await cost.azure.request("PUT", url, json_body=body,
                                           params={"api-version": BUDGET_API}, cache=False)
    except cost.CostError as exc:
        raise BudgetError(
            _explain(str(exc), subscription_id, cost.acting_as_caller())
        ) from exc

    # The budget list for this subscription is now wrong in every cache that holds it — the
    # overview tile as much as the read-back the tab shows next. Dropped rather than left to
    # expire, because a five-minute-old list showing the previous amount is indistinguishable
    # from a write that did not take.
    cost.forget(f"/subscriptions/{subscription_id}/providers/Microsoft.Consumption/budgets")

    props = payload.get("properties", {}) if isinstance(payload, dict) else {}
    spend = props.get("currentSpend") or {}
    sent = body["properties"].get("notifications", {})

    # Everything the caller needs to *show* what was created, so the confirmation is the budget
    # itself rather than a sentence claiming one exists. Echoing the request where Azure's reply
    # is silent — a fresh budget has no currentSpend — keeps the panel populated either way.
    return {
        "name": payload.get("name", name),
        "id": payload.get("id"),
        "subscription": subscription_id,
        "amount": props.get("amount", body["properties"]["amount"]),
        "time_grain": props.get("timeGrain", time_grain),
        "start": start_d.isoformat(),
        "end": end_d.isoformat(),
        "filter": describe(tags, mode),
        "notifications": len(sent),
        "thresholds": [n["threshold"] for n in sent.values()],
        "emails": next(iter(sent.values()))["contactEmails"] if sent else [],
        "current_spend": spend.get("amount"),
        "currency": spend.get("unit"),
    }


def _explain(detail: str, subscription_id: str, as_caller: bool = True) -> str:
    """Turn the two failures that actually happen into something actionable.

    Everything else is passed through: inventing a friendly message for an error we have not
    seen risks describing the wrong problem, which is worse than the raw text.

    `as_caller` decides *whose* permissions the message talks about. Getting that wrong is not
    a wording nicety: told "you need Cost Management Contributor", a subscription Owner will
    check their own roles, find Owner, and conclude the app is broken — while the identity
    Azure actually refused sits there unmentioned and ungranted.
    """
    low = detail.lower()
    if "cost management reader" in low or "403" in low or "authorizationfailed" in low:
        if not as_caller:
            return (
                "Azure refused this budget. Delegated Azure access is off, so the app created "
                "it under its own managed identity rather than under your account — your own "
                "roles on the subscription do not apply. The app's managed identity needs Cost "
                f"Management Contributor (or Contributor/Owner) on {subscription_id}."
            )
        return (
            "You can read costs on this subscription but not create a budget there. Creating "
            "one needs Cost Management Contributor (or Contributor/Owner) on "
            f"{subscription_id} — Cost Management Reader is not enough."
        )
    if "already exists" in low:
        return ("A budget with that name already exists on this subscription. Use a different "
                "name, or submit the same name again to update it.")
    return detail

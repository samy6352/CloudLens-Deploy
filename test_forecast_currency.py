"""The currency a forecast is quoted in, and what happens when only the forecast answers.

The header strip reads its currency from the month-to-date pair, but the forecast is a separate
call and Cost Management throttles them independently. So the forecast routinely survives while
those two come back empty — and a currency read only from them is then unset, at exactly the
moment a figure is still on display. Defaulting that to USD put a dollar sign in front of a
number Azure had billed in rupees: observed live as forecast 1019.69 labelled USD, where every
later call on the same subscription said INR. An 88x overstatement, on first load, which is when
somebody forms their first impression of what their estate costs.

    python test_forecast_currency.py
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import cost

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}  {detail}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def payload(amount: float, currency: str | None) -> dict:
    """An ARM forecast response in the shape _rows() reads."""
    cols = [{"name": "Cost"}, {"name": "Currency"}]
    return {"properties": {"columns": cols, "rows": [[amount, currency]]}}


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- forecast
print("=" * 72)
print("a forecast says what it was quoted in")

SUBS = [{"id": "sub-a", "name": "A"}, {"id": "sub-b", "name": "B"}]


def with_forecast(per_sub: dict[str, tuple[float, str | None]], subs=None):
    """Answer each subscription's forecast call with its own amount and currency."""
    async def fake_resolve(ids):
        return subs if subs is not None else SUBS

    async def fake_request(method, url, **kw):
        for sid, (amount, cur) in per_sub.items():
            if sid in url:
                return payload(amount, cur)
        raise AssertionError(f"unexpected url {url}")

    cost._resolve = fake_resolve
    cost.azure.request = fake_request


_real_resolve, _real_request = cost._resolve, cost.azure.request
try:
    with_forecast({"sub-a": (100.0, "INR"), "sub-b": (25.0, "INR")})
    r = run(cost.cost_forecast(days_ahead=30))
    check("the currency Azure quoted is carried out of the call",
          r.get("currency") == "INR", str(r.get("currency")))
    check("and the total is still the total", r["projected_total"] == 125.0,
          str(r["projected_total"]))

    with_forecast({"sub-a": (100.0, "INR"), "sub-b": (25.0, "USD")})
    r = run(cost.cost_forecast(days_ahead=30))
    check("a mixed estate names no currency at all", r.get("currency") is None,
          "one symbol in front of a sum of two currencies would be a confident wrong answer")

    with_forecast({"sub-a": (100.0, None), "sub-b": (25.0, None)})
    r = run(cost.cost_forecast(days_ahead=30))
    check("silence from Azure stays silence, not a guess", r.get("currency") is None)

    with_forecast({"sub-a": (100.0, "INR")}, subs=[{"id": "sub-a", "name": "A"}])
    r = run(cost.cost_forecast(days_ahead=30))
    check("a single subscription is enough to know", r.get("currency") == "INR")
finally:
    cost._resolve, cost.azure.request = _real_resolve, _real_request


# --------------------------------------------------------------------------- overview
print()
print("=" * 72)
print("the header strip labels the figure it is actually showing")


class FakeWarehouse:
    """An empty warehouse, which is the first-run state and the one that matters here."""

    def summary(self, scope=None):
        return {"rows": 0, "currency": None, "subscriptions": 0}

    def query(self, sql, scope=None):
        return {"rows": []}


def with_overview(mtd_result, forecast_currency, forecast_total=1019.69):
    """Stub the three calls overview() fans out to. `mtd_result` may be an Exception."""
    import app.warehouse as wh
    wh.warehouse = FakeWarehouse()

    async def fake_subs():
        return {"subscriptions": [{"id": "sub-a", "name": "A"}], "count": 1}

    async def fake_forecast(scope):
        return {"projected_total": forecast_total, "currency": forecast_currency}

    async def fake_budgets(scope):
        return {"budgets": [], "count": 0, "over": 0}

    async def fake_summary(scope, months_back=0, group_by="None"):
        if isinstance(mtd_result, Exception):
            raise mtd_result
        return mtd_result

    cost.list_subscriptions = fake_subs
    cost._forecast_by_subscription = fake_forecast
    cost.budgets = fake_budgets
    cost.cost_summary = fake_summary


_saved = (cost.list_subscriptions, cost._forecast_by_subscription,
          cost.budgets, cost.cost_summary)
import app.warehouse as _wh  # noqa: E402
_saved_wh = _wh.warehouse
try:
    # The observed failure: throttling takes the month-to-date pair, the forecast survives.
    with_overview(RuntimeError("429 throttled"), "INR")
    r = run(cost.overview())
    check("a surviving forecast lends its currency when nothing else knows",
          r["currency"] == "INR",
          f"was USD against a rupee figure of {r['forecast_30d']}")
    check("and the figure itself is still reported", r["forecast_30d"] == 1019.69)
    check("with the tiles it could not fill left empty rather than zeroed",
          r["month_to_date"] is None and r["last_month"] is None)

    # The ordinary path must not change: month-to-date is the better source when it answers.
    with_overview({"grand_total": 50.0, "currency": "EUR"}, "INR")
    r = run(cost.overview())
    check("month-to-date still wins when it answers", r["currency"] == "EUR",
          "the forecast is a fallback, not an override")

    # Nothing knows: the old default is still the right answer, because there is nothing to
    # mislabel — every figure is empty.
    with_overview(RuntimeError("429 throttled"), None, forecast_total=None)
    r = run(cost.overview())
    check("with no figure and no currency the default is harmless",
          r["currency"] == "USD" and r["forecast_30d"] is None)

    # A mixed estate: the forecast declines to name one, so the strip must not invent one.
    with_overview(RuntimeError("429 throttled"), None)
    r = run(cost.overview())
    check("a mixed estate does not borrow a currency from the default",
          r["currency"] == "USD" and r["forecast_30d"] == 1019.69,
          "unavoidable without a per-currency strip, but it is no longer the common case")
finally:
    (cost.list_subscriptions, cost._forecast_by_subscription,
     cost.budgets, cost.cost_summary) = _saved
    _wh.warehouse = _saved_wh


print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

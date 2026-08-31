"""Does 'create a budget from these tags' hold together?

The interesting failures here are not crashes. They are the three places where the tab can
express something an Azure budget cannot, and where producing *a* budget rather than an error
would give someone a rule that tracks a different set of resources than the number they were
looking at when they created it. Those are asserted first.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(r"C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\azure-cost-agent")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    from app import budgets as b

    print("=" * 72)
    print("the filter Azure actually receives")

    one = b.build_filter([{"key": "project", "values": ["hdfc"]}])
    check("a single tag goes in bare, not wrapped in `and`",
          one == {"tags": {"name": "project", "operator": "In", "values": ["hdfc"]}}, json.dumps(one))

    two = b.build_filter([{"key": "project", "values": ["hdfc"]},
                          {"key": "env", "values": ["prod", "dev"]}])
    check("two tags are combined with `and`", "and" in two and len(two["and"]) == 2)
    check("each clause keeps its own values",
          two["and"][1]["tags"]["values"] == ["prod", "dev"])

    dupes = b.build_filter([{"key": "a", "values": ["x", "x", "y"]}])
    check("duplicate values are collapsed", dupes["tags"]["values"] == ["x", "y"])

    blank = b.build_filter([{"key": "a", "values": ["", "y"]}])
    check("an empty tag value is a value, not a gap", blank["tags"]["values"] == ["", "y"])

    print("\n" + "=" * 72)
    print("what cannot be expressed is refused, not approximated")

    # The one that matters most: OR across keys has no budget equivalent, and the AND version
    # tracks a strictly smaller set than the figure on screen.
    try:
        b.build_filter([{"key": "a", "values": ["1"]}, {"key": "b", "values": ["2"]}], mode="any")
        check("'has any' across two tags is refused", False, "it built a filter anyway")
    except b.BudgetError as exc:
        check("'has any' across two tags is refused", "AND" in str(exc), str(exc)[:80])

    ok_any = b.build_filter([{"key": "a", "values": ["1"]}], mode="any")
    check("'has any' with one tag is fine — there is nothing to combine",
          ok_any["tags"]["name"] == "a")

    for label, tags, fragment in [
        ("a key with no values", [{"key": "a", "values": []}], "explicitly"),
        ("no tags at all", [], "at least one"),
        ("too many values", [{"key": "a", "values": [str(i) for i in range(60)]}], "Narrow"),
        ("too many tags", [{"key": f"k{i}", "values": ["v"]} for i in range(11)], "at most"),
    ]:
        try:
            b.build_filter(tags)
            check(f"{label} is refused", False, "it built a filter anyway")
        except b.BudgetError as exc:
            check(f"{label} is refused", fragment.lower() in str(exc).lower(), str(exc)[:70])

    print("\n" + "=" * 72)
    print("the request body")

    body = b.build_body(amount=200, time_grain="Monthly", start=date(2026, 8, 1),
                        end=date(2028, 7, 31), tags=[{"key": "project", "values": ["hdfc"]}],
                        thresholds=[80, 100], emails=["a@b.com"])
    p = body["properties"]
    check("category is Cost", p["category"] == "Cost")
    check("amount and grain carry through", p["amount"] == 200.0 and p["timeGrain"] == "Monthly")
    check("dates are sent as Azure wants them",
          p["timePeriod"]["startDate"] == "2026-08-01T00:00:00Z", p["timePeriod"]["startDate"])
    check("one notification per threshold", len(p["notifications"]) == 2)
    check("each notification carries the contacts",
          all(n["contactEmails"] == ["a@b.com"] for n in p["notifications"].values()))
    check("notification keys are stable, so a re-PUT updates rather than stacks",
          set(p["notifications"]) == {"Actual_GreaterThan_80_0_Percent",
                                      "Actual_GreaterThan_100_0_Percent"})

    kw = dict(tags=[{"key": "a", "values": ["1"]}], time_grain="Monthly",
              start=date(2026, 8, 1), end=date(2028, 1, 1))
    for label, over, fragment in [
        ("a zero amount", {"amount": 0}, "greater than zero"),
        ("a negative amount", {"amount": -5}, "greater than zero"),
        ("an unknown reset period", {"amount": 10, "time_grain": "Hourly"}, "Reset period"),
        ("an end before the start", {"amount": 10, "end": date(2026, 1, 1)}, "after the start"),
        ("a start mid-month", {"amount": 10, "start": date(2026, 8, 15)}, "first day"),
        ("an alert with nobody to tell", {"amount": 10, "thresholds": [80], "emails": []},
         "email"),
        ("more alerts than Azure allows",
         {"amount": 10, "thresholds": [10, 20, 30, 40, 50, 60], "emails": ["a@b.com"]},
         "at most 5"),
    ]:
        try:
            b.build_body(**{**kw, **over})
            check(f"{label} is refused", False, "it built a body anyway")
        except b.BudgetError as exc:
            check(f"{label} is refused", fragment.lower() in str(exc).lower(), str(exc)[:70])

    quiet = b.build_body(amount=10, time_grain="Monthly", start=date(2026, 8, 1),
                         end=date(2027, 8, 1), tags=[{"key": "a", "values": ["1"]}],
                         thresholds=[], emails=[])
    check("a budget with no alerts omits notifications entirely",
          "notifications" not in quiet["properties"])

    print("\n" + "=" * 72)
    print("the suggested amount")

    check("a daily rate becomes a rounded-up monthly figure",
          b.suggested_amount(5.37) == 170.0, str(b.suggested_amount(5.37)))
    check("the reset period scales it",
          b.suggested_amount(5.37, "Quarterly") == 490.0, str(b.suggested_amount(5.37, "Quarterly")))
    check("no spend suggests nothing rather than zero", b.suggested_amount(0) is None)
    check("the suggestion is never below the observed run rate",
          b.suggested_amount(5.37) >= 5.37 * 30.4)

    print("\n" + "=" * 72)
    print("names")

    for bad in ["", "-leading", "has space", "has/slash", "x" * 64]:
        ok = bool(b.NAME_RE.match(bad))
        check(f"rejects {bad[:18]!r}", not ok)
    for good in ["hdfc-budget", "Project_1.budget", "a"]:
        check(f"accepts {good!r}", bool(b.NAME_RE.match(good)))

    print("\n" + "=" * 72)
    print("dates")

    start = b.period_start(date(2026, 8, 27))
    check("a budget starts on the first of the current month", start == date(2026, 8, 1))
    check("the default expiry is two years out", b.default_end(start) == date(2028, 8, 1))
    check("29 February does not crash the default expiry",
          b.default_end(date(2028, 2, 29)) == date(2030, 2, 28))

    print("\n" + "=" * 72)
    print("the write path talks to the right URL, with the right method")

    from app import cost

    seen: dict = {}

    async def fake_request(method, url, *, json_body=None, params=None, cache=True, **kw):
        seen.update(method=method, url=url, body=json_body, params=params, cache=cache)
        return {"name": "hdfc-budget", "id": url, "properties": json_body["properties"]}

    real = cost.azure.request
    cost.azure.request = fake_request
    try:
        made = await b.create(subscription_id="12345678-1234-1234-1234-123456789abc",
                              name="hdfc-budget", amount=200,
                              tags=[{"key": "project", "values": ["hdfc"]}],
                              thresholds=[80], emails=["a@b.com"])
        check("it PUTs, so a retry updates instead of duplicating", seen["method"] == "PUT",
              seen["method"])
        check("the URL is the Consumption budget for that subscription",
              seen["url"].endswith("/subscriptions/12345678-1234-1234-1234-123456789abc"
                                   "/providers/Microsoft.Consumption/budgets/hdfc-budget"),
              seen["url"])
        check("the api-version is pinned", seen["params"] == {"api-version": b.BUDGET_API})
        check("a write is never served from cache", seen["cache"] is False)
        check("the result describes the filter in words", made["filter"] == "project = hdfc",
              made["filter"])
        check("the result reports how many alerts were set", made["notifications"] == 1)
        check("the result carries the thresholds and contacts, so the panel can show them",
              made["thresholds"] == [80.0] and made["emails"] == ["a@b.com"], made["thresholds"])

        # The bug this guards: the tab reads the budget list back straight after writing it.
        # A list cached before the write shows the previous amount, which reads as the write
        # having silently failed.
        sub = "12345678-1234-1234-1234-123456789abc"
        listing = (f"https://management.azure.com/subscriptions/{sub}"
                   "/providers/Microsoft.Consumption/budgets")
        cost._cache["stale"] = (time.time() + 9999, {"value": []}, listing)
        cost._cache["unrelated"] = (time.time() + 9999, {"value": []},
                                    "https://management.azure.com/subscriptions/other/x")
        cost.azure.request = fake_request
        await b.create(subscription_id=sub, name="hdfc-budget", amount=200,
                       tags=[{"key": "project", "values": ["hdfc"]}])
        check("writing a budget drops the cached list for that subscription",
              "stale" not in cost._cache)
        check("it drops only that subscription's entries", "unrelated" in cost._cache)
        del cost._cache["unrelated"]

        # A refusal from Azure has to arrive as something the person can act on.
        async def refuse(*a, **k):
            raise cost.CostError("Azure returned 403: AuthorizationFailed")

        cost.azure.request = refuse
        try:
            await b.create(subscription_id="s", name="n", amount=1,
                           tags=[{"key": "a", "values": ["1"]}])
            check("a 403 is explained, not echoed", False, "no error raised")
        except b.BudgetError as exc:
            check("a 403 names the role that would fix it",
                  "Cost Management Contributor" in str(exc), str(exc)[:90])
    finally:
        cost.azure.request = real

    print("\n" + "=" * 72)
    print("reading budgets back: status, headroom and what each one watches")

    listing = {
        "value": [
            {"name": "blown", "properties": {
                "amount": 100.0, "timeGrain": "Monthly",
                "currentSpend": {"amount": 160.0, "unit": "USD"},
                "timePeriod": {"startDate": "2026-08-01T00:00:00Z",
                               "endDate": "2028-08-01T00:00:00Z"},
                "filter": {"tags": {"name": "project", "operator": "In", "values": ["hdfc"]}},
                "notifications": {"a": {"threshold": 100.0}, "b": {"threshold": 80.0}}}},
            {"name": "close", "properties": {
                "amount": 100.0, "timeGrain": "Monthly",
                "currentSpend": {"amount": 85.0, "unit": "USD"}}},
            {"name": "fine", "properties": {
                "amount": 100.0, "timeGrain": "Monthly",
                "currentSpend": {"amount": 10.0, "unit": "USD"}}},
            {"name": "fresh", "properties": {
                "amount": 50.0, "timeGrain": "Monthly",
                "currentSpend": {"amount": 0.0, "unit": "USD"},
                "filter": {"and": [
                    {"dimensions": {"name": "ResourceGroupName", "operator": "In",
                                    "values": ["rg-one"]}},
                    {"tags": {"name": "env", "operator": "In", "values": ["prod"]}}]}}},
        ]
    }

    async def fake_list(method, url, **kw):
        return listing

    cost.azure.request = fake_list
    real_resolve = cost._resolve

    async def one_sub(ids=None):
        return [{"id": "sub-1", "name": "Sub One"}]

    cost._resolve = one_sub
    try:
        seen = await cost.budgets(["sub-1"])
    finally:
        cost.azure.request = real
        cost._resolve = real_resolve

    by_name = {x["name"]: x for x in seen["budgets"]}
    check("over 100% is reported as over", by_name["blown"]["status"] == "over",
          by_name["blown"]["status"])
    check("80% or more is near the limit", by_name["close"]["status"] == "near")
    check("comfortably under is ok", by_name["fine"]["status"] == "ok")

    # The bug: a truthiness check treated zero spend as "no figure", so every new budget read
    # as unknown — the same as one Azure genuinely cannot price.
    check("zero spend is 0%, not unknown",
          by_name["fresh"]["percent_used"] == 0.0 and by_name["fresh"]["status"] == "ok",
          str(by_name["fresh"]["percent_used"]))

    check("headroom is reported", by_name["fine"]["remaining"] == 90.0)
    check("being over is negative headroom, not zero", by_name["blown"]["remaining"] == -60.0)
    check("the worst budget sorts first", seen["budgets"][0]["name"] == "blown")
    check("the count of over-budget budgets is reported", seen["over"] == 1 and seen["near"] == 1)
    check("a tag filter is described in words",
          by_name["blown"]["filter"] == "project = hdfc", by_name["blown"]["filter"])
    check("a portal budget's dimension filter is described too",
          by_name["fresh"]["filter"] == "ResourceGroupName = rg-one AND env = prod",
          by_name["fresh"]["filter"])
    check("a budget with no filter says nothing rather than inventing one",
          by_name["close"]["filter"] is None)
    check("alert thresholds come back for display",
          by_name["blown"]["alerting"] == [80.0, 100.0], str(by_name["blown"]["alerting"]))
    check("the subscription id is carried, so the row can link to the portal",
          by_name["fine"]["subscription_id"] == "sub-1")

    print("\n" + "=" * 72)
    print("the endpoints are wired")

    import inspect

    from app import main as appmain

    src = inspect.getsource(appmain.create_budget)
    check("the subscription is checked against what they may see", "permitted(user)" in src)
    check("the write runs as the signed-in person", "as_user(user)" in src)
    check("app-identity writes are admin-only", "user.admin" in src and "AUTH_DELEGATED_ARM" in src)
    check("a BudgetError becomes a 400, not a 500", "BudgetError" in src and "400" in src)

    routes = {getattr(r, "path", "") for r in appmain.app.routes}
    check("GET/POST /api/budgets exist", "/api/budgets" in routes)
    check("it does not collide with the dashboard catch-all",
          "/api/dashboard/{section_id}" in routes)

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

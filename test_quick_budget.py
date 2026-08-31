"""A quick refresh is a promise about time, not completeness.

The point of this path is a live answer now. It was taking minutes because a 429 could buy
161 seconds of sleeping per subscription — which made the fast option the slowest one in the
menu. These check the budget is real and that running out of it is reported as a decision
rather than as a failure.
"""
import asyncio
import sys
import time
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from app.warehouse import (COLUMNS, NUMERIC, QUERY_BACKOFF, QUICK_DEADLINE,
                           Warehouse, _query_wait)

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


print("the budget")

check("a throttled subscription cannot sleep for minutes",
      sum(QUERY_BACKOFF) <= 10, f"sums to {sum(QUERY_BACKOFF)}s")
check("the first retry is nearly immediate",
      QUERY_BACKOFF[0] <= 3, f"first wait {QUERY_BACKOFF[0]}s")
check("and the whole run is capped", 30 <= QUICK_DEADLINE <= 120, f"{QUICK_DEADLINE}s")

# The service can ask for longer than the budget; honouring that would blow it.
class Resp:
    def __init__(self, retry=None):
        self.headers = {"Retry-After": str(retry)} if retry else {}
        self.status_code = 429
        self.text = "429"


check("a service-supplied Retry-After is still capped",
      _query_wait(Resp(600), 0) <= 60, f"got {_query_wait(Resp(600), 0)}")
check("and without one it uses the short ladder",
      _query_wait(Resp(), 0) == QUERY_BACKOFF[0])
check("the ladder does not run off its end",
      _query_wait(Resp(), 99) == QUERY_BACKOFF[-1])

print("\nrunning out of time")

tmp = Path(tempfile.mkdtemp()) / "costs.duckdb"
wh = Warehouse(db_path=tmp)
idx = {c: i for i, c in enumerate(COLUMNS)}


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = {}
        self.text = "throttled"

    def json(self):
        return self._payload


class AlwaysThrottled:
    """Every call refused, as a subscription whose quota is spent behaves."""

    def __init__(self):
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return FakeResponse(429)


async def timing_run():
    client = AlwaysThrottled()
    sub = {"id": "sub-a", "name": "Sub A"}
    began = time.monotonic()
    # A deadline already in the past: the very first check must stop it.
    try:
        await wh._query_period(client, sub, "2026-08-01", "2026-08-31", {}, "ActualCost",
                               deadline=time.monotonic() - 1)
        return "no raise", 0.0, client.calls
    except TimeoutError:
        return "TimeoutError", time.monotonic() - began, client.calls


kind, took, calls = asyncio.run(timing_run())
check("an expired deadline stops before any request is made",
      kind == "TimeoutError" and calls == 0, f"{kind}, {calls} calls")
check("and returns immediately", took < 1.0, f"took {took:.2f}s")


async def budget_run():
    """A live-but-throttled subscription must give up inside its budget, not after it."""
    client = AlwaysThrottled()
    sub = {"id": "sub-b", "name": "Sub B"}
    began = time.monotonic()
    try:
        await wh._query_period(client, sub, "2026-08-01", "2026-08-31", {}, "ActualCost",
                               deadline=time.monotonic() + 30)
    except TimeoutError:
        return "timeout", time.monotonic() - began, client.calls
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, time.monotonic() - began, client.calls
    return "none", time.monotonic() - began, client.calls


kind, took, calls = asyncio.run(budget_run())
check("a throttled subscription gives up rather than grinding",
      kind in ("timeout", "RuntimeError"), kind)
check("within the short ladder, not minutes",
      took <= sum(QUERY_BACKOFF) + 3, f"took {took:.1f}s")
check("after retrying, not on the first refusal",
      calls >= 2, f"{calls} calls")

print("\nwhat it keeps")


class ThrottlesAfterFirstPage:
    """One good page, then refusal — the shape that must not lose the good page."""

    def __init__(self):
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        if self.calls == 1:
            return FakeResponse(200, {"properties": {
                "columns": [{"name": "Cost"}, {"name": "UsageDate"},
                            {"name": "ServiceName"}, {"name": "ServiceFamily"},
                            {"name": "Currency"}],
                "rows": [[5.0, 20260801, "VMs", "Compute", "USD"],
                         [7.0, 20260802, "VMs", "Compute", "USD"]],
                "nextLink": "https://example/next",
            }})
        return FakeResponse(429)


async def partial_run():
    client = ThrottlesAfterFirstPage()
    sub = {"id": "sub-c", "name": "Sub C"}
    try:
        rows = await wh._query_period(client, sub, "2026-08-01", "2026-08-31", {},
                                      "ActualCost", deadline=time.monotonic() + 20)
        return rows, None
    except Exception as exc:  # noqa: BLE001
        return None, type(exc).__name__


rows, err = asyncio.run(partial_run())
check("a refusal mid-paging does not silently write a half period",
      rows is None and err in ("TimeoutError", "RuntimeError"), f"{err}, rows={rows}")

# And the mapping still works on a good page, so the deadline work did not break the parser.
good = wh._map_query({
    "columns": [{"name": "Cost"}, {"name": "CostUSD"}, {"name": "UsageDate"},
                {"name": "ServiceName"}, {"name": "ServiceFamily"}, {"name": "Currency"}],
    "rows": [[5.0, 5.0, 20260801, "VMs", "Compute", "USD"]],
}, {"id": "s", "name": "S"})
check("a good page still maps", len(good) == 1)
check("with its family, so the cost areas still classify",
      good[0][idx["ServiceFamily"]] == "Compute")

src = (Path(__file__).resolve().parent / "app" / "warehouse.py").read_text(encoding="utf-8")
check("the deadline is checked before sleeping, not only before requesting",
      "time.monotonic() + wait >= deadline" in src)
check("a timeout is reported as running out of time, not as a failure",
      "except TimeoutError" in src and "ran out of time" in src)
check("and the state names which subscriptions were left behind",
      '"timed_out": timed_out' in src)
check("the budget is configurable", "QUICK_REFRESH_SECONDS" in src)

print(f"\n  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

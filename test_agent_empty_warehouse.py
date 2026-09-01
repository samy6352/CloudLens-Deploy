"""An empty warehouse is not an estate that cost nothing.

Asked what last month cost, the agent ran SQL against a warehouse with nothing in it, got no
rows, and answered "there was no recorded spend in August 2026" — beside a dashboard tile
reading the true figure of the same month, taken live. Both were on screen at once.

SQL cannot tell the two apart: "no data is loaded" and "you spent nothing" are the same empty
result. The difference is the entire meaning of the answer, and money is the one place where a
confident wrong number does more damage than a refusal. So the tool says which one it is,
rather than leaving the model to infer it from a rule it was told once.

    python test_agent_empty_warehouse.py
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import agent

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}  {detail}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


class FakeWarehouse:
    """Rows returned by a query, and rows held overall — the two the bug conflated."""

    def __init__(self, query_rows, loaded_rows):
        self._query_rows = query_rows
        self._loaded = loaded_rows

    def query(self, sql, limit=100, scope=None):
        return {"rows": list(self._query_rows), "sql": sql}

    def summary(self, scope=None):
        return {"rows": self._loaded, "currency": None, "subscriptions": 0}


def with_warehouse(query_rows, loaded_rows):
    import app.warehouse as wh
    wh.warehouse = FakeWarehouse(query_rows, loaded_rows)


def run(coro):
    return asyncio.run(coro)


import app.warehouse as _wh  # noqa: E402
_saved = _wh.warehouse

try:
    print("=" * 72)
    print("nothing loaded, nothing returned")

    with_warehouse(query_rows=[], loaded_rows=0)
    r = run(agent.query_costs("SELECT sum(\"BilledCost\") FROM costs"))
    check("the result says the warehouse is empty", r.get("warehouse_empty") is True,
          "the model cannot see this any other way")
    check("and says so in words the model will act on",
          "NOT evidence that nothing was spent" in (r.get("note") or ""),
          (r.get("note") or "")[:80])
    check("it names the tools that can answer",
          all(t in (r.get("note") or "") for t in ("cost_summary", "cost_trend", "cost_changes")))
    check("and asks for the source to be stated", "live" in (r.get("note") or "").lower())

    print()
    print("=" * 72)
    print("loaded, but this question genuinely has no rows")

    with_warehouse(query_rows=[], loaded_rows=48_000)
    r = run(agent.query_costs("SELECT sum(\"BilledCost\") FROM costs WHERE \"ServiceName\" = 'Nope'"))
    check("a loaded warehouse returning nothing is left alone",
          "warehouse_empty" not in r and not r.get("note"),
          "here 'no rows' really does mean no spend, and saying otherwise would be the same "
          "error inverted")

    print()
    print("=" * 72)
    print("rows came back")

    with_warehouse(query_rows=[{"total": 224.85}], loaded_rows=48_000)
    r = run(agent.query_costs("SELECT sum(\"BilledCost\") AS total FROM costs"))
    check("an answered query is untouched", "warehouse_empty" not in r and not r.get("note"))
    check("and still carries its rows", r["rows"] == [{"total": 224.85}])

    # The empty-warehouse note must not fire on a *failed* query either: an error already says
    # what went wrong, and adding "use the live tools" would send the model past a bad query
    # rather than letting it correct one it could fix.
    print()
    print("=" * 72)
    print("a broken query stays a broken query")

    class Exploding(FakeWarehouse):
        def query(self, sql, limit=100, scope=None):
            raise RuntimeError("Binder Error: no such column \"Nonsense\"")

    import app.warehouse as wh
    wh.warehouse = Exploding([], 0)
    r = run(agent.query_costs("SELECT \"Nonsense\" FROM costs"))
    check("the SQL error is surfaced for the model to fix",
          "SQL failed" in (r.get("error") or ""), (r.get("error") or "")[:60])
    check("and it is not told the warehouse is empty instead",
          "warehouse_empty" not in r,
          "that would hide a fixable query behind a fallback")

    print()
    print("=" * 72)
    print("the instructions match the tool")

    check("the prompt no longer treats 'no rows' as an answer on its own",
          "not the same as" in agent.SYSTEM_PROMPT and "warehouse_empty" in agent.SYSTEM_PROMPT,
          "the rule that produced the wrong answer said only 'if a query returns nothing, say so'")

finally:
    _wh.warehouse = _saved

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

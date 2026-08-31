"""Verify subscription scoping is genuinely enforced, not merely suggested."""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.warehouse import warehouse
from app.agent import query_costs, render_chart

TOTAL_SQL = 'SELECT round(sum("BilledCost"),2) AS total, count(DISTINCT "SubAccountName") AS subs FROM costs'

subs = warehouse.subscriptions()
print("Subscriptions in warehouse:")
for s in subs:
    print(f"  {s['name']:<40} {s['cost']:>9,.2f}  {s['id']}")

full = warehouse.query(TOTAL_SQL)["rows"][0]
print(f"\nUnscoped: {full['total']:,.2f} across {full['subs']} subscriptions")

one = subs[0]
scoped = warehouse.query(TOTAL_SQL, scope=[one["id"]])["rows"][0]
print(f"Scoped to '{one['name']}': {scoped['total']:,.2f} across {scoped['subs']} subscription(s)")
assert scoped["subs"] == 1, "scope did not restrict to one subscription"
assert abs(scoped["total"] - one["cost"]) < 0.01, "scoped total does not match that subscription"
print("  -> matches that subscription's own total")

two = [subs[0]["id"], subs[1]["id"]]
pair = warehouse.query(TOTAL_SQL, scope=two)["rows"][0]
expect = round(subs[0]["cost"] + subs[1]["cost"], 2)
print(f"Scoped to two: {pair['total']:,.2f} (expected {expect:,.2f}), {pair['subs']} subs")
# Tolerance of a couple of cents: `expect` sums two already-rounded totals, whereas the scoped
# query rounds the sum. Not a discrepancy in the data.
assert pair["subs"] == 2 and abs(pair["total"] - expect) < 0.02

print("\nESCAPE ATTEMPTS (model tries to see outside the chosen scope):")
for label, sql in [
    ("qualified catalog", 'SELECT round(sum("BilledCost"),2) FROM costs.main.costs'),
    ("catalog.table",     'SELECT round(sum("BilledCost"),2) FROM costs.costs'),
    ("main-qualified",    'SELECT round(sum("BilledCost"),2) FROM main.costs'),
]:
    try:
        v = warehouse.query(sql, scope=[one["id"]])["rows"][0]
        val = list(v.values())[0]
        status = "!! ESCAPED" if abs(val - full["total"]) < 0.01 else "scoped ok"
        print(f"  {label:<20} -> {val:<10} {status}")
    except Exception as exc:
        print(f"  {label:<20} -> refused ({str(exc)[:60]})")


async def tools():
    print("\nTOOL-LEVEL (scope injected by the app, not the model):")
    r = await query_costs(TOTAL_SQL, _scope=[one["id"]])
    print(f"  query_costs scoped -> {r['rows'][0]} ({r['ms']}ms)")
    assert r["rows"][0]["subs"] == 1

    # Model tries to widen the scope itself; the temp table means it simply cannot.
    sneaky = ('SELECT round(sum("BilledCost"),2) AS total, count(DISTINCT "SubAccountName") AS subs '
              'FROM costs WHERE 1=1 OR "SubAccountName" IS NOT NULL')
    r2 = await query_costs(sneaky, _scope=[one["id"]])
    print(f"  model tries to widen -> {r2['rows'][0]}")
    assert r2["rows"][0]["subs"] == 1, "model widened the scope"

    c = await render_chart(
        sql='SELECT "SubAccountName", sum("BilledCost") AS cost FROM costs GROUP BY 1',
        chart_type="pie", label_column="SubAccountName", value_column="cost",
        title="scoped", _scope=[one["id"]])
    print(f"  render_chart scoped -> {len(c['_chart']['labels'])} slice(s): {c['_chart']['labels']}")
    assert len(c["_chart"]["labels"]) == 1

    print("\nAll scope assertions passed.")


asyncio.run(tools())

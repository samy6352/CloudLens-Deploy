"""Verify the warehouse holds sane data, and time some real questions against it."""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.warehouse import warehouse

s = warehouse.summary()
print("WAREHOUSE")
print(f"  rows          : {s['rows']:,}")
print(f"  range         : {s['from']} to {s['to']}")
print(f"  subscriptions : {s['subscriptions']}")
print(f"  total         : {s['total_cost']:,.2f} {s['currency']}")

QUERIES = [
    ("Monthly totals",
     'SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS month, round(sum("BilledCost"),2) AS cost '
     'FROM costs GROUP BY 1 ORDER BY 1'),
    ("July by service (top 8)",
     'SELECT "ServiceName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "ChargePeriodStart" BETWEEN DATE \'2026-07-01\' AND DATE \'2026-07-31\' '
     'GROUP BY 1 ORDER BY cost DESC LIMIT 8'),
    ("By subscription, July",
     'SELECT "SubAccountName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "ChargePeriodStart" BETWEEN DATE \'2026-07-01\' AND DATE \'2026-07-31\' '
     'GROUP BY 1 ORDER BY cost DESC'),
    ("Top 5 resources overall",
     'SELECT "ResourceName", "ServiceName", round(sum("BilledCost"),2) AS cost FROM costs '
     'GROUP BY 1,2 ORDER BY cost DESC LIMIT 5'),
    ("Regions",
     'SELECT "RegionName", round(sum("BilledCost"),2) AS cost FROM costs '
     'WHERE "RegionName" IS NOT NULL GROUP BY 1 ORDER BY cost DESC LIMIT 5'),
    ("Something only SQL can do: daily spend, 7-day moving average",
     'SELECT "ChargePeriodStart" AS d, round(sum("BilledCost"),2) AS cost, '
     'round(avg(sum("BilledCost")) OVER (ORDER BY "ChargePeriodStart" ROWS 6 PRECEDING),2) AS avg7 '
     'FROM costs GROUP BY 1 ORDER BY 1 DESC LIMIT 5'),
    ("Untagged spend share",
     'SELECT CASE WHEN "Tags" IS NULL OR "Tags" = \'\' THEN \'untagged\' ELSE \'tagged\' END AS t, '
     'round(sum("BilledCost"),2) AS cost FROM costs GROUP BY 1 ORDER BY cost DESC'),
]

print("\nQUERIES")
total_ms = 0
for title, sql in QUERIES:
    try:
        r = warehouse.query(sql, limit=10)
        total_ms += r["ms"]
        print(f"\n  {title}  [{r['ms']}ms]")
        for row in r["rows"]:
            print("    ", {k: (round(v, 2) if isinstance(v, float) else str(v)[:42])
                           for k, v in row.items()})
    except Exception as exc:
        print(f"\n  {title} -> FAILED: {str(exc)[:200]}")

print(f"\n  {len(QUERIES)} queries in {total_ms}ms total")

print("\nSAFETY")
for bad in ["DROP TABLE costs", "DELETE FROM costs", "INSERT INTO costs VALUES (1)",
            "SELECT * FROM read_csv('/etc/passwd')"]:
    try:
        warehouse.query(bad)
        print(f"  !! ALLOWED: {bad}")
    except Exception as exc:
        print(f"  refused: {bad[:34]:<36} ({str(exc)[:40]})")

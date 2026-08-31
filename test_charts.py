"""Check render_chart produces sane specs from real warehouse data."""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.agent import render_chart

CASES = [
    ("Daily spend (line)", dict(
        sql='SELECT "ChargePeriodStart" AS d, round(sum("BilledCost"),2) AS cost FROM costs '
            'GROUP BY 1 ORDER BY 1',
        chart_type="line", label_column="d", value_column="cost", title="Daily spend")),
    ("Top services (hbar)", dict(
        sql='SELECT "ServiceName", round(sum("BilledCost"),2) AS cost FROM costs '
            'GROUP BY 1 ORDER BY cost DESC LIMIT 10',
        chart_type="hbar", label_column="ServiceName", value_column="cost", title="Top services")),
    ("Subscription share (pie)", dict(
        sql='SELECT "SubAccountName", round(sum("BilledCost"),2) AS cost FROM costs GROUP BY 1',
        chart_type="pie", label_column="SubAccountName", value_column="cost", title="By subscription")),
    ("Monthly per service (stacked)", dict(
        sql='SELECT strftime("ChargePeriodStart", \'%Y-%m\') AS m, "ServiceName", '
            'round(sum("BilledCost"),2) AS cost FROM costs GROUP BY 1,2 '
            'HAVING sum("BilledCost") > 40 ORDER BY 1',
        chart_type="stacked_bar", label_column="m", value_column="cost",
        series_column="ServiceName", title="Monthly spend by service")),
]

ERRORS = [
    ("bad column", dict(sql='SELECT 1 AS a', chart_type="bar", label_column="nope",
                        value_column="a", title="x")),
    ("empty result", dict(sql='SELECT "ServiceName", 1 AS c FROM costs WHERE 1=0',
                          chart_type="bar", label_column="ServiceName", value_column="c", title="x")),
    ("blocked sql", dict(sql='DROP TABLE costs', chart_type="bar", label_column="a",
                         value_column="b", title="x")),
]


async def main():
    for name, kw in CASES:
        r = await render_chart(**kw)
        if "error" in r:
            print(f"  FAILED {name}: {r['error'][:150]}")
            continue
        c = r["_chart"]
        print(f"  OK {name:<32} type={c['type']:<12} labels={len(c['labels']):<4} "
              f"series={len(c['datasets']):<3} points={c['points']:<5} {r['ms']}ms")
        d0 = c["datasets"][0]
        print(f"      first series '{d0['label'][:26]}' -> {d0['data'][:4]}")

    print()
    for name, kw in ERRORS:
        r = await render_chart(**kw)
        print(f"  {'refused' if 'error' in r else '!! ALLOWED'}: {name:<14} {r.get('error','')[:80]}")


asyncio.run(main())

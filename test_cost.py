"""Exercise every cost tool against the real subscriptions the signed-in user can see."""
import asyncio, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import cost


async def main() -> int:
    passed = failed = 0

    async def check(title, coro, keys=(), rows=None, n=5):
        nonlocal passed, failed
        print(f"\n{'=' * 72}\n{title}")
        try:
            r = await coro
        except Exception as exc:
            print(f"  FAILED: {str(exc)[:250]}")
            failed += 1
            return None
        for k in keys:
            if k in r:
                print(f"  {k}: {json.dumps(r[k], default=str)[:200]}")
        if rows and isinstance(r.get(rows), list):
            print(f"  {len(r[rows])} row(s):")
            for x in r[rows][:n]:
                print(f"    {json.dumps(x, default=str)[:150]}")
        passed += 1
        return r

    subs = await check("1. Subscriptions I can access", cost.list_subscriptions(),
                       keys=["count"], rows="subscriptions", n=8)

    await check("2. Last complete month, by service (all subs)",
                cost.cost_summary(months_back=1, group_by="ServiceName", top=8),
                keys=["period", "currency", "grand_total", "errors"], rows="breakdown")

    await check("3. Month to date, by subscription",
                cost.cost_summary(months_back=0, group_by="None"),
                keys=["period", "grand_total", "currency"], rows="by_subscription")

    await check("4. Six month trend", cost.cost_trend(months=6),
                keys=["latest_vs_previous_pct", "currency"], rows="months", n=8)

    await check("5. What changed (last month vs prior)", cost.cost_changes(top=5),
                keys=["total_change", "currency"], rows="increases")

    await check("6. Forecast, next 30 days", cost.cost_forecast(days_ahead=30),
                keys=["period", "projected_total", "subscriptions_included"])

    await check("7. Budgets", cost.budgets(), keys=["count", "note"], rows="budgets")

    await check("8. Top resources by cost", cost.top_resources(months_back=1, top=5),
                keys=["currency"], rows="breakdown")

    # A scope the user cannot see must produce a clear error, not a wrong answer.
    print(f"\n{'=' * 72}\n9. Unknown subscription must be refused")
    try:
        await cost.cost_summary(subscription_ids=["not-a-real-subscription"])
        print("  !! ACCEPTED - should have been refused")
        failed += 1
    except cost.CostError as exc:
        print(f"  REFUSED: {str(exc)[:150]}")
        passed += 1

    await cost.azure.close()
    print(f"\n{'=' * 72}\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


sys.exit(asyncio.run(main()))

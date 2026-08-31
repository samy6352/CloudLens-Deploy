"""Verify the subscription scope also constrains the waste/utilisation tools."""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import cost, waste
from app.warehouse import warehouse


async def main() -> int:
    subs = warehouse.subscriptions()
    one = subs[0]
    print(f"scoping to: {one['name']}\n")

    full = await waste.find_waste(days=30)
    scoped = await waste.find_waste(days=30, subscription_ids=[one["id"]])
    print(f"  unscoped : {full['total_items']:>3} items, {full['total_cost']:>8,.2f} USD, "
          f"{full['subscriptions']} subs")
    print(f"  scoped   : {scoped['total_items']:>3} items, {scoped['total_cost']:>8,.2f} USD, "
          f"{scoped['subscriptions']} sub")
    assert scoped["subscriptions"] == 1
    assert scoped["total_items"] <= full["total_items"]
    assert scoped["total_cost"] <= full["total_cost"] + 0.01
    print("  -> scope narrowed the findings")

    uf = await waste.vm_utilisation(days=30)
    us = await waste.vm_utilisation(days=30, subscription_ids=[one["id"]])
    print(f"\n  VMs unscoped: {uf['count']}   scoped: {us['count']}")
    assert us["count"] <= uf["count"]

    af = await waste.advisor_recommendations()
    a_s = await waste.advisor_recommendations(subscription_ids=[one["id"]])
    print(f"  Advisor unscoped: {af['count']}   scoped: {a_s['count']}")
    assert a_s["count"] <= af["count"]

    print("\nAll waste scope assertions passed.")
    await cost.azure.close()
    return 0


sys.exit(asyncio.run(main()))

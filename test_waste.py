"""Exercise the waste and utilisation tools against the real estate."""
import asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import cost, waste


async def main() -> int:
    t0 = time.time()
    print("=" * 74)
    print("IDLE / ORPHANED RESOURCES")
    r = await waste.find_waste(days=30)
    print(f"  {r['total_items']} item(s) across {r['subscriptions']} subscription(s), "
          f"{r['total_cost']:,.2f} {r['currency']} in {r['period_days']} days\n")
    for f in r["findings"]:
        if not f["count"]:
            continue
        print(f"  {f['title']}  —  {f['count']} item(s), {f['cost']:,.2f} USD")
        print(f"    {f['why']}")
        for it in f["items"][:3]:
            print(f"      {it['cost']:>8,.2f}  {str(it['name'])[:34]:<34} "
                  f"{str(it['resource_group'])[:18]:<18} {it['detail']}")
        if f["more"]:
            print(f"      … {f['more']} more")
        print()

    print("=" * 74)
    print("VM UTILISATION")
    u = await waste.vm_utilisation(days=30)
    print(f"  {u['count']} VM(s): {u['running']} running, {u['stopped_count']} stopped/deallocated, "
          f"{u['idle_count']} idle (<{u['cpu_threshold']}% CPU)")
    print(f"  idle cost {u['idle_cost']:,.2f} | stopped cost {u['stopped_cost']:,.2f}\n")
    print(f"  {'VM':<24}{'STATE':<14}{'SIZE':<20}{'CPU avg':>8}{'peak':>8}{'cost':>10}")
    for v in u["vms"][:12]:
        avg = f"{v['cpu_avg']}%" if v["cpu_avg"] is not None else "—"
        peak = f"{v['cpu_peak']}%" if v["cpu_peak"] is not None else "—"
        print(f"  {v['name'][:23]:<24}{v['state']:<14}{str(v['size'])[:19]:<20}"
              f"{avg:>8}{peak:>8}{v['cost']:>10,.2f}")

    print("\n" + "=" * 74)
    print("ADVISOR (cost)")
    a = await waste.advisor_recommendations()
    print(f"  {a['count']} recommendation(s); estimated annual saving "
          f"{a['estimated_annual_savings']} {a['currency'] or ''}")
    for rec in a["recommendations"][:5]:
        print(f"    [{rec['impact']}] {str(rec['problem'])[:64]}")
        print(f"        -> {str(rec['solution'])[:70]}  ({rec['resource']})")
    if a.get("note"):
        print(f"  {a['note']}")

    print(f"\nelapsed {time.time() - t0:.1f}s")
    await cost.azure.close()
    return 0


sys.exit(asyncio.run(main()))

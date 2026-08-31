"""Does the tags endpoint now surface tags that exist but carry no cost?"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import cost
from app.tags import cost_by_tag, live_tag_keys, merge_live_keys

SUB = "11111111-2222-3333-4444-555555555555"


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print("=" * 72)
    print("live tag keys from Resource Graph")
    live = await live_tag_keys([SUB])
    print(f"  {len(live)} key(s): {[k['key'] for k in live]}")
    check("Resource Graph returned tag keys", len(live) > 0)

    print("\n" + "=" * 72)
    print("warehouse-derived keys")
    base = cost_by_tag([SUB], days=30)
    costed = [k["key"] for k in base["keys"]]
    print(f"  {len(costed)} key(s) with cost: {costed}")

    print("\n" + "=" * 72)
    print("merged")
    merged = merge_live_keys(dict(base), live)
    unc = merged.get("uncosted", [])
    print(f"  {len(unc)} uncosted: {[(k['key'], k['resources']) for k in unc]}")

    check("uncosted is present", "uncosted" in merged)
    check("no key is both costed and uncosted",
          not (set(costed) & {k['key'] for k in unc}))
    check("the deallocated VM's tags show up as uncosted",
          {"App", "Cost center", "Owner"} <= {k["key"] for k in unc},
          f"got {sorted(k['key'] for k in unc)}")
    check("costed keys are untouched by the merge", merged["keys"] == base["keys"])
    check("totals are untouched by the merge", merged["total"] == base["total"])

    print("\n" + "=" * 72)
    print("daily detail")
    dates = base.get("dates") or []
    print(f"  {len(dates)} day(s): {dates[:3]}{' … ' + dates[-1] if len(dates) > 3 else ''}")
    check("a date series is returned", len(dates) > 0)
    check("dates are sorted and unique", dates == sorted(set(dates)))
    check("daily_total lines up with dates", len(base.get("daily_total") or []) == len(dates))

    withdays = [r for r in base["resources"] if r.get("d")]
    check("resources carry daily cells", len(withdays) > 0,
          f"{len(withdays)}/{len(base['resources'])}")
    check("every day index is inside the window",
          all(0 <= i < len(dates) for r in base["resources"] for i, _ in r.get("d") or []))

    # The per-resource daily cells must add up to the same money as the resource totals, or the
    # chart and the table below it disagree and one of them is lying.
    for r in withdays[:200]:
        if abs(sum(c for _, c in r["d"]) - r["cost"]) > 0.02:
            check("daily cells sum to the resource total", False,
                  f"{r['name']}: {sum(c for _, c in r['d'])} vs {r['cost']}")
            break
    else:
        check("daily cells sum to the resource total", True)

    # The estate-wide daily series and the per-resource cells are two views of one number. If
    # they drift, a bar on the chart and the day drill-down under it show different money for
    # the same day -- which is exactly what carrying untagged resources here is meant to stop.
    cells = sum(c for r in base["resources"] for _, c in r.get("d") or [])
    series = sum(base.get("daily_total") or [])
    check("per-resource cells reconcile with the daily series",
          abs(cells - series) < 0.05, f"{cells:.2f} vs {series:.2f}")

    untagged_in = [r for r in base["resources"] if not r["keys"]]
    check("untagged resources are carried for the day view",
          len(untagged_in) == base["untagged"]["resources"],
          f"{len(untagged_in)} vs {base['untagged']['resources']}")
    check("tagged count excludes them",
          len([r for r in base["resources"] if r["keys"]]) == base["tagged"]["resources"])

    print("\n" + "=" * 72)
    print("tag values")
    pool = base.get("values") or []
    print(f"  {len(pool)} distinct value(s) in the pool")
    check("a value pool is returned", len(pool) > 0)
    check("keys and value indexes line up",
          all(len(r["keys"]) == len(r.get("v") or []) for r in base["resources"]))
    check("every value index resolves",
          all(0 <= i < len(pool) for r in base["resources"] for i in r.get("v") or []))
    check("the pool has no duplicates", len(pool) == len(set(pool)))

    multi = [k for k in base["keys"] if k.get("values", 0) > 1]
    print(f"  {len(multi)} key(s) with more than one value: "
          f"{[(k['key'], k['values']) for k in multi[:5]]}")
    check("multi-valued keys are reported", len(multi) > 0)

    # The point of the whole feature: one key, several values, each its own cost centre.
    def value_of(res, key):
        i = res["keys"].index(key) if key in res["keys"] else -1
        return pool[res["v"][i]] if i >= 0 else None

    if multi:
        k = multi[0]["key"]
        seen: dict[str, float] = {}
        for r in base["resources"]:
            v = value_of(r, k)
            if v is not None:
                seen[v] = seen.get(v, 0.0) + r["cost"]
        print(f"  {k}: " + ", ".join(f"{v or '(no value)'}={c:.2f}" for v, c in
                                     sorted(seen.items(), key=lambda x: -x[1])[:5]))
        check(f"'{k}' splits into its distinct values", len(seen) == multi[0]["values"],
              f"{len(seen)} vs {multi[0]['values']}")
        check("the values add up to the key's total",
              abs(sum(seen.values()) - multi[0]["cost"]) < 0.05,
              f"{sum(seen.values()):.2f} vs {multi[0]['cost']:.2f}")
        check("no single value accounts for the whole key",
              max(seen.values()) < multi[0]["cost"] - 0.001 or len(seen) == 1)

    print("\n" + "=" * 72)
    print("pair parsing")
    from app.tags import _pairs
    check("a JSON object parses to pairs",
          _pairs('{"Owner":"Deb","env":"prod"}') == [("Owner", "Deb"), ("env", "prod")])
    check("a legacy string parses to pairs",
          _pairs('"Owner": "Deb"; "env": "prod"') == [("Owner", "Deb"), ("env", "prod")])
    check("an empty value survives as an empty string",
          _pairs('{"Owner":""}') == [("Owner", "")])
    check("a null value does not become the text 'None'",
          _pairs('{"Owner":null}') == [("Owner", "")])
    check("malformed tags are treated as untagged", _pairs("{not json") == [])
    check("a non-string value is stringified", _pairs('{"n":3}') == [("n", "3")])

    print("\n" + "=" * 72)
    print("failure is soft, and scope is honoured")
    empty = await live_tag_keys([])
    check("an explicitly empty scope returns [] rather than the whole estate", empty == [])
    same = merge_live_keys({"keys": [{"key": "a", "cost": 1.0, "resources": 1}]}, [])
    check("merging nothing leaves the payload alone", "uncosted" not in same)

    # A quick refresh loads costs through the Query API, which cannot return tags at all. The
    # tab then had no keys to show and explained it with "None of the 217 resources in this
    # period carry a tag" -- a confident statement about someone's estate that was simply
    # false, and on the very tab being demonstrated. The three cases are different facts and
    # have to read differently.
    main_py = open("app/main.py", encoding="utf-8").read()
    dash = open("web/assets/dashboard.js", encoding="utf-8").read()
    check("the tags payload says whether tags were even loaded",
          '"tags_not_loaded"' in main_py or "tags_not_loaded" in main_py)
    check("and it is derived from what the last refresh omitted",
          'warehouse.state.get("omits")' in main_py)
    # In-memory state is rebuilt from a summary on boot, so a restart forgot how the data got
    # there and the tab went straight back to blaming the estate. The ingest log lives in the
    # database and survives exactly as long as the rows it describes.
    check("and it survives a restart by reading the ingest log too",
          "FROM ingest_log WHERE status = 'ok'" in main_py)
    check("the log check looks for a quick load", '"quick" in' in main_py)
    check("the empty tab distinguishes 'not loaded' from 'not tagged'",
          "d.tags_not_loaded" in dash)
    check("it does not claim the estate is untagged when tags were never fetched",
          dash.index("d.tags_not_loaded") < dash.index("carry a tag."))
    check("and it points at a refresh route that does return tags",
          "FOCUS report" in dash and "cannot " in dash)

    await cost.azure.close()
    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

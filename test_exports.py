"""Exercise export discovery and ingest against the real tenant."""
import asyncio, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app import cost, exports


async def main() -> int:
    print("=" * 74)
    print("DISCOVERY")
    d = await exports.discover_exports()
    print(f"  {d['count']} export(s) visible\n")
    for e in d["exports"]:
        print(f"  {e['name'][:34]:<36} {e['type']:<15} {e['schedule'] or '-':<8} {e['status'] or '-'}")
        print(f"      -> {e['storage_account']}/{e['container']}/{e['root_folder'] or ''}")
        print(f"         format={e['format']} gzip={e['compressed']} partitioned={e['partitioned']}")
    if d.get("note"):
        print(f"  {d['note']}")

    if d["exports"]:
        e = d["exports"][0]
        print("\n" + "=" * 74)
        print(f"RUN HISTORY for {e['name']}")
        try:
            for r in await exports.export_runs(e["subscription_id"], e["name"]):
                print(f"  {r['status']:<12} submitted={r['submitted']}  file={r['file_name']}")
        except Exception as exc:
            print(f"  failed: {str(exc)[:180]}")

    print("\n" + "=" * 74)
    print("INGEST ATTEMPT (all discovered exports)")
    r = await exports.ingest_all_exports()
    print(f"  discovered={r['discovered']}  loaded={len(r['loaded'])}  failed={len(r['failed'])}"
          f"  rows={r['rows_loaded']:,}")
    for l in r["loaded"]:
        print(f"  OK   {l['export'][:30]:<32} {l['rows']:>7,} rows  {l['from']}..{l['to']}"
              f"  {l['seconds']}s")
    for f in r["failed"]:
        print(f"  FAIL {f['export'][:30]:<32} {f['reason'][:110]}")
    if r.get("note"):
        print(f"\n  {r['note']}")

    await cost.azure.close()
    return 0


sys.exit(asyncio.run(main()))

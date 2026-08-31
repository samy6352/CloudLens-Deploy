"""Populate the local cost warehouse from all accessible subscriptions.

    python ingest.py            # last 3 months
    python ingest.py --months 6
"""
import argparse, asyncio, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# A Windows console defaults to cp1252, which cannot encode the arrow in the summary. Without
# this the ingest completes, writes every row, then dies printing its own success and exits 1 --
# so a run that worked looks like a run that failed. Reconfigure rather than drop the character.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import cost
from app.warehouse import warehouse


async def main(months: int) -> int:
    subs = (await cost.list_subscriptions())["subscriptions"]
    print(f"Subscriptions: {len(subs)}")
    for s in subs:
        print(f"  - {s['name']}")
    print(f"\nIngesting {months} month(s) — reports take a few minutes each, run concurrently.\n")

    started = time.time()

    async def progress() -> None:
        while True:
            await asyncio.sleep(20)
            st = warehouse.state
            if st.get("status") != "running":
                return
            print(f"  … {st.get('done', 0)}/{st.get('total', 0)} jobs, "
                  f"{st.get('rows', 0):,} rows, {int(time.time() - started)}s")

    watcher = asyncio.create_task(progress())
    try:
        await warehouse.ingest(subs, months=months)
    finally:
        watcher.cancel()

    s = warehouse.summary()
    print(f"\n{'=' * 66}")
    print(f"  rows          : {s['rows']:,}")
    print(f"  date range    : {s['from']} → {s['to']}")
    print(f"  subscriptions : {s['subscriptions']}")
    totals = " + ".join(f"{t['cost']:,.2f} {t['currency']}" for t in s["total_by_currency"])
    print(f"  total cost    : {totals or '0'}")
    print(f"  elapsed       : {int(time.time() - started)}s")
    print(f"  failures      : {warehouse.state.get('failed', 0)}")

    await cost.azure.close()
    return 0 if s["rows"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    sys.exit(asyncio.run(main(ap.parse_args().months)))

"""Does per-user backfill notice missing subscriptions without lying about them?"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from app import backfill
from app.warehouse import warehouse

REAL = "11111111-2222-3333-4444-555555555555"
FAKE = "00000000-0000-0000-0000-0000deadbeef"


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
    print("what the warehouse already holds")
    have = backfill.present()
    print(f"  {len(have)} subscription(s) loaded")
    check("the ingested subscription is seen as present", REAL.lower() in have)
    check("an unknown subscription is not", FAKE.lower() not in have)

    print("\n" + "=" * 72)
    print("status for a user entitled to one loaded and one missing subscription")
    entitled = [{"id": REAL, "name": "loaded-one"}, {"id": FAKE, "name": "missing-one"}]
    st = backfill.status_for(entitled)
    names = [p["name"] for p in st["pending"]] + [f["name"] for f in st["failed"]]
    print(f"  pending={[p['name'] for p in st['pending']]} failed={[f['name'] for f in st['failed']]}")
    check("the loaded subscription is not reported as missing", "loaded-one" not in names)
    check("the missing subscription is reported", "missing-one" in names)
    check("something is flagged as outstanding", st["loading"] or st["failed"])

    print("\n" + "=" * 72)
    print("a user entitled only to loaded subscriptions is told nothing")
    quiet = backfill.status_for([{"id": REAL, "name": "loaded-one"}])
    check("no pending", not quiet["pending"])
    check("no failed", not quiet["failed"])
    check("not loading", not quiet["loading"])
    check("no entitlement at all is handled", backfill.status_for(None)["loading"] is False)

    print("\n" + "=" * 72)
    print("a known failure is reported as failed, not as still loading")
    backfill._seen[FAKE.lower()] = {
        "status": "failed", "name": "missing-one",
        "detail": "403 the app identity has no access", "at": time.time(),
    }
    st2 = backfill.status_for(entitled)
    check("it moves out of pending", not any(p["id"] == FAKE for p in st2["pending"]))
    check("it appears as failed with a reason",
          any(f["id"] == FAKE and "403" in f["detail"] for f in st2["failed"]))

    print("\n" + "=" * 72)
    print("scheduling does not re-run a fresh failure, and never raises")
    before = set(backfill._running)
    backfill.schedule(entitled, months=1)
    check("the fresh failure is not retried", FAKE.lower() not in (backfill._running - before))
    backfill.schedule(None)
    backfill.schedule([])
    check("empty input is a no-op", True)

    print("\n" + "=" * 72)
    print("backfill does not clobber the refresh progress state")
    warehouse.state = {"status": "running", "done": 3, "total": 9, "sentinel": True}
    marker = dict(warehouse.state)

    class Boom:
        state = warehouse.state

        async def ingest(self, *a, **k):
            raise RuntimeError("report failed 403")

    try:
        await backfill._ingest_one(warehouse, {"id": FAKE, "name": "x"}, 1)
    except Exception:
        pass
    check("the shared progress state survives a failed backfill",
          warehouse.state == marker, str(warehouse.state)[:120])

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

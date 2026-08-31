"""Does the warehouse ingest read as the signed-in person, not as the app?

This is the difference between a multi-tenant deployment that works and one that signs people
in correctly and then shows them an empty dashboard.
"""
import asyncio
import inspect
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

os.environ.setdefault("PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/p")

SUB_A = "aaaaaaaa-1111-2222-3333-444444444444"
SUB_B = "bbbbbbbb-1111-2222-3333-444444444444"


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    from app import cost, warehouse as wh

    print("=" * 72)
    print("no signed-in person: falls back to the app identity")
    check("caller_token returns None when not acting as anyone",
          cost.caller_token(f"/subscriptions/{SUB_A}") is None)

    print("\n" + "=" * 72)
    print("acting as a person: their token is used")
    caller = cost.Caller(
        fallback="HOME-TOKEN",
        by_subscription={SUB_A.lower(): "TOKEN-FOR-A", SUB_B.lower(): "TOKEN-FOR-B"},
    )
    reset = cost.act_as(caller, "someone@example.com")
    try:
        check("the right token is chosen per subscription",
              cost.caller_token(f"/subscriptions/{SUB_A}") == "TOKEN-FOR-A")
        check("a different subscription gets a different token",
              cost.caller_token(f"/subscriptions/{SUB_B}") == "TOKEN-FOR-B")
        check("an unknown subscription falls back to the home token",
              cost.caller_token("/subscriptions/cccccccc-0000-0000-0000-000000000000")
              == "HOME-TOKEN")

        # The real point: the warehouse's own token function must honour this, or the ingest
        # runs as the app no matter how carefully the request was scoped.
        check("warehouse._token uses the caller's token",
              wh._token(f"/subscriptions/{SUB_A}") == "TOKEN-FOR-A")
        check("warehouse._token picks per subscription",
              wh._token(f"/subscriptions/{SUB_B}") == "TOKEN-FOR-B")
    finally:
        cost.stop_acting(reset)

    check("after stop_acting, the delegated token is gone",
          cost.caller_token(f"/subscriptions/{SUB_A}") is None)

    print("\n" + "=" * 72)
    print("ingest builds a header per subscription, not one for the batch")
    src = inspect.getsource(wh.Warehouse.ingest)
    check("there is a per-subscription header builder", "def headers_for(" in src)
    check("the header is built inside the per-job coroutine",
          'headers = headers_for(sub["id"])' in src)
    check("no single batch-wide header survives",
          'headers = {"Authorization": f"Bearer {_token()}"' not in src)

    print("\n" + "=" * 72)
    print("the backfill inherits the caller's context")
    import app.main as appmain

    subsrc = inspect.getsource(appmain.subscriptions)
    sched = subsrc.index("backfill.schedule(")
    block = subsrc.rindex("as_user(user)", 0, sched)
    check("backfill.schedule runs inside an as_user block", block < sched)
    check("an expired Azure session does not break the picker",
          "except HTTPException:" in subsrc[block:])

    # asyncio.create_task copies the current context; this is what carries the token in.
    seen = {}

    async def probe():
        seen["token"] = cost.caller_token(f"/subscriptions/{SUB_A}")

    reset2 = cost.act_as(caller, "someone@example.com")
    try:
        t = asyncio.create_task(probe())
    finally:
        cost.stop_acting(reset2)
    await t
    check("a task created while acting still sees the token",
          seen.get("token") == "TOKEN-FOR-A", str(seen.get("token")))

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

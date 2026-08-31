"""First-run setup: the offer, and the promise that it is made only once.

The failure modes here are all about showing the card at the wrong moment. Offering setup to
someone whose data is already loading is the same "empty and loading look identical" confusion
the card exists to fix, only inverted — and offering it twice makes a one-time decision look
like a nag.

    python test_onboarding.py
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import onboarding

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


class FakeMeta:
    """Stands in for the warehouse's meta table, so these checks never touch real data."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get_meta(self, key, default=None):
        return self.store.get(key, default)

    def set_meta(self, key, value):
        self.store[key] = value


def with_meta(fake):
    """Point the module's warehouse lookups at a fake for the duration of one check."""
    import app.warehouse as wh

    wh.warehouse = fake  # type: ignore[assignment]


# The real warehouse, restored at the end so importing this file cannot poison another suite.
import app.warehouse as _wh

_real = _wh.warehouse


print("\nthe offer appears only on a genuinely empty, unanswered deployment")
print("=" * 72)
with_meta(FakeMeta())
onboarding._progress.update(running=False, steps=[], started=0.0, finished=0.0)

s = onboarding.state(rows=0, is_admin=True, subscriptions=3)
check("no rows and never answered is a first run", s["first_run"] is True)
check("and an admin who can see subscriptions is offered the scan", s["can_scan"] is True)
check("with nothing blocking it", "blocked" not in s)

s = onboarding.state(rows=20_000, is_admin=True, subscriptions=3)
check("a deployment with rows is not a first run", s["first_run"] is False,
      "the data is its own record that setup happened")
check("and is offered nothing", s["can_scan"] is False)

# One row is enough. Somebody's manual refresh, the boot-time ingest, or a colleague getting
# there first all count — the question is whether there is data, not who put it there.
check("a single row is enough to settle it",
      onboarding.state(rows=1, is_admin=True, subscriptions=3)["first_run"] is False)


print("\nan ingest already running is not an invitation to start another")
print("=" * 72)
with_meta(FakeMeta())
s = onboarding.state(rows=0, is_admin=True, subscriptions=3, ingesting=True)
check("it is still technically a first run", s["first_run"] is True)
check("but the scan is not offered", s["can_scan"] is False,
      "two ingests would write the same rows")
check("and it says data is already on its way",
      "already being loaded" in (s.get("blocked") or ""), s.get("blocked", "")[:60])
check("the wait is reported separately from this card's own scan",
      s["ingesting"] is True and s["running"] is False)


print("\nwho is told what")
print("=" * 72)
with_meta(FakeMeta())
s = onboarding.state(rows=0, is_admin=False, subscriptions=3)
check("a non-admin is not offered a button that would 403", s["can_scan"] is False)
check("and is told to ask an administrator",
      "administrator" in (s.get("blocked") or ""), s.get("blocked", "")[:60])

s = onboarding.state(rows=0, is_admin=True, subscriptions=0)
check("an admin who can see no subscriptions is not offered a scan", s["can_scan"] is False)
check("and is told their Azure access is the problem",
      "cannot see any Azure subscriptions" in (s.get("blocked") or ""),
      s.get("blocked", "")[:60])


print("\nonce answered, never asked again")
print("=" * 72)
fake = FakeMeta()
with_meta(fake)
check("an unanswered deployment offers setup",
      onboarding.state(rows=0, is_admin=True, subscriptions=2)["first_run"] is True)

onboarding.settle("dismissed")
check("dismissing records it", fake.store.get(onboarding.SETTLED_KEY) == "dismissed",
      str(fake.store))
s = onboarding.state(rows=0, is_admin=True, subscriptions=2)
check("and the offer is gone even though there is still no data",
      s["first_run"] is False,
      "this is the whole point: an empty estate must not be asked forever")
check("and cannot be scanned", s["can_scan"] is False)

fake2 = FakeMeta()
with_meta(fake2)
onboarding.settle("done")
check("a completed scan settles it the same way",
      onboarding.state(rows=0, is_admin=True, subscriptions=2)["first_run"] is False)
check("and the reason it was settled is kept",
      onboarding.state(rows=0, is_admin=True, subscriptions=2)["settled"] == "done")


print("\nprogress is reported as steps, not as a spinner")
print("=" * 72)
with_meta(FakeMeta())
onboarding._progress.update(running=True, steps=[], started=0.0, finished=0.0)
onboarding._step("Looking for exports", "running")
onboarding._step("Looking for exports", "skipped", "none readable")
onboarding._step("Loading cost history", "running")

p = onboarding.progress()
check("a step is updated in place, not duplicated", len(p["steps"]) == 2,
      f"{[s['name'] for s in p['steps']]}")
check("its status moves with it", p["steps"][0]["status"] == "skipped")
check("and carries the reason", p["steps"][0]["detail"] == "none readable")
check("order is preserved", p["steps"][1]["name"] == "Loading cost history")

# The card reads this while the scan mutates it; handing out the live object would let a
# render iterate a list that grows underneath it.
p["steps"].append({"name": "injected", "status": "done", "detail": ""})
check("progress() hands back a copy", len(onboarding.progress()["steps"]) == 2)

onboarding._progress.update(running=False, steps=[], started=0.0, finished=0.0)


print("\na scan cannot be started twice")
print("=" * 72)
onboarding._progress["running"] = True
check("start() refuses while one is in flight",
      onboarding.start([{"id": "x", "name": "x"}]) is False,
      "two would create duplicate exports and race on the same rows")
onboarding._progress["running"] = False


print("\nthe escalation ladder is ordered cheapest and most complete first")
print("=" * 72)
import inspect

src = inspect.getsource(onboarding._run)
i_existing = src.index("_use_existing_exports")
i_create = src.index("_create_exports")
i_api = src.index("_load_from_api")
check("existing exports are tried before creating one", i_existing < i_create)
check("and creating one is tried before the slow API path", i_create < i_api)
check("the API path runs whenever nothing above it produced rows",
      "if not loaded:" in src,
      "an export set up for tomorrow leaves today's dashboard empty")
check("success is recorded from the data, not from the attempt",
      src.index("if loaded:") < src.index('settle("done")'),
      "a failed scan must stay retryable")


_wh.warehouse = _real

print("\n" + "=" * 72)
print(f"{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

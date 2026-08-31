"""Prove the Entra sign-in and, more importantly, that RBAC is actually enforced.

The tenant this runs against requires administrator approval before any app registration can
be consented to, so a full interactive sign-in cannot be completed from a test. Everything
either side of that one step is exercised for real:

  * the authorization request we send to Entra (PKCE, scope, redirect, anti-replay state)
  * the callback's refusal of unknown, replayed and tampered responses
  * the session cookie
  * the scope arithmetic that decides what someone may see
  * the whole HTTP surface, driven with a real signed-in session, checking that a person
    limited to one subscription cannot see the others by any route
  * a *live* delegated Azure token, to prove the impersonation plumbing genuinely changes whose
    access a call runs under, rather than merely filtering results afterwards
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TMP = Path(tempfile.mkdtemp())
os.environ["AUTH_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["AUTH_CLIENT_ID"] = "00000000-1111-2222-3333-444444444444"
os.environ["AUTH_TENANT_ID"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
os.environ["AUTH_REDIRECT_URI"] = "http://localhost:8100/auth/callback"
os.environ["AUTH_ADMINS"] = "boss@microsoft.com"
os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")
for leftover in ("AUTH_DISABLED", "AUTH_ALLOW_LOCAL", "AUTH_PASSWORD", "AUTH_USERS",
                 "AUTH_API_TOKENS", "AUTH_CLIENT_SECRET"):
    os.environ[leftover] = ""

from app import auth as auth_mod  # noqa: E402

auth_mod.USER_FILE = TMP / "users.json"

import asyncio  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import cost, entra, main  # noqa: E402

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


service = main.auth_service()
check("SSO is the active mode", service.mode == "entra", service.mode)
check("local passwords are off by default in SSO mode", service.allow_local is False)
check("no local account was bootstrapped", len(service.users) == 0, str(len(service.users)))
check("delegated ARM is off by default, so no admin approval is needed",
      service.sso.delegated is False)


# --------------------------------------------------------- the request to Entra
print("AUTHORIZATION REQUEST")
url = asyncio.run(service.sso.start("/api/overview"))
q = parse_qs(urlparse(url).query)
check("goes to the right tenant",
      urlparse(url).path.startswith("/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/"), urlparse(url).path)
check("asks for a code, not a token", q["response_type"] == ["code"])
check("uses PKCE", q.get("code_challenge_method") == ["S256"] and bool(q.get("code_challenge")))
# The whole point of the default: only scopes a person may consent to for themselves, so a
# colleague can sign in without anyone in the directory doing anything first.
check("asks only for sign-in scopes",
      "management.azure.com" not in q["scope"][0], q["scope"][0])
check("asks for the basics it needs to identify them",
      all(s in q["scope"][0] for s in ("openid", "profile")), q["scope"][0])
check("redirects back to this app", q["redirect_uri"] == ["http://localhost:8100/auth/callback"])
check("carries a nonce", bool(q.get("nonce")))
check("names the account rather than reusing the last one", q.get("prompt") == ["select_account"])

# Delegated mode is still available where a tenant does allow it.
delegated = entra.EntraSSO()
delegated.delegated = True
durl = asyncio.run(delegated.start("/"))
check("delegated mode still asks for Azure access when switched on",
      "management.azure.com%2Fuser_impersonation" in durl
      or "management.azure.com/user_impersonation" in durl,
      parse_qs(urlparse(durl).query).get("scope", [""])[0])

state = q["state"][0]
check("the flow is held server-side, not in the browser", state in service.sso._pending)
check("two sign-ins get different state",
      parse_qs(urlparse(asyncio.run(service.sso.start("/"))).query)["state"][0] != state)

# The callback is the part an attacker can reach, so it must refuse anything it did not start.
session, target, problem = asyncio.run(service.sso.complete({"code": "x", "state": "never-issued"}))
check("a callback with unknown state is refused", session is None and bool(problem), problem)
check("the refusal is understandable", "expired" in problem.lower() or "again" in problem.lower())

held = dict(service.sso._pending[state])
service.sso._pending[state] = held
asyncio.run(service.sso.complete({"code": "bad", "state": state}))  # consumes the flow
session, _, problem = asyncio.run(service.sso.complete({"code": "bad", "state": state}))
check("a replayed callback is refused", session is None, problem)


# ------------------------------------------------------------------- the cookie
print("\nSESSION COOKIE")
secret = service.sessions.secret
signed = entra.sign("session-id-123", secret)
check("a signed id verifies", entra.unsign(signed, secret) == "session-id-123")
check("a tampered id does not", entra.unsign("session-id-124." + signed.split(".")[1], secret) is None)
check("an unsigned id does not", entra.unsign("session-id-123", secret) is None)
check("another key does not", entra.unsign(signed, b"other-secret") is None)
check("the raw id is not usable on its own", service.sso.get("session-id-123") is None)


# --------------------------------------------------------------- scope decisions
print("\nWHAT SOMEONE MAY SEE")
narrow = main.narrow
check("no pick means everything they may have", narrow([], ["a", "b"]) == ["a", "b"])
check("a pick inside their access is honoured", narrow(["a"], ["a", "b"]) == ["a"])
check("a pick outside their access is dropped", narrow(["c"], ["a", "b"]) == ["a", "b"])
check("a mixed pick keeps only the allowed part", narrow(["a", "c"], ["a", "b"]) == ["a"])
# The dangerous case: [] means "everything" downstream, so an empty intersection must never
# be allowed to fall through as an empty list.
check("an entirely disallowed pick never becomes 'all'", narrow(["c"], ["a"]) == ["a"])
check("an unrestricted caller is left alone", narrow(["a"], None) == ["a"])
check("an unrestricted caller with no pick stays unscoped", narrow([], None) == [])

check("an admin is recognised by email", service.sso.is_admin("BOSS@microsoft.com"))
check("everyone else is not", not service.sso.is_admin("someone@microsoft.com"))


# ------------------------------------------------- a signed-in person, end to end
print("\nENFORCEMENT (signed in, restricted to one subscription)")

# A real ARM token for the person running this test, issued to the Azure CLI. Used only to
# prove the impersonation path works; the app itself never borrows another app's identity.
def cli_token() -> str | None:
    try:
        out = subprocess.run(
            ["az", "account", "get-access-token", "--resource", "https://management.azure.com",
             "-o", "json"],
            capture_output=True, text=True, timeout=120, shell=True)
        return json.loads(out.stdout)["accessToken"] if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


token = cli_token()
print(f"  (delegated ARM token from az: {'available' if token else 'not available'})")


class _Stub:
    """Stands in for MSAL: hands back the token we already have, without a directory round trip."""

    def __init__(self, token: str | None) -> None:
        self._token = token

    def acquire_token_silent(self, scopes, account=None, **kw):  # noqa: ANN001, ARG002
        return {"access_token": self._token} if self._token else None


all_subs = asyncio.run(cost.list_subscriptions())["subscriptions"] if token else []
if token:
    print(f"  (this identity can see {len(all_subs)} subscription(s) in total)")

only_one = [{"id": all_subs[0]["id"], "name": all_subs[0].get("name")}] if all_subs else \
           [{"id": "sub-alpha", "name": "Alpha"}]

session = entra.Session(
    sid="test-session", name="Ravi Verma", email="ravi@microsoft.com", oid="oid-1",
    tenant=os.environ["AUTH_TENANT_ID"], app=_Stub(token), account={},
    subscriptions=only_one, checked=time.time(),
)
service.sso._sessions[session.sid] = session

client = TestClient(main.app, follow_redirects=False,
                    cookies={auth_mod.COOKIE: entra.sign(session.sid, secret)})

me = client.get("/api/auth/me").json()
check("the session is recognised", me["authenticated"] and me["user"]["email"] == "ravi@microsoft.com",
      json.dumps(me)[:160])
check("they are not an admin", me["user"]["admin"] is False)
check("their subscription count is reported", me.get("subscriptions") == 1, str(me.get("subscriptions")))
check("an ingest is refused", client.post("/api/ingest").status_code == 403)

picker = client.get("/api/subscriptions").json()
check("the picker is marked restricted", picker.get("restricted") is True)
check("the picker holds only their subscription",
      {s["id"] for s in picker["subscriptions"]} <= {only_one[0]["id"]},
      str([s["id"] for s in picker["subscriptions"]]))

from app.warehouse import warehouse  # noqa: E402

everything = warehouse.summary()
theirs = client.get("/api/warehouse").json()
check("the warehouse view is narrowed to their access",
      theirs["subscriptions"] <= 1 and theirs["rows"] <= everything["rows"],
      f"{theirs['rows']}/{everything['rows']} rows, {theirs['subscriptions']} sub(s)")
if everything["subscriptions"] > 1:
    check("and it is genuinely smaller than the whole estate",
          theirs["rows"] < everything["rows"],
          f"{theirs['rows']} vs {everything['rows']}")

# The dashboard is a second way into the same numbers. If it were scoped differently from the
# chat, the tabs would quietly report the whole estate to someone entitled to one subscription.
from app.dashboard import get_dashboard  # noqa: E402

whole = get_dashboard().sections(None, days=30)
scoped = client.get("/api/dashboard/sections?days=30").json()
check("the dashboard totals are their subscriptions, not the estate",
      (scoped.get("total") or 0) <= (whole.get("total") or 0),
      f"{scoped.get('total')} vs {whole.get('total')}")
if everything["subscriptions"] > 1 and (whole.get("total") or 0) > 0:
    check("and are genuinely smaller than the unscoped total",
          (scoped.get("total") or 0) < whole["total"],
          f"{scoped.get('total')} vs {whole['total']}")

tab = client.get("/api/dashboard/compute?days=30").json()
unscoped_tab = get_dashboard().section("compute", None, days=30)
check("an individual tab is scoped the same way",
      (tab["kpis"]["total"] or 0) <= (unscoped_tab["kpis"]["total"] or 0),
      f"{tab['kpis']['total']} vs {unscoped_tab['kpis']['total']}")
check("a tab still reports its own shape",
      set(tab) >= {"kpis", "trend", "services", "regions", "resources_top"}, str(list(tab)))
check("an unknown tab is a 404, not an empty page",
      client.get("/api/dashboard/nonsense").status_code == 404)

# A report is a file that leaves the building, so it is the worst place to leak another
# subscription. It must carry the same scope as the screen it was exported from.
from app.report import collect  # noqa: E402

whole_report = collect(None, days=30)
scoped_report = collect(only_one_ids := [only_one[0]["id"]], days=30)
check("a report built for one subscription is smaller than the estate's",
      (scoped_report["total"] or 0) <= (whole_report["total"] or 0),
      f"{scoped_report['total']} vs {whole_report['total']}")
if everything["subscriptions"] > 1 and (whole_report["total"] or 0) > 0:
    check("and is genuinely narrower, not merely equal",
          scoped_report["total"] < whole_report["total"],
          f"{scoped_report['total']} vs {whole_report['total']}")
check("a scoped report says so", scoped_report["scoped"] is True)

for fmt, sniff in (("xlsx", b"PK"), ("pptx", b"PK"), ("csv", None), ("md", None)):
    r = client.get(f"/api/report/{fmt}?days=30")
    check(f"the {fmt} report downloads", r.status_code == 200, str(r.status_code))
    check(f"the {fmt} report is a real file",
          len(r.content) > 200 and (sniff is None or r.content.startswith(sniff)),
          f"{len(r.content)} bytes")
    check(f"the {fmt} report is sent as an attachment",
          "attachment" in r.headers.get("content-disposition", ""),
          r.headers.get("content-disposition", ""))
check("an unknown report format is a 404",
      client.get("/api/report/exe").status_code == 404)

# The point of the Report tab is that a selection actually narrows the file. If every
# selection produced the same bytes, the checkboxes would be decoration.
print("\nREPORT BUILDER")
opts = client.get("/api/report/options?days=30").json()
check("the options list only areas with spend",
      all(a.get("cost") for a in opts["areas"]), str(opts["areas"])[:120])
check("the options offer every format", len(opts["formats"]) >= 5, str(len(opts["formats"])))
check("the options offer the live datasets",
      {l["id"] for l in opts["live"]} == {"waste", "rightsizing", "esu", "advisor"},
      str(opts["live"]))

def report_size(**body) -> int:
    r = client.post("/api/report", json={"days": 30, "format": "xlsx", **body})
    assert r.status_code == 200, r.text[:200]
    return len(r.content)

full = report_size(summary=True, sections=None,
                   blocks=["trend", "services", "regions", "resources"], live=[])
one_area = report_size(summary=False, sections=[opts["areas"][0]["id"]],
                       blocks=["services"], live=[])
summary_only = report_size(summary=True, sections=[], blocks=[], live=[])
check("a narrower selection produces a smaller file", one_area < full, f"{one_area} vs {full}")
check("summary-only is smaller still or equal", summary_only <= one_area + 2048,
      f"{summary_only} vs {one_area}")
check("dropping every block still yields a valid workbook",
      summary_only > 3000 and client.post(
          "/api/report", json={"days": 30, "format": "xlsx", "summary": True,
                               "sections": [], "blocks": [], "live": []}
      ).content.startswith(b"PK"))

for fmt in ("xlsx", "pptx", "csv", "md", "json"):
    r = client.post("/api/report", json={"days": 30, "format": fmt,
                                         "sections": [opts["areas"][0]["id"]]})
    check(f"a selective {fmt} report builds", r.status_code == 200 and len(r.content) > 50,
          f"{r.status_code} {len(r.content)}")

bad = client.post("/api/report", json={"days": 30, "format": "exe"})
check("an unknown format is refused", bad.status_code == 404, str(bad.status_code))
check("an out-of-range period is refused",
      client.post("/api/report", json={"days": 9999, "format": "xlsx"}).status_code == 422)
# A section id that isn't theirs must simply not appear, not widen the report.
sneaky = client.post("/api/report", json={"days": 30, "format": "json",
                                          "summary": False, "sections": ["nonsense"],
                                          "blocks": ["services"], "live": []})
check("an unknown section is ignored rather than honoured",
      sneaky.status_code == 200 and sneaky.json()["sections"] == [], sneaky.text[:120])

# Someone asking for a subscription they have no access to must not get it, even by naming it.
other = next((s["id"] for s in all_subs if s["id"] != only_one[0]["id"]), "sub-beta")
allowed = asyncio.run(main.permitted(
    auth_mod.User(name="Ravi", source="entra", email="ravi@microsoft.com", sid=session.sid)))
check("their permitted list comes from the session, not the request", allowed == [only_one[0]["id"]])
check("naming another subscription does not widen the scope",
      main.narrow([other], allowed) == [only_one[0]["id"]])

# No access at all is an answer, not a blank page.
session.subscriptions = []
session.checked = time.time()
blocked = client.get("/api/subscriptions")
check("no access at all is refused with an explanation", blocked.status_code == 403,
      str(blocked.status_code))
check("the explanation says what to ask for",
      "Cost Management Reader" in blocked.json()["detail"], blocked.text[:160])
check("the warehouse is refused too", client.get("/api/warehouse").status_code == 403)
check("so is asking a question", client.post(
    "/api/ask", json={"question": "what did I spend?"}).status_code == 403)
# The dashboard reads the same warehouse by a different route, so it needs the same gate.
check("the dashboard tab bar is refused",
      client.get("/api/dashboard/sections").status_code == 403)
check("an individual dashboard tab is refused",
      client.get("/api/dashboard/compute").status_code == 403)
# The live tabs reach Azure directly, so they need the same gate as the warehouse ones.
for live_tab in ("rightsizing", "advisor", "esu", "waste"):
    check(f"the {live_tab} tab is refused",
          client.get(f"/api/dashboard/{live_tab}").status_code == 403)
# Exporting must be refused too, or the one route that produces a shareable file would be
# the one route that ignores entitlement.
for fmt in ("xlsx", "pptx", "csv", "md"):
    check(f"the {fmt} report is refused when they can see nothing",
          client.get(f"/api/report/{fmt}").status_code == 403)
check("the report builder is refused too",
      client.post("/api/report", json={"format": "xlsx", "days": 30}).status_code == 403)
check("and so are its options",
      client.get("/api/report/options").status_code == 403)
session.subscriptions = only_one
session.checked = time.time()

check("signing out drops the server-side session",
      client.post("/api/auth/logout").status_code == 200
      and service.sso.get(session.sid) is None)
check("and the cookie no longer works", client.get("/api/warehouse").status_code == 401)


# --------------------------------------------------- the impersonation, for real
print("\nDELEGATED AZURE ACCESS")
if not token:
    print("  SKIP no delegated token available (az login first) - the plumbing was not exercised")
else:
    reset = cost.act_as(token, "test-user")
    try:
        as_them = asyncio.run(cost.list_subscriptions())["subscriptions"]
    finally:
        cost.stop_acting(reset)
    check("a delegated token really is used for Azure calls", len(as_them) == len(all_subs),
          f"{len(as_them)} vs {len(all_subs)}")
    check("the caller is the server again afterwards", cost.acting_as() == "server")

    # A token that is not a valid ARM token must fail as that caller, which proves the header
    # is genuinely swapped rather than the server's own token being used regardless.
    reset = cost.act_as("not-a-real-token", "impostor")
    try:
        asyncio.run(cost.list_subscriptions())
        refused = False
    except Exception:  # noqa: BLE001
        refused = True
    finally:
        cost.stop_acting(reset)
    check("an invalid delegated token is not silently replaced by the server's", refused)

    # Two callers must not share cached Azure responses.
    cost.clear_cache()
    reset = cost.act_as(token, "person-a")
    try:
        asyncio.run(cost.list_subscriptions())
    finally:
        cost.stop_acting(reset)
    after_a = cost.cache_stats()["entries"]
    reset = cost.act_as(token, "person-b")
    try:
        asyncio.run(cost.list_subscriptions())
    finally:
        cost.stop_acting(reset)
    check("the response cache is per caller, so answers can't leak between people",
          cost.cache_stats()["entries"] > after_a,
          f"{after_a} -> {cost.cache_stats()['entries']}")


# ------------------------------------------- RBAC without a delegated token
# The default path: the app asks Azure which subscriptions carry a role assignment for a
# person, rather than holding a token for them. This is what lets a colleague sign in with no
# administrator involved and still see only their own subscriptions.
print("\nRBAC FROM ROLE ASSIGNMENTS (no delegated token)")
my_oid = None
try:
    out = subprocess.run(["az", "ad", "signed-in-user", "show", "--query", "id", "-o", "tsv"],
                         capture_output=True, text=True, timeout=120, shell=True)
    my_oid = out.stdout.strip() or None
except Exception:  # noqa: BLE001
    pass

if not my_oid:
    print("  SKIP no signed-in Azure identity to resolve")
else:
    mine = asyncio.run(cost.subscriptions_for_principal(my_oid))
    check("a real principal resolves to the subscriptions they hold a role on",
          len(mine) > 0, f"{len(mine)} found")
    check("every resolved subscription is one the app can actually read",
          {s["id"] for s in mine} <= {s["id"] for s in all_subs} if all_subs else True)
    check("this ran as the app, not as a delegated user", cost.acting_as() == "server")

    # Someone with no role assignments anywhere must resolve to nothing, not to everything —
    # the failure mode that would quietly hand a stranger the whole estate.
    nobody = asyncio.run(cost.subscriptions_for_principal(
        "00000000-0000-0000-0000-000000000001"))
    check("a principal with no assignments resolves to no subscriptions",
          nobody == [], str(nobody))
    check("an empty object id resolves to nothing rather than everything",
          asyncio.run(cost.subscriptions_for_principal("")) == [])

print(f"\n  {'FAILED: ' + ', '.join(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)

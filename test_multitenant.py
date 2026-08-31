"""Prove that cross-tenant sign-in routes each call to the tenant that can authorise it.

An ARM token is issued by one directory and refused by every other. That makes the failure mode
here particularly nasty: a person with access in three tenants, given a single token, gets a
correct-looking answer for one of them and a 401 for the rest — which the UI cannot tell apart
from having no access at all. So the thing worth testing is not "does a token get attached" but
"does the *right* token get attached", and that a subscription in another directory is neither
silently dropped nor silently queried with credentials that cannot work.

No network: token acquisition and ARM are both stubbed, because what is under test is the
routing decision, not Entra's ability to issue tokens.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TMP = Path(tempfile.mkdtemp())
os.environ["AUTH_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["AUTH_CLIENT_ID"] = "00000000-1111-2222-3333-444444444444"
os.environ["AUTH_TENANT_ID"] = "organizations"
os.environ["AUTH_DELEGATED_ARM"] = "true"
os.environ["AUTH_REDIRECT_URI"] = "http://localhost:8100/auth/callback"
os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")
for leftover in ("AUTH_DISABLED", "AUTH_ALLOW_LOCAL", "AUTH_PASSWORD", "AUTH_USERS",
                 "AUTH_API_TOKENS", "AUTH_CLIENT_SECRET"):
    os.environ[leftover] = ""

from app import cost, entra  # noqa: E402

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


HOME = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
CLOSED = "33333333-3333-3333-3333-333333333333"

SUB_HOME = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB_OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
SUB_CLOSED = "cccccccc-cccc-cccc-cccc-cccccccccccc"

TENANTS = {
    HOME: ("Contoso", [{"id": SUB_HOME, "name": "Prod", "state": "Enabled"}]),
    OTHER: ("Fabrikam", [{"id": SUB_OTHER, "name": "Dev", "state": "Enabled"}]),
    CLOSED: ("Locked Down", [{"id": SUB_CLOSED, "name": "Secret", "state": "Enabled"}]),
}


print("\nthe token that gets attached is the one that can authorise the call")

caller = cost.Caller(
    fallback="home-token",
    by_subscription={SUB_HOME: "home-token", SUB_OTHER: "other-token"},
)

check("a call scoped to the home subscription uses the home token",
      caller.token_for(f"{cost.ARM}/subscriptions/{SUB_HOME}/providers/x") == "home-token")
check("a call scoped to another tenant's subscription uses that tenant's token",
      caller.token_for(f"{cost.ARM}/subscriptions/{SUB_OTHER}/providers/x") == "other-token",
      "this is the whole point: a home-tenant token would 401 here")
check("a tenant-wide call falls back to the home token",
      caller.token_for(f"{cost.ARM}/tenants") == "home-token")
check("an unknown subscription falls back rather than sending nothing",
      caller.token_for(f"{cost.ARM}/subscriptions/{SUB_CLOSED}/x") == "home-token")
check("subscription matching is case-insensitive",
      caller.token_for(f"{cost.ARM}/subscriptions/{SUB_OTHER.upper()}/x") == "other-token",
      "ARM echoes ids in mixed case; a case-sensitive map would silently misroute")


print("\na bare token still works, so single-tenant callers are unchanged")

single = cost.act_as("just-a-token", "someone")
try:
    got = cost._caller.get()
    check("a plain string is accepted", got is not None and got.fallback == "just-a-token")
    check("and answers for every URL",
          got.token_for(f"{cost.ARM}/subscriptions/{SUB_HOME}/x") == "just-a-token")
finally:
    cost.stop_acting(single)
check("the caller is cleared afterwards", cost._caller.get() is None)


print("\nthe tenant sweep keeps what it can reach and reports what it cannot")


class FakeSSO(entra.EntraSSO):
    """Real logic, stubbed Entra: token issuance is not what is under test here."""

    async def token(self, session):
        return "home-token"

    async def token_for_tenant(self, session, tenant):
        # CLOSED stands for a directory that will not consent to this app — a refusal, which is
        # an answer, not an error.
        return None if tenant == CLOSED else f"{tenant}-token"


async def fake_list_tenants(token):
    return [{"id": t, "name": name} for t, (name, _) in TENANTS.items()]


async def fake_subscriptions_with(token):
    for tenant, (_, subs) in TENANTS.items():
        if token in (f"{tenant}-token", "home-token" if tenant == HOME else None):
            return [{**s, "tenant": tenant} for s in subs]
    return []


cost.list_tenants = fake_list_tenants
cost.subscriptions_with = fake_subscriptions_with

sso = FakeSSO()
session = entra.Session(sid="s", name="Ravi", email="ravi@example.com", oid="oid",
                        tenant=HOME, app=None, account={})

subs = asyncio.run(sso.subscriptions(session))
ids = {s["id"] for s in subs}

check("subscriptions from the home tenant are present", SUB_HOME in ids)
check("subscriptions from another tenant are present too", SUB_OTHER in ids,
      "a single-tenant enumeration would have stopped at the home directory")
check("a tenant that refused contributes nothing", SUB_CLOSED not in ids)
check("both reachable tenants are recorded", len(session.tenants) == 2,
      str(session.tenants))
check("the refusing tenant is reported rather than dropped silently",
      len(session.skipped) == 1 and session.skipped[0]["id"] == CLOSED,
      str(session.skipped))
check("each subscription is labelled with its directory",
      all(s.get("tenant_name") for s in subs))

built = session.caller
check("a caller was built", built is not None)
check("the home subscription routes to the home token",
      built.by_subscription[SUB_HOME] == "home-token")
check("the other tenant's subscription routes to its own token",
      built.by_subscription[SUB_OTHER] == f"{OTHER}-token")
check("the refused tenant's subscription has no token at all",
      SUB_CLOSED not in built.by_subscription)


print("\nlist_subscriptions answers from the merged view, not one directory")

reset = cost.act_as(built, "ravi")
try:
    listed = asyncio.run(cost.list_subscriptions())
    check("both tenants' subscriptions are listed",
          {s["id"] for s in listed["subscriptions"]} == {SUB_HOME, SUB_OTHER},
          str(listed))
finally:
    cost.stop_acting(reset)


print("\nconsent policy is reported with a way out, not as a dead end")

os.environ["AUTH_TENANT_ID"] = "organizations"
os.environ["AUTH_DELEGATED_ARM"] = "true"
consent_sso = entra.EntraSSO()

real_microsoft_refusal = (
    "AADSTS90094: An administrator of Microsoft has set a policy that prevents you from "
    "granting CloudLens the permissions it is requesting. Contact an administrator of "
    "Microsoft who can grant permissions to this application on your behalf.")

check("the tenant-policy refusal is recognised",
      entra.needs_admin_consent(real_microsoft_refusal),
      "this is the exact string Entra returned for @microsoft.com")
check("a first-time consent prompt is recognised",
      entra.needs_admin_consent("AADSTS65001: The user or administrator has not consented"))
check("an unrelated failure is not mistaken for one",
      not entra.needs_admin_consent("AADSTS50126: Invalid username or password"))
check("an empty message is handled",
      not entra.needs_admin_consent(""))

url = consent_sso.consent_url()
check("the consent url is on the Entra authority", url.startswith(entra.AUTHORITY_HOST), url)
check("it is the adminconsent endpoint", "/adminconsent" in url, url)
check("it names this app", consent_sso.client_id in url, url)
check("it asks for the ARM scope it was refused",
      "management.azure.com" in url, url)
check("it works for any organisation rather than one tenant",
      "/organizations/" in url, url)

single_url = entra.EntraSSO.consent_url(
    type("S", (), {"multi_tenant": False, "tenant": HOME, "client_id": "cid",
                   "audience": HOME, "redirect_uri": "https://x/auth/callback"})())
check("a single-tenant deployment gets its own tenant's link",
      f"/{HOME}/" in single_url, single_url)


print("\nthe multi-tenant endpoints are for work accounts; the tenant route is for the rest")

# Verified against live Entra rather than assumed. Azure Resource Manager is an Entra-only
# resource, so a request carrying an ARM scope makes *both* /organizations and /common answer a
# personal account with "You can't sign in here with a personal account". The same account
# against a tenant authority federates to login.live.com and is issued an ordinary
# organisational ARM token -- confirmed by decoding one: idp=live.com, tid set to the
# organisation, and ARM accepted it.
#
# So personal accounts are not excluded because they lack Azure access. They frequently have it:
# the subscription this was first deployed against is owned solely by one. They need a different
# door, which is what AUTH_HOME_TENANT provides.
os.environ["AUTH_TENANT_ID"] = "common"
common_sso = entra.EntraSSO()
check("common is recognised as multi-tenant", common_sso.multi_tenant)
check("and sends people to the common authority",
      common_sso.authority.endswith("/common"), common_sso.authority)

os.environ["AUTH_TENANT_ID"] = "organizations"
orgs_sso = entra.EntraSSO()
check("organizations maps to its own endpoint",
      orgs_sso.authority.endswith("/organizations"), orgs_sso.authority)

os.environ["AUTH_TENANT_ID"] = "multi"
alias_sso = entra.EntraSSO()
check("the 'multi' alias means organizations",
      alias_sso.authority.endswith("/organizations"), alias_sso.authority)

os.environ["AUTH_TENANT_ID"] = "any"
any_sso = entra.EntraSSO()
check("the 'any' alias means common",
      any_sso.authority.endswith("/common"), any_sso.authority)

os.environ["AUTH_TENANT_ID"] = "organizations"
os.environ["AUTH_HOME_TENANT"] = "99999999-8888-7777-6666-555555555555"
home_sso = entra.EntraSSO()
check("a home tenant is picked up for the personal-account route",
      home_sso.home_tenant == "99999999-8888-7777-6666-555555555555")
check("its authority is that directory, not the multi-tenant endpoint",
      home_sso.authority_for(home_sso.home_tenant).endswith(home_sso.home_tenant),
      home_sso.authority_for(home_sso.home_tenant))
check("while the default authority stays multi-tenant",
      home_sso.authority.endswith("/organizations"), home_sso.authority)

os.environ["AUTH_HOME_TENANT"] = ""
bare = entra.EntraSSO()
check("nothing is offered when no directory is named", bare.home_tenant == "")


print("\nthe tenant parameter cannot be used to point sign-in anywhere")

# /auth/login takes a tenant so a personal account can reach a directory authority. Left
# unchecked that is an open redirect carrying our client id: an attacker sends someone to
# ?tenant=<their-tenant> and collects a sign-in against an app the victim recognises.
#
# A real directory, because MSAL fetches OIDC metadata from it and a made-up GUID does not
# resolve. This one is public and belongs to the deployment under test.
REAL_TENANT = "99999999-8888-7777-6666-555555555555"
os.environ["AUTH_TENANT_ID"] = "organizations"
os.environ["AUTH_HOME_TENANT"] = REAL_TENANT
os.environ["AUTH_ALLOW_LOCAL"] = ""

import importlib  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import auth as auth_mod  # noqa: E402
from app import main as main_mod  # noqa: E402

importlib.reload(auth_mod)
importlib.reload(main_mod)

client = TestClient(main_mod.app)

r = client.get(f"/auth/login?tenant={REAL_TENANT}", follow_redirects=False)
check("the configured directory is accepted", r.status_code == 303, str(r.status_code))
if r.status_code == 303:
    check("and sends the browser to that directory",
          REAL_TENANT in r.headers.get("location", ""), r.headers.get("location", "")[:120])

r = client.get("/auth/login?tenant=99999999-9999-9999-9999-999999999999",
               follow_redirects=False)
check("an unknown directory is refused", r.status_code == 400, str(r.status_code))

r = client.get("/auth/login?tenant=evil.example.com", follow_redirects=False)
check("so is anything that is not a directory at all", r.status_code == 400, str(r.status_code))

r = client.get("/auth/login", follow_redirects=False)
check("no tenant still works", r.status_code == 303, str(r.status_code))

os.environ["AUTH_HOME_TENANT"] = ""


print("\nthe configuration that cannot work is refused at startup")

os.environ["AUTH_TENANT_ID"] = "organizations"
os.environ["AUTH_DELEGATED_ARM"] = ""
try:
    entra.EntraSSO()
    check("multi-tenant without delegated ARM is refused", False,
          "it started, and would have shown every external user an empty dashboard")
except RuntimeError as exc:
    check("multi-tenant without delegated ARM is refused", True)
    check("and the message says what to set", "AUTH_DELEGATED_ARM" in str(exc), str(exc))
finally:
    os.environ["AUTH_DELEGATED_ARM"] = "true"

os.environ["AUTH_TENANT_ID"] = HOME
os.environ["AUTH_DELEGATED_ARM"] = ""
try:
    single_sso = entra.EntraSSO()
    check("a single-tenant app still starts without delegated ARM", True)
    check("and is not in multi-tenant mode", not single_sso.multi_tenant)
    check("and keeps its own authority", single_sso.authority.endswith(HOME),
          single_sso.authority)
except RuntimeError as exc:
    check("a single-tenant app still starts without delegated ARM", False, str(exc))


print()
if fails:
    print(f"  {len(fails)} check(s) failed")
    for f in fails:
        print(f"    - {f}")
else:
    print("  all checks passed")
sys.exit(1 if fails else 0)

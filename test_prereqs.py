"""Fixing a deployment's prerequisites with the signed-in admin's own access.

Two failures this exists for, both silent. An unregistered CostManagementExports provider makes
first-run setup fall back to the slow API path — the data still arrives, so nothing looks
broken. A managed identity missing its roles reads an empty estate, which looks like having no
cost rather than no permission.

The risk in fixing them automatically is the reason `apply` is separate and admin-only: a role
grant is standing access that outlives the session. So the tests below care most about two
things — that nothing is granted without being previewed first, and that one subscription
refusing never costs you the others.

    python test_prereqs.py
"""

from __future__ import annotations

import asyncio
import os

os.environ.setdefault("AUTH_DISABLED", "true")

from app import prereqs

passed = failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}  {detail}")
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def run(coro):
    return asyncio.run(coro)


READER = prereqs.ROLES["Reader"]
CMR = prereqs.ROLES["Cost Management Reader"]

calls: list[tuple[str, str]] = []


def fake_arm(*, roles_by_sub=None, providers_by_sub=None, can_grant_by_sub=None,
             grant_result=None):
    """Stand in for ARM. Records every call so we can assert what was and was not attempted."""
    roles_by_sub = roles_by_sub or {}
    providers_by_sub = providers_by_sub or {}
    can_grant_by_sub = can_grant_by_sub or {}
    calls.clear()

    async def request(method, url, *, body=None, params=None):
        calls.append((method, url))
        sub = url.split("/subscriptions/")[1].split("/")[0] if "/subscriptions/" in url else ""

        if "/Microsoft.Authorization/permissions" in url:
            allowed = can_grant_by_sub.get(sub, True)
            if allowed is None:
                raise RuntimeError("403 AuthorizationFailed reading permissions")
            return {"value": [{"actions": ["*"],
                               "notActions": [] if allowed else ["Microsoft.Authorization/*/Write"]}]}

        if "/roleAssignments" in url and method == "GET":
            held = roles_by_sub.get(sub, [])
            return {"value": [{"properties": {"roleDefinitionId": f"/x/y/{r}"}} for r in held]}

        if "/roleAssignments/" in url and method == "PUT":
            if grant_result:
                raise RuntimeError(grant_result)
            return {}

        if url.rstrip("/").endswith("/register"):
            return {}

        for ns in prereqs.PROVIDERS:
            if url.endswith(f"/providers/{ns}"):
                return {"registrationState": providers_by_sub.get(sub, {}).get(ns, "Registered")}

        raise AssertionError(f"unexpected call {method} {url}")

    prereqs._request = request


_real_request = prereqs._request
_real_principal = prereqs.principal_id


async def fake_principal():
    return "app-principal-oid"


prereqs.principal_id = fake_principal

try:
    # ------------------------------------------------------------------ check
    print("=" * 72)
    print("what a subscription still needs")

    fake_arm(roles_by_sub={"sub-ready": [READER, CMR]})
    r = run(prereqs.check(["sub-ready"], {"sub-ready": "Ready"}))
    one = r["subscriptions"][0]
    check("a fully configured subscription is ready", one["ready"] is True, str(one["roles_held"]))
    check("and is not offered a command it does not need", one["command"] is None)
    check("the counts summarise it", r["ready"] == 1 and r["fixable"] == 0 and r["blocked"] == 0)

    fake_arm(roles_by_sub={"sub-half": [READER]})
    one = run(prereqs.check(["sub-half"]))["subscriptions"][0]
    check("only the missing role is named", one["roles_missing"] == ["Cost Management Reader"],
          "Reader alone shows the estate and none of the money")

    fake_arm(roles_by_sub={"sub-none": []},
             providers_by_sub={"sub-none": {"Microsoft.CostManagementExports": "NotRegistered"}})
    one = run(prereqs.check(["sub-none"]))["subscriptions"][0]
    check("an unregistered provider is reported",
          one["providers_unregistered"] == ["Microsoft.CostManagementExports"],
          "the one that fails late and quietly")
    check("and the subscription is not called ready", one["ready"] is False)

    # ------------------------------------------------------- who may fix it
    print()
    print("=" * 72)
    print("whether this caller can fix it")

    fake_arm(roles_by_sub={"sub-x": []}, can_grant_by_sub={"sub-x": False})
    r = run(prereqs.check(["sub-x"]))
    one = r["subscriptions"][0]
    check("Contributor cannot grant, and is told so", one["can_grant"] is False,
          "Contributor carries * and excludes Authorization writes")
    check("it is counted as blocked rather than fixable",
          r["blocked"] == 1 and r["fixable"] == 0)
    check("and gets the exact command instead of 'ask an administrator'",
          "az role assignment create" in (one["command"] or "")
          and "app-principal-oid" in (one["command"] or "")
          and "sub-x" in (one["command"] or ""),
          "the administrator being asked still has to know what to type")

    fake_arm(roles_by_sub={"sub-y": []}, can_grant_by_sub={"sub-y": None})
    one = run(prereqs.check(["sub-y"]))["subscriptions"][0]
    check("an unanswerable permissions check is unknown, not a refusal",
          one["can_grant"] is None and one["command"] is None,
          "'we could not ask' and 'you may not' need different sentences")

    # ------------------------------------------------------------------ apply
    print()
    print("=" * 72)
    print("applying, and refusing to overreach")

    fake_arm(roles_by_sub={"sub-a": [READER, CMR]})
    r = run(prereqs.apply(["sub-a"]))
    check("a ready subscription is left completely alone",
          not any(m == "PUT" for m, _ in calls),
          "re-running must not rewrite assignments that already exist")
    check("and is not counted as changed", r["changed"] == 0 and r["already_ready"] == 1,
          "claiming to have granted what was already there is a wrong number, quietly")
    check("nor as failed", r["failed"] == 0)
    check("and it says so rather than warning about propagation",
          r["note"] == "Nothing needed changing.")

    fake_arm(roles_by_sub={"sub-b": []})
    r = run(prereqs.apply(["sub-b"]))
    one = r["subscriptions"][0]
    check("both missing roles are granted",
          one["roles"] == {"Reader": "granted", "Cost Management Reader": "granted"})
    check("and it says so plainly", one["ok"] is True and r["changed"] == 1)
    check("the caller is warned that RBAC is not instant",
          "few minutes" in (r["note"] or ""),
          "a refresh run immediately after reads the estate as still empty")

    fake_arm(roles_by_sub={"sub-c": []}, grant_result="RoleAssignmentExists")
    one = run(prereqs.apply(["sub-c"]))["subscriptions"][0]
    check("an existing assignment is success, not failure",
          one["roles"]["Reader"] == "already granted" and one["ok"] is True,
          "409 is the desired state arriving by another route")

    fake_arm(roles_by_sub={"sub-d": []}, grant_result="AuthorizationFailed")
    one = run(prereqs.apply(["sub-d"]))["subscriptions"][0]
    check("a refusal is reported as a refusal", one["roles"]["Reader"].startswith("refused"))
    check("and carries the command to run instead",
          "az role assignment create" in (one["command"] or ""))
    check("the subscription is not claimed as done", one["ok"] is False)

    # --------------------------------------------------- a mixed estate
    print()
    print("=" * 72)
    print("one subscription refusing does not cost you the others")

    seen: list[str] = []

    async def mixed(method, url, *, body=None, params=None):
        sub = url.split("/subscriptions/")[1].split("/")[0]
        if "/Microsoft.Authorization/permissions" in url:
            return {"value": [{"actions": ["*"], "notActions": []}]}
        if "/roleAssignments" in url and method == "GET":
            return {"value": []}
        if "/roleAssignments/" in url and method == "PUT":
            if sub == "owned":
                seen.append(sub)
                return {}
            raise RuntimeError("AuthorizationFailed")
        for ns in prereqs.PROVIDERS:
            if url.endswith(f"/providers/{ns}"):
                return {"registrationState": "Registered"}
        raise AssertionError(url)

    prereqs._request = mixed
    r = run(prereqs.apply(["owned", "readonly"]))
    by_id = {s["id"]: s for s in r["subscriptions"]}
    check("the one they own is fixed", by_id["owned"]["ok"] is True)
    check("the one they cannot is named, not silently skipped",
          by_id["readonly"]["ok"] is False and by_id["readonly"]["command"])
    check("and the totals report both", r["changed"] == 1 and r["failed"] == 1,
          "a mixed estate is the normal case, not an error")

    # ------------------------------------------------- no identity at all
    print()
    print("=" * 72)
    print("a deployment with no identity cannot grant itself anything")

    async def no_principal():
        return None

    prereqs.principal_id = no_principal
    fake_arm(roles_by_sub={"sub-z": []})
    r = run(prereqs.check(["sub-z"]))
    check("check says so rather than reporting every role as missing",
          "cannot determine its own identity" in (r["note"] or ""))

    raised = ""
    try:
        run(prereqs.apply(["sub-z"]))
    except prereqs.PrereqError as exc:
        raised = str(exc)
    check("and apply refuses outright", "cannot determine its own identity" in raised,
          "granting to an unknown principal is worse than not granting")
    prereqs.principal_id = fake_principal

    # ------------------------------------------------------- claim parsing
    print()
    print("=" * 72)
    print("reading our own object id")

    import base64
    import json as _json

    payload = base64.urlsafe_b64encode(_json.dumps({"oid": "abc-123"}).encode()).decode().rstrip("=")
    check("the oid claim is read from an unpadded token",
          prereqs._claims(f"header.{payload}.sig").get("oid") == "abc-123",
          "base64url in JWTs drops padding; naive decoding raises")
    check("a malformed token is an unknown identity, not a crash",
          prereqs._claims("not-a-jwt") == {})

    # --------------------------------------------------- action matching
    print()
    print("=" * 72)
    print("who may create a role assignment")

    check("Owner's bare * matches", prereqs._matches("*", prereqs.ROLE_WRITE))
    check("Azure matches case-insensitively",
          prereqs._matches("MICROSOFT.AUTHORIZATION/*/WRITE", prereqs.ROLE_WRITE))
    check("a wildcard spans a slash",
          prereqs._matches("Microsoft.Authorization/*", prereqs.ROLE_WRITE))
    check("an unrelated action does not match",
          not prereqs._matches("Microsoft.Compute/*", prereqs.ROLE_WRITE))

finally:
    prereqs._request = _real_request
    prereqs.principal_id = _real_principal

print()
print("=" * 72)
print(f"  {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)

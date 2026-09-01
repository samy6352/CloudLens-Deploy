"""What a deployment still needs, and doing it with the signed-in admin's own access.

Two things go wrong after a deployment and neither announces itself.

`Microsoft.CostManagementExports` is not needed to deploy anything, so nothing registers it —
and then first-run setup asks Azure for a daily export, gets "RP Not Registered", and falls
back to the slower API path. The data still arrives. Nothing looks broken. The deployment is
just permanently slower for a reason nobody would think to look for.

The managed identity's roles are worse. The Bicep grants Reader and Cost Management Reader on
the subscription it deploys into, and that is the only one it can reach; every other
subscription in the estate has to be granted by hand. Without them the app reads an empty
estate and says so, which looks like having no cost rather than having no permission.

The person hitting both is usually the person who could fix both — they hold Owner on the
subscriptions, and the app is holding their delegated ARM token. So it can do the work instead
of printing instructions and hoping.

**Never automatically.** Granting the app's identity a role is standing access that outlives
the session, and in a `--no-sso` deployment `permitted()` returns None for local accounts, so
widening the identity widens what every local user can see. That is a decision somebody makes,
not a side effect of signing in. `check` reports; `apply` acts, and only an admin may call it.

Everything runs on the caller's token, so Azure enforces their RBAC rather than ours: a
subscription they cannot grant on refuses them, and the refusal is reported next to the exact
command an administrator can run instead.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Any

log = logging.getLogger("cloudlens.prereqs")

ARM = "https://management.azure.com"
ROLE_API = "2022-04-01"
PROVIDER_API = "2021-04-01"

# The two the app reads cost with. Reader alone shows the estate and no money; Cost Management
# Reader alone shows money with nothing to attach it to.
ROLES = {
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "Cost Management Reader": "72fafb9e-0641-4937-9268-a91bfd8191a3",
}

# CostManagementExports is the one that fails late and quietly, and the reason this module
# bothers with providers at all. The other two are cheap to confirm while we are here.
PROVIDERS = ("Microsoft.CostManagement", "Microsoft.CostManagementExports")

# What creating a role assignment actually needs. Owner has it; Contributor carries `*` and
# excludes this in notActions, which is the whole Owner-versus-Contributor distinction.
ROLE_WRITE = "Microsoft.Authorization/roleAssignments/write"


class PrereqError(Exception):
    """Something the caller should read, not a stack trace."""


async def _request(method: str, url: str, *, body: dict | None = None,
                   params: dict | None = None) -> dict:
    from . import cost

    return await cost.azure.request(method, url, json_body=body, params=params, cache=False)


def _claims(token: str) -> dict[str, Any]:
    """The payload of a JWT, without verifying it.

    Deliberately unverified: this reads a token we just acquired for ourselves, from the
    credential in this process, to learn our own object id. There is no attacker in that path —
    the alternative is an ARM round trip to look up a principal we already are.
    """
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:  # noqa: BLE001 - a malformed token is simply an unknown identity
        return {}


async def principal_id() -> str | None:
    """The object id of the identity this app runs as, or None if it cannot be determined.

    Read from the `oid` claim of a token the app acquires for itself. The obvious alternative —
    `az webapp identity show` against our own site — needs the site's resource id, which needs
    the subscription, which is exactly the thing that may be unreadable when this is asked.

    The token must be the *app's*, not the caller's, so this deliberately steps outside any
    delegated context first. Asked while acting as a signed-in person, the naive version returns
    that person's object id and the app then grants roles to the wrong principal.
    """
    from . import cost

    override = os.getenv("CLOUDLENS_PRINCIPAL_ID", "").strip()
    if override:
        return override

    reset = None
    caller = cost._caller.get()
    if caller is not None:
        reset = cost._caller.set(None)
    try:
        token = await cost.azure._bearer()
    except Exception as exc:  # noqa: BLE001 - no identity is an answer, not a crash
        log.info("prereqs: could not acquire the app's own token: %s", str(exc)[:200])
        return None
    finally:
        if reset is not None:
            cost._caller.reset(reset)

    return _claims(token).get("oid") or None


def _matches(pattern: str, action: str) -> bool:
    """Azure's own matching: case-insensitive, and `*` spans `/`."""
    import re

    return re.fullmatch(re.escape(pattern).replace(r"\*", ".*"), action, re.IGNORECASE) is not None


async def can_grant(subscription_id: str) -> bool | None:
    """Whether the caller may create a role assignment here. None when it cannot be determined.

    None rather than False on failure, because "we could not ask" and "you may not" lead to
    different sentences — one is a blocker to report, the other a check to skip.
    """
    try:
        payload = await _request(
            "GET",
            f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Authorization/permissions",
            params={"api-version": ROLE_API})
    except Exception as exc:  # noqa: BLE001
        log.info("prereqs: could not read permissions on %s: %s", subscription_id, str(exc)[:160])
        return None

    for entry in payload.get("value", []):
        granted = any(_matches(p, ROLE_WRITE) for p in entry.get("actions") or [])
        if not granted:
            continue
        withheld = any(_matches(p, ROLE_WRITE) for p in entry.get("notActions") or [])
        if not withheld:
            return True
    return False


async def _held_roles(subscription_id: str, principal: str) -> set[str]:
    """Which of ROLES the principal already holds at or above this subscription.

    Filtered server-side to the principal, and `atScope()` is deliberately *not* used: a grant
    made at a management group is inherited here and is just as good, so asking only for
    assignments written at this exact scope would report a working estate as unconfigured.
    """
    try:
        payload = await _request(
            "GET",
            f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Authorization/roleAssignments",
            params={"api-version": ROLE_API, "$filter": f"principalId eq '{principal}'"})
    except Exception as exc:  # noqa: BLE001
        log.info("prereqs: could not list roles on %s: %s", subscription_id, str(exc)[:160])
        return set()

    ids = {(a.get("properties") or {}).get("roleDefinitionId", "").rsplit("/", 1)[-1].lower()
           for a in payload.get("value", [])}
    return {name for name, rid in ROLES.items() if rid.lower() in ids}


async def _provider_states(subscription_id: str) -> dict[str, str]:
    async def one(ns: str) -> tuple[str, str]:
        try:
            payload = await _request("GET", f"{ARM}/subscriptions/{subscription_id}/providers/{ns}",
                                     params={"api-version": PROVIDER_API})
            return ns, payload.get("registrationState") or "unknown"
        except Exception as exc:  # noqa: BLE001
            log.info("prereqs: could not read provider %s: %s", ns, str(exc)[:160])
            return ns, "unknown"

    return dict(await asyncio.gather(*(one(ns) for ns in PROVIDERS)))


def grant_command(subscription_id: str, principal: str | None, missing: list[str]) -> str:
    """The exact command for whatever this run could not do itself.

    Printed for refusals rather than "ask an administrator", because the administrator being
    asked still has to work out what to type, and every step of that is knowable here.
    """
    who = principal or "<principal-id>"
    roles = " ".join(f'"{r}"' for r in missing) or '"Reader" "Cost Management Reader"'
    return (f"for ROLE in {roles}; do\n"
            f"  az role assignment create --assignee-object-id {who} \\\n"
            f"    --assignee-principal-type ServicePrincipal \\\n"
            f'    --role "$ROLE" --scope "/subscriptions/{subscription_id}"\n'
            f"done")


async def check(subscription_ids: list[str], names: dict[str, str] | None = None) -> dict[str, Any]:
    """What each subscription still needs, and whether this caller can supply it.

    Reported per subscription and never as a single verdict: an estate where three of five are
    ready is normal, and collapsing that to "not ready" hides both what works and what to ask
    for.
    """
    principal = await principal_id()
    names = names or {}

    async def one(sub: str) -> dict[str, Any]:
        roles, providers, may_grant = await asyncio.gather(
            _held_roles(sub, principal) if principal else _none_set(),
            _provider_states(sub),
            can_grant(sub),
        )
        missing_roles = [r for r in ROLES if r not in roles]
        unregistered = [ns for ns, state in providers.items() if state != "Registered"]
        return {
            "id": sub,
            "name": names.get(sub) or sub,
            "roles_held": sorted(roles),
            "roles_missing": missing_roles,
            "providers_unregistered": unregistered,
            "can_grant": may_grant,
            "ready": not missing_roles and not unregistered,
            # Only worth showing where it is both needed and refused. Offering a command for
            # work the app is about to do itself is noise.
            "command": (grant_command(sub, principal, missing_roles)
                        if missing_roles and may_grant is False else None),
        }

    results = await asyncio.gather(*(one(s) for s in subscription_ids))
    actionable = [r for r in results if not r["ready"] and r["can_grant"] is not False]
    blocked = [r for r in results if not r["ready"] and r["can_grant"] is False]
    return {
        "principal_id": principal,
        "subscriptions": list(results),
        "ready": sum(1 for r in results if r["ready"]),
        "fixable": len(actionable),
        "blocked": len(blocked),
        "roles": list(ROLES),
        "note": None if principal else (
            "This deployment cannot determine its own identity, so roles cannot be checked or "
            "granted. It usually means the app is running without a managed identity."),
    }


async def _none_set() -> set[str]:
    return set()


async def _register(subscription_id: str, ns: str) -> str:
    try:
        await _request("POST", f"{ARM}/subscriptions/{subscription_id}/providers/{ns}/register",
                       params={"api-version": PROVIDER_API})
        return "registering"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {str(exc)[:160]}"


async def _grant(subscription_id: str, principal: str, role: str) -> str:
    import uuid

    url = (f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
           f"roleAssignments/{uuid.uuid4()}")
    body = {"properties": {
        "roleDefinitionId": (f"/subscriptions/{subscription_id}/providers/"
                             f"Microsoft.Authorization/roleDefinitions/{ROLES[role]}"),
        "principalId": principal,
        # Named explicitly. Without it Azure looks the principal up in Entra, and a
        # freshly-created identity that has not replicated yet is rejected as non-existent.
        "principalType": "ServicePrincipal",
    }}
    try:
        await _request("PUT", url, body=body, params={"api-version": ROLE_API})
        return "granted"
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        # An existing assignment is the desired state, not a failure. Re-running must be safe.
        if "RoleAssignmentExists" in text or "409" in text:
            return "already granted"
        if "AuthorizationFailed" in text or "403" in text:
            return "refused: you cannot create role assignments here"
        return f"failed: {text[:160]}"


async def apply(subscription_ids: list[str], names: dict[str, str] | None = None) -> dict[str, Any]:
    """Do what this caller's access allows, and report the rest.

    One subscription refusing must not stop the others: an estate is commonly a mix of ones the
    signed-in person owns and ones they merely read, and the useful outcome is the owned ones
    fixed and the rest named.

    Re-running is safe by construction — an existing role assignment comes back 409, which is
    reported as already granted, and registering a registered provider is a no-op.
    """
    principal = await principal_id()
    if not principal:
        raise PrereqError(
            "This deployment cannot determine its own identity, so it cannot grant itself "
            "anything. It usually means the app is running without a managed identity.")

    names = names or {}

    async def one(sub: str) -> dict[str, Any]:
        providers, roles = await asyncio.gather(
            _provider_states(sub), _held_roles(sub, principal))

        registered = {}
        for ns, state in providers.items():
            if state != "Registered":
                registered[ns] = await _register(sub, ns)

        granted = {}
        for role in ROLES:
            if role not in roles:
                granted[role] = await _grant(sub, role=role, principal=principal)

        refused = [r for r, outcome in granted.items() if outcome.startswith("refused")]
        return {
            "id": sub,
            "name": names.get(sub) or sub,
            "providers": registered or None,
            "roles": granted or None,
            "ok": not refused and not any(v.startswith("failed") for v in
                                          list(granted.values()) + list(registered.values())),
            "command": grant_command(sub, principal, refused) if refused else None,
        }

    results = await asyncio.gather(*(one(s) for s in subscription_ids))
    # "Changed" has to mean changed. Counting every subscription that ended up in a good state
    # reports work that was never done — a re-run over a healthy estate would claim to have
    # granted things, which is the same confident wrong number this module exists to avoid.
    changed = [r for r in results if r["ok"] and (r["providers"] or r["roles"])]
    already = [r for r in results if r["ok"] and not (r["providers"] or r["roles"])]
    failed = [r for r in results if not r["ok"]]
    return {
        "principal_id": principal,
        "subscriptions": list(results),
        "changed": len(changed),
        "already_ready": len(already),
        "failed": len(failed),
        # Role assignments are eventually consistent, and a refresh started the instant this
        # returns will read the estate as still empty and look like the fix did nothing. Only
        # worth saying when something actually moved.
        "note": ("Role assignments take a few minutes to take effect. If a refresh still shows "
                 "nothing, wait and try again before changing anything else."
                 if changed else "Nothing needed changing."),
    }

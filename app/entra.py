"""Microsoft Entra sign-in, and the per-user Azure token that makes RBAC real.

Two things happen here, and the second is the point of the first.

**Sign-in.** Authorization code flow with PKCE against Entra, via MSAL — the same library
`azure-identity` already depends on, so this costs no new package. People sign in with the work
account they already have; the app never sees a password and has none to store.

**Impersonation.** The same flow asks for a delegated Azure Resource Manager token
(`user_impersonation`). That token *is* the person's own ARM access, so "which subscriptions can
you see" stops being a question this app answers and becomes one Azure answers. Someone with
Reader on one subscription gets one subscription — in the picker, in the tiles, in the warehouse
queries and in the live calls — because ARM tells us so, not because we filtered a list we
already had.

Design notes:

- **Sessions are server-side here, unlike the local-password mode.** They have to be: they hold
  refresh tokens, which must never go near a browser. The cookie is an opaque signed id. A
  restart therefore drops sessions — but re-signing in is a silent redirect through Entra, not
  a prompt, so it costs the user nothing visible.
- **MSAL is synchronous** and does network I/O on refresh, so every call into it is pushed to a
  worker thread rather than blocking the event loop.
- **The subscription list is cached per user** for a few minutes. It is one ARM call, but it
  happens on every request that needs scope, and a role change does not need to be visible
  within seconds.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

log = logging.getLogger("cloudlens.entra")

AUTHORITY_HOST = os.getenv("AUTH_AUTHORITY_HOST", "https://login.microsoftonline.com")

# Delegated ARM access. Asking for this makes the app call Azure *as* the signed-in person,
# which is the strongest possible enforcement — but it is not a permission a user can consent
# to in a tenant that restricts consent to low-impact scopes, so it is opt-in. Without it the
# app signs people in with the basic OIDC scopes, which users can consent to themselves, and
# works out what each person may see by asking Azure for their role assignments.
ARM_SCOPE = "https://management.azure.com/user_impersonation"

# `organizations` accepts a work or school account from any directory. `common` nominally also
# accepts personal Microsoft accounts -- but not here, and the reason is worth stating because it
# is not obvious: Azure Resource Manager is an Entra-only resource, so asking for an ARM scope
# makes Entra restrict *both* endpoints to work accounts. A personal account typing its own
# address gets "You can't sign in here with a personal account" from either one.
#
# That matters because personal accounts routinely own exactly the subscriptions this app is
# pointed at -- Visual Studio and pay-as-you-go especially. They reach Azure as a guest member of
# a real directory, and a *tenant* authority does admit them: it federates the sign-in to
# login.live.com and issues an ordinary organisational ARM token (verified: idp=live.com,
# tid set to the organisation).
#
# So the multi-tenant endpoints are for work accounts, and anyone whose Azure access comes
# through a personal account signs in against their tenant. `authority_for` exists for exactly
# this, and AUTH_HOME_TENANT names a directory to offer them.
MULTI_TENANT = {"organizations", "common", "multi", "any"}

# Which of the two to actually send. Anything not recognised is passed through untouched, so a
# tenant GUID still means one directory.
AUDIENCE = {"multi": "organizations", "any": "common"}

SESSION_HOURS = float(os.getenv("AUTH_SESSION_HOURS", "12"))
SUBSCRIPTION_TTL = float(os.getenv("AUTH_RBAC_TTL_SECONDS", "300"))
FLOW_TTL = 600.0  # a half-finished sign-in is worth ten minutes, not forever


@dataclass
class Session:
    """One signed-in person, and the tokens that let us act as them."""

    sid: str
    name: str
    email: str
    oid: str
    tenant: str
    app: Any                      # msal.PublicClientApplication bound to this user's cache
    account: dict
    cache: Any = None             # the shared token cache, so sibling tenants reuse the refresh token
    created: float = field(default_factory=time.time)
    seen: float = field(default_factory=time.time)

    # One MSAL app per directory, built lazily. Keeping them avoids re-fetching OIDC metadata
    # on every refresh, which for someone in six tenants is six needless round trips a time.
    apps: dict = field(default_factory=dict)
    # Their Azure access: a token per tenant, and which subscription each one is good for.
    caller: Any = None

    # Their subscriptions, from ARM, with the time we asked.
    subscriptions: list[dict] | None = None
    checked: float = 0.0
    # Set when ARM refuses: shown to the user instead of an empty screen with no explanation.
    problem: str = ""
    # Which tenants were reachable, and which refused. A person with access in four directories
    # who sees three should be told about the fourth rather than left to wonder.
    tenants: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    @property
    def expired(self) -> bool:
        return time.time() > self.created + SESSION_HOURS * 3600

    def public(self) -> dict:
        return {"name": self.name, "email": self.email}


class EntraSSO:
    """Configured by four environment variables; inert without them.

    `AUTH_CLIENT_ID` and `AUTH_TENANT_ID` are what switch the app from local passwords to SSO.
    A client secret is optional: with one this is a confidential client, without one it is a
    public client using PKCE, which is what a tenant that caps secret lifetimes wants anyway.
    """

    def __init__(self) -> None:
        self.client_id = os.getenv("AUTH_CLIENT_ID", "").strip()
        self.tenant = os.getenv("AUTH_TENANT_ID", "").strip()
        self.secret = os.getenv("AUTH_CLIENT_SECRET", "").strip() or None
        self.redirect_uri = os.getenv(
            "AUTH_REDIRECT_URI", "http://localhost:8100/auth/callback"
        ).strip()
        # Who may run an ingest. Everyone else can read and ask, which is the common case.
        self.admins = {
            a.strip().lower()
            for a in os.getenv("AUTH_ADMINS", "").replace("\n", ",").split(",")
            if a.strip()
        }

        self.enabled = bool(self.client_id and self.tenant)
        # Off by default: `user_impersonation` on Azure Resource Manager needs an administrator
        # to approve the app in most tenants, and requiring that would mean nobody can sign in
        # at all until someone with a directory role gets involved. Turn it on where consent is
        # available — it upgrades enforcement from "we filter" to "Azure refuses".
        self.delegated = os.getenv("AUTH_DELEGATED_ARM", "").lower() in ("1", "true", "yes")

        # Anyone with a work account, from any directory. The alternative — a tenant GUID — is
        # still the default, and is what an app serving one organisation should keep.
        self.multi_tenant = self.tenant.lower() in MULTI_TENANT

        # A directory to offer people whose Azure access comes through a personal Microsoft
        # account. They cannot use the multi-tenant endpoints for an ARM scope (see above), but
        # a tenant authority federates them to login.live.com and works. Optional: without it
        # the app simply says why they were turned away rather than pretending it cannot happen.
        self.home_tenant = os.getenv("AUTH_HOME_TENANT", "").strip()

        # Hide the multi-tenant work-account button, leaving only the named directory.
        #
        # For a deployment whose users all sign in through one directory, the general button is
        # worse than useless: it sends everyone else into their own tenant's consent policy and
        # ends at "ask your administrator", which reads as the app being broken rather than as a
        # door that was never open to them. The route still exists for anyone holding the URL —
        # this governs what is advertised, not what is enforced. Entitlement stays with Azure.
        self.hide_work_button = os.getenv(
            "AUTH_HIDE_WORK_SIGNIN", "").lower() in ("1", "true", "yes")

        if self.multi_tenant and self.enabled and not self.delegated:
            # The non-delegated path asks ARM, as this app, who holds a role assignment. The
            # app's identity exists in one directory and cannot read role assignments in any
            # other, so it would return nothing for every external user while looking perfectly
            # healthy. Refuse the combination rather than ship that.
            raise RuntimeError(
                "AUTH_TENANT_ID=organizations requires AUTH_DELEGATED_ARM=true. Resolving access "
                "as the app only works inside its own tenant, so without a delegated token every "
                "external user would sign in successfully and see nothing."
            )

        self._sessions: dict[str, Session] = {}
        self._pending: dict[str, dict] = {}
        self._login_app: Any = None

        if self.enabled:
            log.info("Entra SSO enabled: tenant=%s client=%s admins=%d delegated_arm=%s "
                     "multi_tenant=%s",
                     self.tenant, self.client_id, len(self.admins), self.delegated,
                     self.multi_tenant)

    @property
    def scopes(self) -> list[str]:
        """Empty means sign-in only: MSAL still requests openid/profile/offline_access, which
        are the scopes a user is normally allowed to consent to without an administrator."""
        return [ARM_SCOPE] if self.delegated else []

    @property
    def authority(self) -> str:
        # `common` and `organizations` are real authority paths, not placeholders: Entra
        # resolves them to whichever directory the person signing in actually belongs to.
        return f"{AUTHORITY_HOST}/{self.audience}"

    @property
    def audience(self) -> str:
        tenant = self.tenant.lower()
        if tenant in MULTI_TENANT:
            return AUDIENCE.get(tenant, tenant)
        return self.tenant

    def authority_for(self, tenant: str) -> str:
        """The authority for one specific directory, used to swap a token into that tenant."""
        return f"{AUTHORITY_HOST}/{tenant}"

    def _build(self, cache: Any = None, tenant: str = "") -> Any:
        import msal

        authority = self.authority_for(tenant) if tenant else self.authority
        kwargs = {"authority": authority, "token_cache": cache}
        if self.secret:
            return msal.ConfidentialClientApplication(
                self.client_id, client_credential=self.secret, **kwargs)
        return msal.PublicClientApplication(self.client_id, **kwargs)

    def _app_for_login(self) -> Any:
        """One shared app for starting flows, so OIDC metadata is fetched once, not per click."""
        if self._login_app is None:
            self._login_app = self._build()
        return self._login_app

    def is_admin(self, email: str, oid: str = "") -> bool:
        return bool(self.admins & {email.lower(), oid.lower()})

    def consent_url(self, tenant: str = "") -> str:
        """The link an administrator opens once to approve this app for their whole directory.

        This is the supported answer to 'user consent is disabled here', which is the default in
        any security-conscious tenant and is not something an application can or should route
        around. One admin clicks it once, and afterwards everyone in that directory signs in
        normally — including guests, whose access is still their own.

        `organizations` as the tenant lets the admin's own sign-in select the directory, so the
        same link works for every organisation rather than needing one per tenant.
        """
        target = tenant or (self.audience if self.multi_tenant else self.tenant)
        return (f"{AUTHORITY_HOST}/{target}/v2.0/adminconsent"
                f"?client_id={self.client_id}"
                f"&scope={quote(ARM_SCOPE + ' offline_access openid profile', safe='')}"
                f"&redirect_uri={quote(self.redirect_uri, safe='')}")

    # ----------------------------------------------------------------- sign-in
    async def start(self, next_url: str = "/", tenant: str = "") -> str:
        """Begin a sign-in and return the Entra URL to send the browser to.

        MSAL generates the PKCE verifier, state and nonce; we keep the flow server-side and
        hand the browser nothing but an opaque state, so none of it is forgeable from outside.

        `tenant` overrides the authority for this one sign-in, which is how a personal-account
        holder gets in: the multi-tenant endpoints refuse them for an ARM scope, a tenant
        authority does not.
        """
        def _initiate() -> dict:
            app = self._build(tenant=tenant) if tenant else self._app_for_login()
            return app.initiate_auth_code_flow(
                scopes=self.scopes,
                redirect_uri=self.redirect_uri,
                # Ask Entra to name the account in the picker rather than silently reusing the
                # last one, which on a shared machine signs you in as your colleague.
                prompt="select_account",
            )

        flow = await asyncio.to_thread(_initiate)
        self._sweep()
        self._pending[flow["state"]] = {"flow": flow, "next": next_url, "at": time.time(),
                                        "tenant": tenant}
        return flow["auth_uri"]

    async def complete(self, params: dict) -> tuple[Session | None, str, str]:
        """Finish a sign-in from the callback query string. Returns (session, next_url, error)."""
        import msal

        state = params.get("state", "")
        pending = self._pending.pop(state, None)
        if pending is None:
            # Also the case for a replayed callback, which is exactly what this should refuse.
            return None, "/", "That sign-in link has expired. Please try again."

        cache = msal.SerializableTokenCache()
        # Same authority the flow was started on, or MSAL refuses the redemption. For a
        # personal-account sign-in that is the tenant, not the multi-tenant endpoint.
        app = self._build(cache, tenant=pending.get("tenant", ""))

        def _redeem() -> dict:
            # MSAL checks state and the id_token's nonce here; a mismatch raises rather than
            # returning a token, which is the CSRF protection for the callback.
            return app.acquire_token_by_auth_code_flow(pending["flow"], params,
                                                       scopes=self.scopes)

        try:
            result = await asyncio.to_thread(_redeem)
        except ValueError as exc:  # MSAL's own state/nonce validation failure
            log.warning("auth code flow rejected: %s", exc)
            return None, pending["next"], "Sign-in could not be verified. Please try again."

        # Sign-in only needs to establish who they are; a delegated deployment additionally
        # needs the Azure token, so the success condition differs between the two.
        claims = result.get("id_token_claims") or {}
        ok = "access_token" in result if self.delegated else bool(claims)
        if not ok:
            detail = result.get("error_description") or result.get("error") or "unknown error"
            log.warning("token exchange failed: %s", str(detail)[:400])
            return None, pending["next"], _friendly(result)

        accounts = app.get_accounts()
        session = Session(
            sid=secrets.token_urlsafe(32),
            name=claims.get("name") or claims.get("preferred_username") or "signed in",
            email=(claims.get("preferred_username") or claims.get("email") or "").lower(),
            oid=claims.get("oid", ""),
            tenant=claims.get("tid", self.tenant),
            app=app,
            account=accounts[0] if accounts else {},
            cache=cache,
        )
        self._sessions[session.sid] = session
        log.info("%s signed in via Entra (oid=%s, tenant=%s)",
                 session.email or session.name, session.oid, session.tenant)
        return session, pending["next"], ""

    # ---------------------------------------------------------------- sessions
    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expired:
            self._sessions.pop(sid, None)
            return None
        session.seen = time.time()
        return session

    def drop(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def _sweep(self) -> None:
        now = time.time()
        for state, pending in list(self._pending.items()):
            if now - pending["at"] > FLOW_TTL:
                self._pending.pop(state, None)
        for sid, session in list(self._sessions.items()):
            if session.expired:
                self._sessions.pop(sid, None)

    def count(self) -> int:
        self._sweep()
        return len(self._sessions)

    # ------------------------------------------------------------------ tokens
    async def token(self, session: Session) -> str | None:
        """A current ARM access token for this person, refreshed silently if needed.

        Only meaningful in delegated mode; without it there is no user token to hold, and calls
        run as the app's own identity restricted to the subscriptions they are entitled to.
        """
        if not self.delegated:
            return None

        def _silent() -> dict | None:
            return session.app.acquire_token_silent(self.scopes, account=session.account or None)

        result = await asyncio.to_thread(_silent)
        if result and "access_token" in result:
            return result["access_token"]
        # The refresh token is gone or revoked; the next request will bounce them to sign in.
        log.info("no ARM token for %s; session needs re-authentication", session.email)
        return None

    async def token_for_tenant(self, session: Session, tenant: str) -> str | None:
        """An ARM token for one specific directory, or None if that tenant will not issue one.

        The refresh token in the cache is not bound to a tenant, so an app pointed at another
        directory's authority can redeem the same one there — this is what makes a single
        sign-in work across every tenant the person belongs to, without sending them back to
        Entra once per directory.

        None is an ordinary outcome, not a failure: it means this person cannot use the app in
        that tenant (no consent there, or a Conditional Access policy said no), which is Azure's
        answer and is to be reported rather than worked around.
        """
        def _acquire() -> dict | None:
            app = session.apps.get(tenant)
            if app is None:
                app = self._build(session.cache, tenant=tenant)
                session.apps[tenant] = app
            accounts = app.get_accounts()
            account = accounts[0] if accounts else (session.account or None)
            return app.acquire_token_silent([ARM_SCOPE], account=account)

        try:
            result = await asyncio.to_thread(_acquire)
        except Exception as exc:  # noqa: BLE001 - one bad tenant must not end the sign-in
            log.info("no token for %s in tenant %s: %s", session.email, tenant, str(exc)[:160])
            return None
        if result and "access_token" in result:
            return result["access_token"]
        if result:
            log.info("tenant %s refused a token for %s: %s",
                     tenant, session.email, str(result.get("error_description"))[:160])
        return None

    # -------------------------------------------------------------------- RBAC
    async def caller(self, session: Session, force: bool = False) -> Any:
        """This person's Azure access, as something the cost layer can call Azure with.

        None in non-delegated mode: there is no user token to act with, and entitlement is
        enforced by scoping to the subscriptions their role assignments name.
        """
        await self._refresh(session, force=force)
        return session.caller

    async def subscriptions(self, session: Session, force: bool = False) -> list[dict]:
        """The subscriptions *this person* can see, decided by Azure either way.

        Delegated: ask ARM with their own token — in every tenant they belong to — so the list
        is literally what Azure shows them. Otherwise: ask ARM, as this app, which subscriptions
        carry a role assignment for their object id, which is transitive so group-granted access
        counts. Different mechanism, same authority: neither is a list of names in a config file.
        """
        await self._refresh(session, force=force)
        return session.subscriptions or []

    async def _refresh(self, session: Session, force: bool = False) -> None:
        fresh = (session.subscriptions is not None
                 and time.time() - session.checked < SUBSCRIPTION_TTL)
        if fresh and not force:
            return

        from . import cost

        try:
            if not self.delegated:
                found = await cost.subscriptions_for_principal(session.oid)
                session.subscriptions = [{"id": s["id"], "name": s.get("name")} for s in found]
                session.caller = None
                session.problem = ""
                return

            home = await self.token(session)
            if home is None:
                session.problem = "Your Azure session expired. Sign in again."
                return

            if self.multi_tenant:
                await self._refresh_across_tenants(session, home)
            else:
                # One directory, so one token answers for everything in it.
                subs = await cost.subscriptions_with(home)
                session.caller = cost.Caller(fallback=home, subscriptions=subs)
                session.subscriptions = [{"id": s["id"], "name": s.get("name")} for s in subs]
            session.problem = ""
        except Exception as exc:  # noqa: BLE001 - an ARM failure must not 500 the whole page
            log.warning("could not list subscriptions for %s: %s", session.email, str(exc)[:200])
            session.problem = f"Azure did not return your subscriptions: {str(exc)[:160]}"
            if session.subscriptions is None:
                session.subscriptions = []
        finally:
            session.checked = time.time()

    async def _refresh_across_tenants(self, session: Session, home: str) -> None:
        """Enumerate every directory this person can reach, and hold a token for each.

        Done concurrently: a consultant in a dozen customer tenants would otherwise wait through
        a dozen sequential round trips on the first page load after signing in.
        """
        from . import cost

        tenants = await cost.list_tenants(home)
        if not tenants:
            tenants = [{"id": session.tenant, "name": session.tenant}]

        async def one(entry: dict) -> tuple[dict, str | None, list[dict]]:
            token = (home if entry["id"].lower() == session.tenant.lower()
                     else await self.token_for_tenant(session, entry["id"]))
            if token is None:
                return entry, None, []
            try:
                return entry, token, await cost.subscriptions_with(token)
            except Exception as exc:  # noqa: BLE001 - one refusal must not hide the rest
                log.info("tenant %s listed no subscriptions for %s: %s",
                         entry["id"], session.email, str(exc)[:160])
                return entry, token, []

        results = await asyncio.gather(*(one(t) for t in tenants))

        by_subscription: dict[str, str] = {}
        merged: list[dict] = []
        reachable: list[dict] = []
        skipped: list[dict] = []

        for entry, token, subs in results:
            if token is None:
                skipped.append({"id": entry["id"], "name": entry["name"],
                                "reason": "no access granted to this app in that directory"})
                continue
            reachable.append({"id": entry["id"], "name": entry["name"], "subscriptions": len(subs)})
            for sub in subs:
                key = sub["id"].lower()
                if key in by_subscription:
                    continue
                by_subscription[key] = token
                merged.append({"id": sub["id"], "name": sub.get("name"),
                               "tenant": entry["id"], "tenant_name": entry["name"]})

        session.caller = cost.Caller(fallback=home, by_subscription=by_subscription,
                                     subscriptions=merged)
        session.subscriptions = merged
        session.tenants = reachable
        session.skipped = skipped
        log.info("%s: %d subscription(s) across %d tenant(s), %d skipped",
                 session.email, len(merged), len(reachable), len(skipped))


# Entra's way of saying "an administrator has to approve this app for your directory". They
# differ in who is refusing and why, but the remedy is identical, and every one of them is a
# dead end for the person signing in unless they are handed the admin-consent link.
CONSENT_CODES = (
    "AADSTS90094",   # tenant policy: users may not consent to apps
    "AADSTS65001",   # nobody has consented for this user or tenant yet
    "AADSTS900941",  # admin consent required for the requested permission
    "AADSTS900971",  # no reply address / consent flow interrupted for a first-party admin path
)


def needs_admin_consent(text: str) -> bool:
    lowered = (text or "").lower()
    return (any(code in text for code in CONSENT_CODES)
            or "admin approval" in lowered
            or "administrator" in lowered and "consent" in lowered)


def _friendly(result: dict) -> str:
    """Turn the failures people actually hit into something they can act on."""
    error = (result.get("error") or "") + " " + (result.get("error_description") or "")
    if "AADSTS65004" in error or "consent" in error.lower():
        return ("You declined the permission request, or an administrator must grant it. The app "
                "needs to read your Azure subscriptions on your behalf.")
    if "AADSTS50020" in error or "AADSTS50034" in error:
        return ("That account has no access to this Azure tenant. Ask for guest access to the "
                "tenant the subscriptions live in, then try again.")
    if "AADSTS700016" in error or "AADSTS700009" in error:
        return "The app registration is misconfigured. Check AUTH_CLIENT_ID and AUTH_TENANT_ID."
    # A "Web" redirect URI is a confidential-client registration, and Entra will not redeem a
    # code for one without a secret however correct everything else is. Naming the setting is
    # the difference between a five-minute fix and an afternoon spent re-checking ids.
    if "AADSTS7000218" in error or "client_assertion" in error or "client_secret" in error:
        return ("This app is registered with a Web redirect URI, which Entra will only redeem "
                "with a client secret. Set AUTH_CLIENT_SECRET, or register the redirect URI as "
                "a public client instead.")
    # Multi-tenant only: the registration is single-tenant, so Entra will not admit anyone from
    # another directory. The fix is one setting on the app registration, and without naming it
    # this reads as the user's account being at fault when it is not.
    if "AADSTS50194" in error or "AADSTS700051" in error or "not configured as a multi-tenant" in error:
        return ("This app is registered for a single directory, so accounts from other "
                "organisations cannot sign in. Set the registration's supported account types to "
                "'Accounts in any organizational directory'.")
    if "AADSTS90072" in error:
        return ("Your account is from another directory and has not been added to this one. "
                "Ask for guest access to the directory that owns the subscriptions.")
    return (f"Sign-in failed: {(result.get('error_description') or 'unknown error')[:200]}")


# ------------------------------------------------------------------- cookie id
def sign(value: str, secret: bytes) -> str:
    mac = hmac.new(secret, value.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{value}.{mac}"


def unsign(token: str | None, secret: bytes) -> str | None:
    if not token or "." not in token:
        return None
    value, _, mac = token.rpartition(".")
    return value if hmac.compare_digest(sign(value, secret), f"{value}.{mac}") else None

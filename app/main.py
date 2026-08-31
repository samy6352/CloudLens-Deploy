"""CloudLens — a small web app that answers cost questions about your subscriptions."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

# Load .env before anything reads os.environ, so a local checkout only needs the file.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:  # pragma: no cover - env vars can always be exported instead
    pass

from .agent import CostAgent
from .auth import COOKIE, Auth, User
from .entra import needs_admin_consent as entra_needs_consent
from .entra import sign as entra_sign

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("cloudlens")

WEB = Path(__file__).resolve().parents[1] / "web"
_agent: CostAgent | None = None
_auth: Auth | None = None

# The header strip is expensive to compute (a dozen-plus Cost Management calls across every
# subscription, each of which can be throttled). Computing it per request made the page take two
# minutes to become useful. Instead it is refreshed on a timer and served from this snapshot, so
# the endpoint always answers instantly.
_overview: dict = {"status": "loading"}
_refresh_seconds = float(os.getenv("OVERVIEW_REFRESH_SECONDS", "600"))
_ingest_task: asyncio.Task | None = None


async def _refresh_overview() -> None:
    from . import cost

    while True:
        try:
            snapshot = await cost.overview()
            snapshot["status"] = "ok"
            globals()["_overview"] = snapshot
            log.info("overview refreshed: MTD=%s last=%s",
                     snapshot.get("month_to_date"), snapshot.get("last_month"))
        except Exception as exc:  # noqa: BLE001 - a failed refresh must not kill the loop
            log.warning("overview refresh failed: %s", exc)
            if _overview.get("status") == "loading":
                globals()["_overview"] = {"status": "error", "detail": str(exc)[:200]}
        await asyncio.sleep(_refresh_seconds)


async def _daily_refresh() -> None:
    """Reload the warehouse once a day, so nobody waits for it.

    Every route to fresh cost data is slow in its own way: a detail report is ten minutes of
    Azure building it, and the Query API is rate-limited hard enough that a busy day still
    takes minutes. Neither is a thing to do while someone watches. But none of it needs
    watching — the data changes once a day, overnight, when Azure writes the exports.

    So this runs on its own, at an hour after those exports land, and the person who opens the
    dashboard in the morning finds the work already done. It prefers the export path for the
    same reason the menu now lists it first: it is a blob read, so it is both the fastest and
    the only one that touches no quota. If there is no readable export it does nothing rather
    than falling back to the slow path — a background job that silently spends the rate limit
    everyone shares is worse than one that skips a day and says so in the log.
    """
    global _ingest_task
    from . import archive
    from .warehouse import warehouse

    hour = max(0, min(int(os.getenv("AUTO_REFRESH_HOUR", "6")), 23))
    if os.getenv("AUTO_REFRESH", "true").lower() == "false":
        log.info("daily refresh disabled by AUTO_REFRESH")
        return

    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())

        try:
            # Never fight a refresh someone is watching: theirs is the one with a person
            # attached to it, and two ingests would race for the same periods.
            if _ingest_task and not _ingest_task.done():
                log.info("daily refresh skipped: an ingest is already running")
                continue

            already = str(warehouse.summary().get("to") or "")
            today = datetime.now(timezone.utc).date().isoformat()
            if already >= today:
                log.info("daily refresh skipped: warehouse already holds %s", already)
                continue

            from .exports import discover_exports, ingest_export, reachable

            log.info("daily refresh starting")
            sas = os.getenv("COST_EXPORT_SAS_URL", "").strip()
            if sas:
                _ingest_task = asyncio.create_task(ingest_export(sas_url=sas, max_files=60))
                await _ingest_task
                await _archive_after_ingest("FocusCost", source="daily refresh")
            else:
                # It used to call ingest_export() with no arguments, which cannot work: with no
                # account, container or SAS the reader raises before it reads anything, so every
                # night would have failed with "Provide either sas_url, or both account and
                # container" into the log and nowhere else. Discover the same way the button
                # does, and load the same complementary pair.
                found = await discover_exports()
                usable = [e for e in found["exports"] if e.get("container")]
                ok, _bad = await reachable(usable) if usable else ([], [])
                if not ok:
                    log.info("daily refresh skipped: no readable export")
                    continue
                _ingest_task = asyncio.create_task(_load_export_pair(ok, max_files=60))
                result = await _ingest_task
                log.info("daily refresh loaded %s", result.get("loaded") or "nothing")
            log.info("daily refresh finished: %s, data to %s",
                     warehouse.state.get("status"), warehouse.summary().get("to"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad night must not stop tomorrow's run
            log.warning("daily refresh failed: %s", str(exc)[:300])


async def _load_export_pair(candidates: list[dict], sas_url: str | None = None,
                            max_files: int = 60) -> dict[str, Any]:
    """Load one export covering the current period and one covering the closed months.

    Two groups, not one ranked list, because a MonthToDate export and a TheLastMonth export
    answer different questions and neither substitutes for the other. Loading only the first
    that worked meant a FOCUS refresh rewrote June and July and left August untouched — on an
    estate where August is the month anyone is actually looking at.

    Each ingest replaces only the date range it covers, so the two are complementary.
    """
    from .exports import covers_current_period, ingest_export
    from .warehouse import warehouse

    problems: list[tuple[str, str]] = []
    loaded: list[str] = []

    async def first_that_works(group: list[dict]) -> bool:
        for candidate in group:
            try:
                await ingest_export(
                    account=candidate["storage_account"],
                    container=candidate["container"],
                    prefix=candidate["root_folder"] or "",
                    sas_url=sas_url,
                    subscription_id=candidate["subscription_id"],
                    max_files=max_files,
                )
            except Exception as exc:  # noqa: BLE001 - try the next export in this group
                log.warning("export ingest failed for %s: %s",
                            candidate["name"], str(exc)[:200])
                problems.append((candidate["storage_account"], str(exc)))
                continue

            # Recorded the moment the rows are in, before anything optional runs after it.
            # `ingest_export` marks the run ready as its last act and the archive below takes
            # seconds, so a stamp placed after the archive leaves a window where a poll sees a
            # finished refresh with no record of what it loaded — which is exactly what was
            # observed: exports_loaded came back null on a run that had loaded two.
            loaded.append(candidate["name"])
            warehouse.state = {**warehouse.state, "exports_loaded": list(loaded)}

            # Archiving is a follow-up, not part of the load. It was inside the same try, so a
            # failure writing the parquet snapshot would have discarded a perfectly good
            # refresh and sent the caller on to the next export to do it all again.
            try:
                await _archive_after_ingest(
                    candidate.get("type") or "FocusCost",
                    source=f"{candidate['name']} ({candidate['storage_account']})",
                )
            except Exception as exc:  # noqa: BLE001 - the data is already loaded
                log.warning("archive after %s failed: %s", candidate["name"], str(exc)[:200])
            return True
        return False

    current = [c for c in candidates if covers_current_period(c)]
    closed = [c for c in candidates if not covers_current_period(c)]

    # Current period first: if only one of the two can be read, the month in progress is the one
    # worth having, because it is the only one still changing.
    got_current = await first_that_works(current)
    got_closed = await first_that_works(closed)

    if got_current or got_closed:
        state = dict(warehouse.state)
        state["exports_loaded"] = loaded
        if not got_current:
            state["detail"] = (
                "Loaded closed-month data only — nothing covering the current month could be "
                "read, so today's figures are unchanged."
                if current else
                "Loaded closed-month data only. No export covers the current month, so today's "
                "figures come from the last full refresh.")
        warehouse.state = state
    return {"loaded": loaded, "problems": problems,
            "current": got_current, "closed": got_closed}


async def _run_ingest(months: int, metric: str = "AmortizedCost", quick: bool = False) -> None:
    """Populate the local warehouse. Slow (minutes), so always a background job.

    Prefers a configured export over ARM. Where COST_EXPORT_SAS_URL is set, the operator has
    said that blob storage is this deployment's route to cost data — usually because the app's
    identity holds no role on the subscription, which makes the ARM path not merely slower but
    guaranteed to fail. Trying ARM first there means every startup logs an authorization error
    and leaves the warehouse empty while a readable export sits untouched.
    """
    from . import cost
    from .exports import ingest_export
    from .warehouse import warehouse

    sas = os.getenv("COST_EXPORT_SAS_URL", "").strip()
    if sas:
        try:
            log.info("ingest starting from configured export")
            await ingest_export(sas_url=sas, max_files=int(os.getenv("EXPORT_MAX_FILES", "60")))
            log.info("export ingest complete: %s", warehouse.state.get("status"))
            await _archive_after_ingest(metric, source="configured export")
        except Exception as exc:  # noqa: BLE001
            log.warning("export ingest failed: %s", str(exc)[:300])
            warehouse.state = {"status": "failed", "detail": str(exc)[:300],
                               **warehouse.summary()}
        return

    try:
        subs = (await cost.list_subscriptions())["subscriptions"]
        log.info("ingest starting for %d subscription(s), %d month(s), metric=%s, quick=%s",
                 len(subs), months, metric, quick)
        if quick:
            await warehouse.quick_ingest(subs, months=months, metric=metric)
        else:
            await warehouse.ingest(subs, months=months, metric=metric)
        log.info("ingest complete: %s", warehouse.state.get("status"))
        await _archive_after_ingest(
            metric, source=f"Cost {'Query' if quick else 'Details'} API ({metric})")
    except Exception as exc:  # noqa: BLE001
        log.exception("ingest failed")
        warehouse.state = {"status": "failed", "detail": str(exc)[:300]}


async def _archive_after_ingest(metric: str, source: str | None = None) -> None:
    """Write the day's copy of whatever was just loaded.

    Never allowed to fail the refresh. The refresh has already succeeded by this point — the
    warehouse holds new data and the dashboard will show it — so turning an unreachable storage
    account into a failed refresh would report a false problem and hide a real success. The
    outcome is recorded on `warehouse.state` instead, which is what the UI reads.
    """
    from . import archive
    from .warehouse import warehouse

    if warehouse.state.get("status") not in ("ready", "partial"):
        return
    if not archive.enabled():
        return

    try:
        written = await archive.archive_current(metric, source=source)
        warehouse.state = {**warehouse.state, "archive": written}
        log.info("archive written: %s (%s bytes)", written["name"], written["bytes"])
    except Exception as exc:  # noqa: BLE001 - a refresh that worked must not report failure
        log.warning("archive failed: %s", str(exc)[:300])
        warehouse.state = {**warehouse.state,
                           "archive": {"error": str(exc)[:300],
                                       "name": archive.blob_name(metric)}}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _agent, _ingest_task
    if not os.getenv("PROJECT_ENDPOINT"):
        raise RuntimeError("PROJECT_ENDPOINT is not set. See README.md.")
    auth_service()  # bootstraps the first account and logs its password before anything serves
    _agent = CostAgent()

    from .warehouse import warehouse

    task = asyncio.create_task(_refresh_overview())
    # The warehouse reloads itself overnight, so the morning's first visitor finds it done
    # rather than waiting ten minutes for a report Azure has to build on request.
    daily = asyncio.create_task(_daily_refresh())

    # Auto-ingest on first run so the warehouse is useful without a manual step.
    if warehouse.summary()["rows"] == 0 and os.getenv("AUTO_INGEST", "true").lower() != "false":
        _ingest_task = asyncio.create_task(_run_ingest(int(os.getenv("INGEST_MONTHS", "3"))))

    log.info("cost agent ready, model=%s", _agent.model)
    # Warm the exchange rates so the first person to switch currency does not wait on a fetch.
    # Fire-and-forget: a rate feed being slow must never delay the app coming up.
    from . import currency

    asyncio.create_task(currency.refresh())
    yield
    task.cancel()
    daily.cancel()
    if _ingest_task:
        _ingest_task.cancel()
    await _agent.close()


app = FastAPI(title="CloudLens", version="1.0.0", lifespan=lifespan)

# SSE is already chunked and must not be buffered, so exclude it by minimum size rather than
# compressing everything: JSON and the HTML/CSS/JS bundle benefit, the event stream is unaffected
# because each frame is far below the threshold.
app.add_middleware(GZipMiddleware, minimum_size=1024)


def auth_service() -> Auth:
    """One shared Auth, built on first use so importing this module has no side effects."""
    global _auth
    if _auth is None:
        _auth = Auth()
    return _auth


# Signing in, the login page itself and the static assets it needs. Everything else — including
# the app shell — requires a session, so an unauthenticated visitor learns nothing about the
# estate, not even how many subscriptions there are.
PUBLIC_PATHS = {"/login", "/healthz", "/api/auth/login", "/api/auth/logout", "/api/auth/me",
                # Admin consent has to be reachable by someone who cannot sign in yet — that is
                # the entire situation it exists to resolve.
                "/auth/login", "/auth/callback", "/auth/admin-consent"}
PUBLIC_PREFIXES = ("/assets/",)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def safe_next(target: str | None) -> str:
    """Sanitise a `next=` value into a path on this site, or fall back to the app root.

    A login page that will redirect anywhere is a phishing tool: the link looks like ours, the
    landing page is not. `//evil.example.com` and `/\\evil.example.com` are the ones that catch
    people out — both start with a slash, and both are read by browsers as *another origin*.
    API and callback paths are excluded too, so a redirect can never dump someone on raw JSON.
    """
    if not target or not target.startswith("/"):
        return "/"
    if target.startswith(("//", "/\\")):
        return "/"
    if target.startswith(("/api/", "/auth/", "/login")):
        return "/"
    return target


class RequireAuth(BaseHTTPMiddleware):
    """Gate every request that isn't explicitly public.

    A browser navigation is redirected to the login page (with `next`, so the person lands back
    where they were aiming); anything else gets a 401 with a JSON body, which is what fetch and
    curl can actually act on.
    """

    async def dispatch(self, request: Request, call_next):
        auth = auth_service()
        path = request.url.path
        request.state.user = auth.identify(
            request.cookies.get(COOKIE), request.headers.get("authorization")
        )

        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if request.state.user is None:
            if _wants_html(request):
                target = request.url.path
                if request.url.query:
                    target = f"{target}?{request.url.query}"
                return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
            return JSONResponse({"detail": "Sign in to continue.", "auth": "required"},
                                status_code=401)

        return await call_next(request)


app.add_middleware(RequireAuth)


class CacheHeaders(BaseHTTPMiddleware):
    """Long-lived caching for vendored libraries, revalidation for our own assets.

    The vendor bundle is version-pinned by content, so it can be cached hard. app.js/app.css
    change with every deploy, so they use no-cache (revalidate, but reuse when unchanged) —
    that keeps a repeat visit to one 304 rather than a full download.

    It also carries the handful of security headers that apply site-wide. Framing is denied
    outright: nothing here is meant to be embedded, and a login form that can be framed is a
    login form that can be overlaid.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/assets/vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.startswith("/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        elif path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            # The app shell and the login page are behind a sign-in, so they must not sit in a
            # shared cache or come back from history after someone signs out.
            response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
        return response


app.add_middleware(CacheHeaders)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[dict] = Field(default_factory=list)
    scope: list[str] = Field(default_factory=list)


def agent() -> CostAgent:
    if _agent is None:
        raise HTTPException(503, "Agent not ready")
    return _agent


def current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:  # the middleware should have stopped this already
        raise HTTPException(401, "Sign in to continue.")
    return user


def require_admin(request: Request) -> User:
    """Ingests rewrite shared data and hammer the Azure APIs, so they are admin-only."""
    user = current_user(request)
    if not user.admin:
        raise HTTPException(403, "Only an admin can start an ingest.")
    return user


# --------------------------------------------------------------- who sees what
class NoAccess(HTTPException):
    """Signed in, but Azure says they can see nothing. Not an error — an answer."""

    def __init__(self, detail: str) -> None:
        super().__init__(status_code=403, detail=detail)


async def permitted(user: User) -> list[str] | None:
    """The subscriptions this person may see, or None for 'whatever the server can see'.

    For an Entra sign-in the list comes from ARM using their *own* delegated token, so it is
    their RBAC — not a copy of it that we maintain and get wrong.
    """
    if user.source != "entra":
        return None

    auth = auth_service()
    session = auth.sso.get(user.sid)
    if session is None:
        raise HTTPException(401, "Your session expired. Sign in again.")

    subs = await auth.sso.subscriptions(session)
    if not subs:
        raise NoAccess(session.problem or _no_access_detail(session, auth))
    return [s["id"] for s in subs]


def _no_access_detail(session, auth) -> str:
    """Say which of the two dead ends this is, because the fix differs.

    'No subscriptions' after checking eight directories is a different problem from 'no
    subscriptions' after being turned away at all eight, and someone told the wrong one will go
    asking the wrong people for the wrong thing.
    """
    if auth.sso.multi_tenant and session.skipped and not session.tenants:
        names = ", ".join(t["name"] for t in session.skipped[:4])
        return (f"None of your Azure directories ({names}) would grant this app access on your "
                "behalf. An administrator in the directory that owns the subscriptions has to "
                "approve it once, after which sign-in works for everyone there.")
    if auth.sso.multi_tenant:
        checked = len(session.tenants) or 1
        return (f"Your account has no access to any Azure subscription in the {checked} "
                "directory(ies) it can reach. Ask a subscription owner for the Cost Management "
                "Reader role.")
    return ("Your account has no access to any Azure subscription in this tenant. "
            "Ask a subscription owner for the Cost Management Reader role.")


def narrow(picked: list[str], allowed: list[str] | None) -> list[str]:
    """Intersect what they asked for with what they may have.

    Anything they pick that they aren't entitled to is dropped rather than refused — the picker
    is a convenience, the entitlement is the boundary. Picking nothing means everything they
    are entitled to, which for an unrestricted caller is the empty list the rest of the app
    already understands as 'all'.
    """
    if allowed is None:
        return [p for p in picked if p.strip()]
    chosen = [p for p in picked if p in set(allowed)]
    return chosen or list(allowed)


async def offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call on a worker thread instead of on the event loop.

    Every warehouse-backed endpoint is `async def`, but DuckDB is synchronous — so the query ran
    *on the event loop* and nothing else could be served until it finished. The symptom was
    subtle: eight parallel requests took 2.0s against 2.6s sequential, which looks like slow
    endpoints rather than a queue. Individually none of them exceeded 800ms.

    `asyncio.to_thread` releases the loop for the duration. DuckDB is safe here because each
    call opens and closes its own connection.
    """
    return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))


@asynccontextmanager
async def as_user(user: User) -> AsyncIterator[None]:
    """Run live Azure calls inside this block as the signed-in person, where that is possible.

    With delegated ARM access the call carries their own token — the one issued by the tenant
    that owns whichever subscription is being read — so Azure itself refuses anything they
    aren't entitled to. Without it — the common case, because that permission usually needs an
    administrator to approve — the call runs as the app's identity and the entitlement is
    enforced by the subscription scope worked out from their role assignments.
    """
    from . import cost

    reset = None
    if user.source == "entra":
        auth = auth_service()
        if auth.sso.delegated:
            session = auth.sso.get(user.sid)
            caller = await auth.sso.caller(session) if session else None
            if caller is None:
                raise HTTPException(401, "Your Azure session expired. Sign in again.")
            reset = cost.act_as(caller, session.oid or session.sid)
    try:
        yield
    finally:
        if reset is not None:
            cost.stop_acting(reset)


class Credentials(BaseModel):
    # Deliberately no minimum length: a blank username should be answered by the normal
    # "wrong username or password" — and counted by the throttle — rather than by a validation
    # error, which both looks different and tells an attacker their input never reached the
    # credential check.
    username: str = Field(max_length=120)
    password: str = Field(max_length=256)


@app.get("/login", include_in_schema=False)
async def login_page(request: Request, next: str = "/") -> Response:
    if getattr(request.state, "user", None) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return FileResponse(WEB / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/auth/login", include_in_schema=False)
async def sso_start(request: Request, next: str = "/", tenant: str = "") -> Response:
    """Hand the browser to Entra. Public: this *is* the way in.

    `tenant` picks a specific directory's authority, which is the only route in for someone
    whose Azure access comes through a personal Microsoft account.
    """
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)

    # Only a directory this deployment already names. Otherwise the sign-in link becomes an
    # open redirect into any tenant an attacker likes, with our client id on it.
    chosen = ""
    if tenant:
        allowed = {auth.sso.home_tenant.lower(), auth.sso.tenant.lower()} - {""}
        if tenant.lower() not in allowed:
            raise HTTPException(400, "Unknown directory.")
        chosen = tenant

    return RedirectResponse(await auth.sso.start(safe_next(next), tenant=chosen),
                            status_code=303)


def _explain_denied(code: str, detail: str) -> str:
    """'access_denied' is an OAuth code, not a sentence. Put something readable on the page."""
    if code == "access_denied" and detail == code:
        return ("Sign-in was not completed. This usually means your organisation requires an "
                "administrator to approve CloudLens before anyone there can use it.")
    return detail


def _consent_redirect(auth, detail: str) -> str:
    """Send someone blocked by consent policy to the login page *with the way out attached*.

    Without this the flow ends on 'ask your administrator' with nothing to forward, and the
    person has to work out for themselves which app, which permission and which URL.

    Only a flag travels in the query string, not the URL. The page turns it into a link to
    /auth/admin-consent, so the destination is always one this server built — a login page that
    renders whatever URL it is handed is a ready-made phishing page.
    """
    return "/login?error=" + quote(detail[:300], safe="") + "&consent=1"


@app.get("/auth/admin-consent", include_in_schema=False)
async def admin_consent(request: Request) -> Response:
    """Start the tenant-wide consent flow, for an administrator to approve once."""
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse(auth.sso.consent_url(), status_code=303)


@app.get("/auth/callback", include_in_schema=False)
async def sso_callback(request: Request) -> Response:
    """Where Entra sends them back with an authorization code."""
    auth = auth_service()
    if not auth.sso.enabled:
        return RedirectResponse("/login", status_code=303)

    params = dict(request.query_params)

    # An admin coming back from the consent screen. Entra returns them here with no code, so
    # the normal exchange would fail with something unhelpful about a missing authorization.
    if params.get("admin_consent"):
        if params.get("error"):
            detail = params.get("error_description") or params["error"]
            return RedirectResponse(f"/login?error={quote(detail[:300], safe='')}",
                                    status_code=303)
        return RedirectResponse(
            "/login?notice=" + quote(
                "Consent granted for your organisation. Everyone there can sign in now.",
                safe=""),
            status_code=303)

    if params.get("error"):
        # The user declined consent, or the tenant refused. Say so on the login page rather
        # than showing a raw Microsoft error screen.
        detail = params.get("error_description") or params["error"]
        code = params.get("error", "")

        # Entra shows its own "Need admin approval" page and keeps the AADSTS90094 detail
        # there: all that reaches us is a bare access_denied. So the specific reason is not
        # available to match on, and the two causes -- consent policy, or the person clicking
        # cancel -- are indistinguishable from here. Offer the link in both cases: it is the
        # remedy for the first and harmless noise for the second, whereas withholding it
        # leaves the first with nowhere to go.
        if entra_needs_consent(detail) or (code == "access_denied" and auth.sso.multi_tenant):
            return RedirectResponse(_consent_redirect(auth, _explain_denied(code, detail)),
                                    status_code=303)
        return RedirectResponse(f"/login?error={quote(detail[:300], safe='')}", status_code=303)

    session, target, problem = await auth.sso.complete(params)
    if session is None:
        if entra_needs_consent(problem):
            return RedirectResponse(_consent_redirect(auth, problem), status_code=303)
        return RedirectResponse(f"/login?error={quote(problem, safe='')}", status_code=303)

    response = RedirectResponse(safe_next(target), status_code=303)
    response.set_cookie(
        COOKIE,
        entra_sign(session.sid, auth.sessions.secret),
        max_age=int(float(os.getenv("AUTH_SESSION_HOURS", "12")) * 3600),
        **auth.cookie_kwargs(request.url.scheme == "https"),
    )
    return response


@app.post("/api/auth/login")
def login(body: Credentials, request: Request) -> JSONResponse:
    """Deliberately a sync endpoint: FastAPI runs it in a worker thread, so the ~200 ms of
    PBKDF2 that verifying a password costs never blocks the event loop for everyone else."""
    auth = auth_service()
    client = request.client.host if request.client else "?"
    user, problem, retry_after = auth.login(body.username, body.password, client)
    if user is None:
        log.warning("failed sign-in for %r from %s", body.username[:40], client)
        # 429 when throttled, so the page can say "wait" rather than "wrong password", and
        # Retry-After so a client knows how long without parsing the sentence.
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return JSONResponse({"detail": problem},
                            status_code=429 if retry_after else 401, headers=headers)

    log.info("%s signed in from %s", user.name, client)
    response = JSONResponse({"user": user.public()})
    response.set_cookie(
        COOKIE,
        auth.sessions.issue(user),
        max_age=int(float(os.getenv("AUTH_SESSION_HOURS", "12")) * 3600),
        **auth.cookie_kwargs(request.url.scheme == "https"),
    )
    return response


@app.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    """Public: signing out must work even when the session has already gone, or someone whose
    cookie expired is left with a Sign out button that returns 401 and appears to do nothing."""
    auth = auth_service()
    user = getattr(request.state, "user", None)
    if user is not None and user.sid:
        auth.sso.drop(user.sid)  # the refresh token dies with the session, not at expiry
    response = JSONResponse({"status": "signed out"})
    # Cleared with the same flags it was set with. The value is already empty and the session is
    # already revoked server-side, so this is cosmetic — but a Set-Cookie without HttpOnly and
    # Secure is what a scanner sees, and a security review that has to be argued down is a cost
    # of its own. This one was: it was reported as "the session cookie is readable by
    # JavaScript", which it is not.
    response.delete_cookie(COOKIE, **auth.cookie_kwargs(request.url.scheme == "https"))
    return response


@app.get("/api/auth/me")
async def me(request: Request) -> JSONResponse:
    """Public on purpose: the page uses it to decide whether to show itself or the login form."""
    user = getattr(request.state, "user", None)
    auth = auth_service()
    body: dict = {
        "authenticated": user is not None,
        "required": auth.enabled,
        "mode": auth.mode,
        "local": auth.mode == "local" or auth.allow_local,
        "user": user.public() if user else None,
    }
    # A personal Microsoft account cannot use the multi-tenant endpoints for an ARM scope, so
    # the page has to offer them a tenant sign-in instead of letting them bounce off a message
    # about needing a work account. Only advertised when a directory has been named for it.
    if auth.sso.enabled and auth.sso.multi_tenant and auth.sso.home_tenant:
        body["home_tenant"] = auth.sso.home_tenant
        # Whether to also advertise the general work-account button. Hiding it needs somewhere
        # else to send people, so it only applies once a home tenant exists.
        body["hide_work_signin"] = auth.sso.hide_work_button
    if user is not None and user.source == "entra":
        # Surface the RBAC picture so the header can say what this person is looking at.
        session = auth.sso.get(user.sid)
        if session is not None:
            subs = await auth.sso.subscriptions(session)
            body["subscriptions"] = len(subs)
            body["tenant"] = session.tenant
            if auth.sso.multi_tenant:
                body["tenants"] = session.tenants
                # A directory that would not let this app act for them is why a subscription
                # they expected is missing. Saying nothing turns that into a silent gap.
                if session.skipped:
                    body["skipped_tenants"] = session.skipped
            if session.problem:
                body["problem"] = session.problem
    return JSONResponse(body)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> JSONResponse:
    """Liveness for a load balancer. Deliberately says nothing about the estate."""
    return JSONResponse({"status": "ok"})


@app.get("/api/subscriptions")
async def subscriptions(request: Request) -> JSONResponse:
    """Subscriptions available to pick from, with their spend in the loaded period.

    In SSO mode this is the signed-in person's list from ARM, so two colleagues open the same
    URL and legitimately see different pickers.
    """
    from . import cost
    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)

    stored = {s["id"]: s for s in warehouse.subscriptions()}
    try:
        async with as_user(user):
            live = (await cost.list_subscriptions())["subscriptions"]
    except Exception:  # noqa: BLE001 - the warehouse alone is enough to populate the picker
        live = []

    merged: list[dict] = []
    for s in live or stored.values():
        row = stored.get(s["id"], {})
        entry = {"id": s["id"], "name": s.get("name") or row.get("name"),
                 "cost": row.get("cost"), "in_warehouse": s["id"] in stored}
        # Which directory it came from. With one tenant this is noise; with several, two
        # subscriptions can share a name and the picker becomes a guess without it.
        if s.get("tenant_name"):
            entry["tenant_name"] = s["tenant_name"]
        merged.append(entry)
    for sid, row in stored.items():
        if not any(m["id"] == sid for m in merged):
            merged.append({**row, "in_warehouse": True})

    if allowed is not None:
        # Belt and braces: `live` already came back under their own token, but the warehouse
        # rows did not, and those must not widen the picker.
        permitted_ids = set(allowed)
        merged = [m for m in merged if m["id"] in permitted_ids]

    merged.sort(key=lambda m: (m["cost"] is None, -(m["cost"] or 0)))

    # Anything they are entitled to that the warehouse has never loaded is queued here. The
    # picker is the first thing the page asks for, so this is the earliest honest moment to
    # notice that their estate is wider than the ingest was, and to start closing the gap.
    #
    # Scheduled inside `as_user` on purpose. asyncio.create_task copies the current context, so
    # the background ingest inherits this person's delegated tokens and reads their estate with
    # their own access. Outside the block the context is already reset, and the ingest would run
    # as the app -- which in a multi-tenant deployment can see almost nothing, and would leave
    # every warehouse-backed tab empty for everyone but the tenant the app happens to live in.
    from . import backfill, onboarding

    mine = [{"id": m["id"], "name": m.get("name")} for m in merged]

    # Not while first-run setup is still on the table. The backfill closes a *gap* between what
    # someone is entitled to and what the warehouse holds; on a first run there is no gap,
    # there is nothing at all, and the onboarding card is the deliberate path — it explains
    # itself, and it sets up the export pipeline that the backfill's API route never touches.
    # Letting both run means two ingests writing the same rows while a progress card claims to
    # be the one doing the work.
    awaiting_setup = onboarding.settled() is None and not warehouse.row_count()

    if os.getenv("USER_BACKFILL", "true").lower() != "false" and not awaiting_setup:
        try:
            async with as_user(user):
                backfill.schedule(mine, months=int(os.getenv("INGEST_MONTHS", "3")))
        except HTTPException:
            # An expired Azure session must not fail the picker; the page still renders from
            # what the warehouse already holds.
            log.info("skipping backfill: no usable Azure session for %s", user.name)

    return JSONResponse({"subscriptions": merged, "count": len(merged),
                         "restricted": allowed is not None,
                         # Nothing is pending while setup is unanswered: the card is saying so
                         # already, and a second banner repeating it under different words
                         # reads as two different problems.
                         "backfill": ({"pending": [], "failed": [], "loading": False}
                                      if awaiting_setup else backfill.status_for(mine))})


@app.get("/api/health")
async def health() -> JSONResponse:
    try:
        return JSONResponse(await agent().health())
    except Exception as exc:  # noqa: BLE001 - health must never raise
        return JSONResponse({"status": "degraded", "detail": str(exc)[:300]})


@app.get("/api/overview")
async def overview(request: Request, scope: str = "", currency: str = "") -> JSONResponse:
    """Unscoped comes from the background snapshot (instant). A scoped view is computed on
    demand — the warehouse part is milliseconds, and only forecast/budgets touch Azure.

    An Entra user is never unscoped: their figures are computed over the subscriptions they
    can actually see, so the tiles at the top of the page are their numbers, not the estate's.
    """
    from . import currency as fx

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    def shown(snapshot: dict) -> dict:
        # The snapshot's `currency` is what Azure billed in. Once the figures are converted that
        # label would be wrong, and a wrong currency label on a right number is worse than an
        # unconverted number — the header tiles are the first thing anyone reads.
        out = fx.convert_money(snapshot, currency)
        if out is not snapshot:
            out["currency"] = currency.upper()
            out["converted_from"] = snapshot.get("currency")
        return out

    if not picked and allowed is None:
        return JSONResponse(shown(_overview))

    from . import cost

    try:
        async with as_user(user):
            snapshot = await cost.overview(scope=picked)
        snapshot["status"] = "ok"
        return JSONResponse(shown(snapshot))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"status": "error", "detail": str(exc)[:200]})


@app.post("/api/ask")
async def ask(body: Ask, request: Request) -> StreamingResponse:
    # Entitlement first, then readiness: whether someone may ask does not depend on whether the
    # model happens to be up, and a refusal should say so even while it is starting.
    user = current_user(request)
    allowed = await permitted(user)
    a = agent()
    scope = narrow(body.scope, allowed)

    names: list[str] = []
    if scope:
        from .warehouse import warehouse

        lookup = {s["id"]: s["name"] for s in warehouse.subscriptions()}
        names = [lookup.get(s, s) for s in scope]

    log.info("Q(%s): %s  [scope: %s]", user.email or user.name, body.question[:100],
             names or "all")

    async def stream() -> AsyncIterator[str]:
        # The delegated token is established inside the generator, not in the handler: this is
        # where the agent's tool calls actually run, and a ContextVar set outside would not
        # reliably reach them.
        async with as_user(user):
            # Keep only the last few turns; cost answers rarely need deep history and long
            # transcripts make the model slower and more expensive.
            async for event in a.ask(body.question, body.history[-6:],
                                     scope=scope, scope_names=names):
                yield event.sse()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/warehouse")
async def warehouse_status(request: Request, scope: str = "") -> JSONResponse:
    """What the local cost warehouse currently holds, plus ingest progress.

    Honours `scope` like every other read. It used to accept only the caller's entitlement and
    ignore the parameter, so the header's "local data" figure stayed at the whole estate while
    every other tile followed the subscription picker — the one number on the page that did not
    agree with the others.
    """
    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    state = dict(warehouse.state)
    return JSONResponse({**(await offload(warehouse.summary, scope=picked or allowed)),
                         "ingest": state})


@app.get("/api/diagnostics")
async def diagnostics(request: Request) -> JSONResponse:
    """Measure this instance, rather than reasoning about it from a laptop.

    Refresh timings only make sense against what the hardware can actually do. Two rounds of
    optimisation were aimed at the wrong bottleneck because the numbers being compared came
    from a developer machine: writes measured 2,052 rows/s locally and 39 rows/s here, and the
    gap was attributed first to the query API, then to the filesystem, before anyone measured
    the box itself. This endpoint is the measurement.

    Admin-only, and it writes nothing outside a temporary table.
    """
    import platform
    import tempfile
    import time
    from typing import Any

    import duckdb

    from .warehouse import COLUMNS, insert_rows, warehouse

    require_admin(request)

    def _measure() -> dict[str, Any]:
        out: dict[str, Any] = {
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "db_path": str(warehouse.db_path),
            "durable_path": str(warehouse.durable_path),
            "using_local_cache": warehouse.db_path != warehouse.durable_path,
        }
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith(("MemTotal", "MemAvailable")):
                        out[line.split(":")[0]] = line.split()[1] + " kB"
        except Exception:  # noqa: BLE001 - absent on Windows
            pass

        con = warehouse.connect()
        try:
            out["duckdb_threads"] = con.execute(
                "SELECT current_setting('threads')").fetchone()[0]
            out["duckdb_memory_limit"] = con.execute(
                "SELECT current_setting('memory_limit')").fetchone()[0]
            out["rows_in_costs"] = con.execute("SELECT count(*) FROM costs").fetchone()[0]

            # A plain CPU yardstick, so the write figure can be read against how fast this box
            # is at all rather than against a laptop.
            t0 = time.perf_counter()
            acc = 0
            for i in range(3_000_000):
                acc += i * 3 % 7
            out["cpu_loop_3m_seconds"] = round(time.perf_counter() - t0, 2)

            # Real rows, round-tripped: read some out and write them straight back into a temp
            # table of the same shape. Synthetic strings will not do -- the table has a DATE
            # column and four DOUBLEs, and a benchmark that ignores that is measuring something
            # the ingest never does.
            names = ",".join(f'"{c}"' for c in COLUMNS)
            con.execute(f"CREATE TEMP TABLE bench AS SELECT {names} FROM costs LIMIT 0")
            sample = [list(r) for r in
                      con.execute(f"SELECT {names} FROM costs LIMIT 2000").fetchall()]
            out["sample_rows"] = len(sample)
            if sample:
                t0 = time.perf_counter()
                insert_rows(con, sample, table="bench")
                dt = time.perf_counter() - t0
                out["insert_seconds"] = round(dt, 2)
                out["insert_rows_per_second"] = round(len(sample) / dt) if dt else None

            t0 = time.perf_counter()
            con.execute('SELECT count(*), sum("BilledCost") FROM costs').fetchone()
            out["scan_costs_seconds"] = round(time.perf_counter() - t0, 3)

            # How much of the loaded data actually carries a tag, and where it came from.
            # "Cost by Tag is empty" has several possible causes -- the route that loaded the
            # data cannot return tags, the export does not contain them, or the estate really
            # is untagged -- and they are indistinguishable from the tab alone.
            tagged, total = con.execute(
                'SELECT count(*) FILTER (WHERE "Tags" IS NOT NULL AND "Tags" NOT IN (\'\', \'{}\')),'
                " count(*) FROM costs").fetchone()
            out["rows_with_tags"] = tagged
            out["rows_total"] = total
            out["recent_loads"] = [
                {"subscription": r[0], "period": r[1], "rows": r[2], "status": r[3],
                 "detail": (r[4] or "")[:80], "at": str(r[5])}
                for r in con.execute(
                    "SELECT subscription_name, period, rows, status, detail, ingested_at "
                    "FROM ingest_log ORDER BY ingested_at DESC LIMIT 8").fetchall()
            ]

            # Duplicate detection.
            #
            # Every loader replaces the range it writes, so a row should appear once. If two
            # loaders disagree about how a subscription is *identified* — a bare GUID versus the
            # ARM path FOCUS uses — the delete misses and both copies survive. The totals still
            # look like money, just twice as much of it, which is the hardest kind of wrong to
            # notice from a dashboard.
            out["subaccount_ids"] = [
                {"id": r[0], "name": r[1], "rows": r[2], "cost": round(r[3] or 0, 2)}
                for r in con.execute(
                    'SELECT "SubAccountId", MAX("SubAccountName"), count(*), sum("BilledCost") '
                    'FROM costs GROUP BY 1 ORDER BY 3 DESC LIMIT 12').fetchall()
            ]
            dup_rows, dup_cost = con.execute(
                'SELECT coalesce(sum(n - 1), 0), coalesce(sum(c - c / n), 0) FROM ('
                '  SELECT count(*) AS n, sum("BilledCost") AS c FROM costs'
                '  GROUP BY "ChargePeriodStart", "ResourceId", "ServiceName", "BilledCost"'
                '  HAVING count(*) > 1) t').fetchone()
            out["duplicate_rows"] = dup_rows
            out["duplicate_cost"] = round(dup_cost or 0, 2)

            # What the billing units actually look like, for anything that wants to reason in
            # hours. Azure bills per day, so an hourly *profile* is not available at all -- but
            # a per-running-hour rate is, wherever the meter is sold by time and the quantity is
            # the number of hours consumed. This says how much of the estate that covers.
            out["units"] = [
                {"unit": r[0], "rows": r[1], "cost": round(r[2] or 0, 2),
                 "qty": round(r[3] or 0, 2)}
                for r in con.execute(
                    'SELECT "UnitOfMeasure", count(*), sum("BilledCost"), sum("PricingQuantity")'
                    ' FROM costs WHERE "UnitOfMeasure" IS NOT NULL'
                    " AND \"UnitOfMeasure\" <> '' GROUP BY 1 ORDER BY 3 DESC LIMIT 15").fetchall()
            ]
            out["rows_with_quantity"] = con.execute(
                'SELECT count(*) FROM costs WHERE "PricingQuantity" IS NOT NULL'
                ' AND "PricingQuantity" > 0').fetchone()[0]
            out["distinct_charge_days"] = con.execute(
                'SELECT count(DISTINCT "ChargePeriodStart") FROM costs').fetchone()[0]
        finally:
            con.close()

        # What the two filesystems cost per synchronous write -- the thing DuckDB does on
        # commit, and the reason the database was moved off the share.
        for label, path in (("tmp", tempfile.gettempdir()), ("durable",
                            str(warehouse.durable_path.parent))):
            try:
                probe = os.path.join(path, "._cloudlens_io_probe")
                fd = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_TRUNC)
                t0 = time.perf_counter()
                for _ in range(100):
                    os.write(fd, b"x" * 4096)
                    os.fsync(fd)
                dt = time.perf_counter() - t0
                os.close(fd)
                os.remove(probe)
                out[f"fsync_{label}_ops_per_second"] = round(100 / dt) if dt else None
            except Exception as exc:  # noqa: BLE001
                out[f"fsync_{label}_ops_per_second"] = f"error: {exc}"
        return out

    return JSONResponse(await asyncio.to_thread(_measure))


@app.get("/api/diagnostics/export")
async def diagnostics_export(request: Request, account: str = "", container: str = "",
                             prefix: str = "") -> JSONResponse:
    """Which columns the configured export actually contains.

    The loader fills any column it cannot find with NULL, which is right — a missing
    PublisherName should not fail a refresh — but it means an export that omits Tags is
    indistinguishable, from the dashboard, from an estate that has none.
    """
    from .exports import describe_export, discover_exports

    user = current_user(request)
    require_admin(request)

    if account and container:
        return JSONResponse(await describe_export(account, container, prefix))

    allowed = await permitted(user)
    found = await discover_exports(allowed or None)
    out = []
    for e in [x for x in (found.get("exports") or []) if x.get("container")][:6]:
        entry = {"export": e.get("name"), "type": e.get("type"),
                 "subscription": e.get("subscription_id")}
        try:
            entry.update(await describe_export(e["storage_account"], e["container"],
                                               e.get("root_folder") or ""))
        except Exception as exc:  # noqa: BLE001 - one unreachable export must not hide the rest
            entry["error"] = str(exc)[:200]
        out.append(entry)
    return JSONResponse({"exports": out})


@app.post("/api/ingest")
async def start_ingest(request: Request, months: int = 3,
                       metric: str = "AmortizedCost", quick: bool = False) -> JSONResponse:
    """Kick off a warehouse refresh. Returns immediately; poll /api/warehouse for progress.

    `quick` swaps the asynchronous Cost Details report for the synchronous Query API: seconds
    instead of minutes, at the cost of the columns a query cannot return.
    """
    global _ingest_task
    from .warehouse import warehouse

    who = require_admin(request)
    if _ingest_task and not _ingest_task.done():
        return JSONResponse({"status": "already running", "ingest": warehouse.state})

    # Only the two metrics the UI offers. An unrecognised value would be passed straight to
    # Cost Management and come back as an opaque 400 several minutes into a background job.
    if metric not in ("AmortizedCost", "ActualCost"):
        raise HTTPException(400, "metric must be AmortizedCost or ActualCost")

    log.info("ingest requested by %s (metric=%s, quick=%s)", who.name, metric, quick)
    _ingest_task = asyncio.create_task(
        _run_ingest(max(1, min(months, 12)), metric, quick=quick))
    return JSONResponse({"status": "started", "months": months, "metric": metric,
                         "quick": quick})



@app.get("/api/exports")
async def list_exports(request: Request) -> JSONResponse:
    """Cost Management exports visible to the signed-in user, best candidates first.

    This is the scalable onboarding path: reading an export that Azure already writes on a
    schedule avoids the per-subscription, per-month API calls that make a large tenant take
    hours to load.
    """
    from .exports import discover_exports

    user = current_user(request)
    await permitted(user)  # refuses early if they can see nothing

    try:
        async with as_user(user):
            found = await discover_exports()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface the reason rather than an empty list
        raise HTTPException(status_code=502, detail=f"Could not list exports: {exc}") from exc
    return JSONResponse(found)


class ExportIngest(BaseModel):
    export_id: str | None = Field(
        None,
        description="Resource id of the export to load. Omitted means 'pick the best one': "
                    "discovery already ranks FOCUS ahead of amortized ahead of actual.",
    )
    sas_url: str | None = Field(
        None, description="Container SAS URL, for storage where the data plane is firewalled"
    )
    max_files: int = Field(60, ge=1, le=500)


def _sas_source(url: str) -> str:
    """Account and container from a SAS URL, with the token removed.

    The query string is the credential. It travels through logs, API responses and anything
    the browser caches, so it is cut here rather than anywhere further downstream.
    """
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        host = parts.netloc.split(".", 1)[0]
        return f"{host}{parts.path}".rstrip("/") or "SAS URL"
    except Exception:  # noqa: BLE001 - a label must never be the reason a request fails
        return "SAS URL"


@app.post("/api/exports/ingest")
async def ingest_from_export(body: ExportIngest, request: Request) -> JSONResponse:
    """Load an export's blobs into the warehouse. Poll /api/warehouse for progress."""
    global _ingest_task
    from . import cost
    from .exports import discover_exports

    from .warehouse import warehouse

    require_admin(request)
    if _ingest_task and not _ingest_task.done():
        return JSONResponse({"status": "already running", "ingest": warehouse.state})

    # A SAS URL names its own account and container, so it needs no discovery -- and must not
    # wait on any. Discovery is an ARM call, and the whole reason to hand this endpoint a SAS is
    # that the caller's route to the data does not go through ARM: storage behind a firewall, or
    # an app whose identity has no role on the subscription the export belongs to. Insisting on
    # a successful ARM listing first made the one credential that was always going to work
    # depend on the one that was always going to fail.
    #
    # COST_EXPORT_SAS_URL is the deployment-time answer to the same problem. Where the app's
    # identity cannot list exports at all, the operator configures the URL once and the refresh
    # button works for everyone -- rather than every person who presses it being told to go and
    # find a SAS the UI has no field for.
    sas = body.sas_url or os.getenv("COST_EXPORT_SAS_URL", "").strip()
    if sas and not body.export_id:
        async def run_sas() -> None:
            try:
                await ingest_export(sas_url=sas, max_files=body.max_files)
                await _archive_after_ingest("FocusCost", source=_sas_source(sas))
            except Exception as exc:  # noqa: BLE001 - record it so the UI can show the reason
                log.exception("SAS export ingest failed")
                warehouse.state = {
                    "status": "failed",
                    "detail": str(exc)[:300],
                    "export": "SAS URL",
                    **warehouse.summary(),
                }

        _ingest_task = asyncio.create_task(run_sas())
        return JSONResponse({"status": "started", "export": "SAS URL", "type": "sas",
                             "source": _sas_source(sas)})

    found = await discover_exports()
    usable = [e for e in found["exports"] if e.get("container")]

    if body.export_id:
        match = next((e for e in found["exports"] if e["id"] == body.export_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="No export with that id is visible to you.")
        candidates = [match]
    else:
        # One button, not a list of nine. discover_exports already ranks FOCUS first, and an
        # export with no storage destination configured can never be read, so it is not a
        # candidate no matter how well it ranks.
        #
        # All of them, in rank order, rather than only the best: an export definition outlives
        # the storage account it writes to, so the top-ranked candidate is quite often one whose
        # account has since been deleted and no longer resolves at all. Stopping there reported
        # a DNS failure and left the warehouse untouched while a perfectly readable export sat
        # second in the list.
        candidates = usable
        if not candidates:
            # Distinguish "you have no exports" from "this app cannot see anything at all".
            # They call for opposite actions -- create an export, versus grant the app access --
            # and the second one also rules out the advice the first one wants to give, because
            # 'Latest from Azure' reads cost through the same identity that just came back empty.
            blind = not found["exports"] and not (await cost.list_subscriptions())["count"]
            raise HTTPException(
                status_code=404,
                detail=(
                    "This app cannot see any subscription, so it can neither list exports nor "
                    "read cost directly. Its managed identity needs Reader on the subscription, "
                    "or set COST_EXPORT_SAS_URL to a container SAS for an export's storage."
                    if blind else
                    "No Cost Management export with a storage destination is visible to "
                    "this app. Use 'Latest from Azure' instead."
                ),
            )
        match = candidates[0]

    if not match["container"]:
        raise HTTPException(
            status_code=400,
            detail=f"Export '{match['name']}' has no storage destination configured.",
        )

    # Pre-flight, before anything is started. Without it, an estate whose export storage is
    # private-endpoint only — the normal case in a governed tenant — gives the same experience
    # every time: press Refresh, watch a spinner for half a minute, get a failure. The check is
    # one listing per account in parallel, so it costs a couple of seconds and turns that into
    # an immediate answer that names the wall.
    if not body.sas_url:
        from .exports import reachable

        ok, bad = await reachable(candidates)
        if not ok:
            raise HTTPException(
                status_code=400,
                detail=_explain_export_failures([(e.get("storage_account") or "?", err)
                                                 for e, err in bad]),
            )
        # Rank order is preserved, but an export that just answered goes ahead of one that did
        # not: trying a known-unreachable account first only spends the caller's time.
        candidates = ok
        match = candidates[0]

    async def run() -> None:
        result = await _load_export_pair(candidates, sas_url=body.sas_url,
                                         max_files=body.max_files)
        if result["loaded"]:
            return
        log.error("every visible export failed across %d candidate(s)",
                  len(result["problems"]))
        # Replace the state rather than merging into it. Merging kept `detail` from
        # whatever ran last, and since the UI prefers detail over error it would show a
        # stale message describing a completely different failure.
        warehouse.state = {
            "status": "failed",
            "detail": _explain_export_failures(result["problems"]),
            "export": match["name"],
            **warehouse.summary(),
        }

    _ingest_task = asyncio.create_task(run())
    return JSONResponse({
        "status": "started",
        "export": match["name"],
        "type": match["type"],
        "source": f"{match['storage_account']}/{match['container']}/{match['root_folder'] or ''}",
    })


def _explain_export_failures(problems: list[tuple[str, str]]) -> str:
    """One sentence about why no export could be read, not three stack traces spliced together.

    Every one of these failures is the same shape — the export's storage account is unreachable
    or refuses this identity — and the old message concatenated three of them and truncated the
    result at 300 characters, which produced a string that ended mid-word and told the reader
    nothing they could act on. What matters is whether the wall is the network or the role,
    because those need opposite fixes, so that is what gets said.
    """
    if not problems:
        return "No export could be read."

    accounts = sorted({a for a, _ in problems})
    blob = " ".join(e.lower() for _, e in problems)
    network = "does not resolve" in blob or "private-endpoint" in blob
    denied = "denied" in blob or "403" in blob

    named = ", ".join(accounts[:3]) + (f" and {len(accounts) - 3} more" if len(accounts) > 3 else "")
    head = (f"None of the {len(problems)} Cost Management export(s) could be read "
            f"({named}).")

    if network and denied:
        why = ("Some are private-endpoint only and unreachable from this app's network; the rest "
               "refused this identity.")
    elif network:
        why = "Their storage accounts are private-endpoint only and do not resolve from this app."
    elif denied:
        why = "Their storage accounts refused this app's identity."
    else:
        why = problems[0][1][:120]

    return (f"{head} {why} Use Amortized or API data instead — both read cost through ARM and "
            "do not touch blob storage.")[:600]


@app.get("/api/onboarding")
async def onboarding_state(request: Request) -> JSONResponse:
    """Whether this deployment still needs first-run setup, and how a scan is going.

    Public-ish in the sense that any signed-in person may ask: an empty dashboard needs an
    explanation whoever is looking at it. Only an admin is offered the button.
    """
    from . import cost, onboarding
    from .warehouse import warehouse

    user = current_user(request)
    try:
        allowed = await permitted(user)
    except HTTPException:
        # Signed in but Azure shows them nothing. That is an answer the card should explain,
        # not a reason for it to fail to load.
        allowed = []

    rows = await offload(warehouse.row_count)

    # How many subscriptions they can actually see, asked of Azure rather than of the
    # warehouse. On a first run the warehouse is empty *by definition*, so counting its
    # subscriptions returns zero at exactly the moment the answer matters — and the card
    # would tell someone with a perfectly good estate that they have no access to anything.
    if allowed is not None:
        count = len(allowed)
    else:
        try:
            async with as_user(user):
                count = (await cost.list_subscriptions())["count"]
        except Exception as exc:  # noqa: BLE001 - an ARM hiccup must not break the card
            log.info("onboarding: could not count subscriptions: %s", str(exc)[:200])
            count = len(warehouse.subscriptions())

    return JSONResponse(onboarding.state(rows, bool(user.admin), count,
                                         ingesting=_ingest_running()))


def _ingest_running() -> bool:
    """Whether any ingest is in flight — this app's boot-time one, or a manual refresh."""
    from .warehouse import warehouse

    if _ingest_task is not None and not _ingest_task.done():
        return True
    return str((warehouse.state or {}).get("status") or "") == "running"


@app.post("/api/onboarding/scan")
async def onboarding_scan(request: Request, months: int = 3) -> JSONResponse:
    """Find cost data for this deployment and load it. First run only.

    Runs as the signed-in person, so every Azure call — listing exports, creating one,
    reading cost — is authorised against their own access rather than the app's.
    """
    from . import cost, onboarding
    from .warehouse import warehouse

    user = require_admin(request)
    if await offload(warehouse.row_count):
        # Somebody else got there first, or a refresh landed while this page was open. Not an
        # error: the outcome they wanted has already happened.
        raise HTTPException(409, "This deployment already has cost data loaded.")
    if onboarding.settled():
        raise HTTPException(409, "First-time setup has already been completed.")

    async with as_user(user):
        subs = (await cost.list_subscriptions())["subscriptions"]
        if not subs:
            raise HTTPException(
                400, "Your account cannot see any Azure subscriptions, so there is nothing "
                     "to load.")
        # Started inside `as_user`: create_task copies the current context, so the scan
        # inherits this person's delegated tokens. Outside the block the context is already
        # reset and every Azure call would go out as the app, which in a multi-tenant
        # deployment can often see nothing at all.
        started = onboarding.start([{"id": s["id"], "name": s.get("name")} for s in subs],
                                   months=max(1, min(months, 12)))

    if not started:
        return JSONResponse({"status": "already running",
                             "progress": onboarding.progress()})
    log.info("first-run scan started by %s over %d subscription(s)", user.name, len(subs))
    return JSONResponse({"status": "started", "subscriptions": len(subs),
                         "progress": onboarding.progress()})


@app.post("/api/onboarding/dismiss")
async def onboarding_dismiss(request: Request) -> JSONResponse:
    """Stop offering first-run setup. Deliberately irreversible from the UI.

    The offer is meant to be answered once. Someone who dismisses it and later wants data has
    the Refresh button and the Cost exports tab, which is where that job belongs anyway.
    """
    from . import onboarding

    user = require_admin(request)
    await offload(onboarding.settle, "dismissed")
    log.info("first-run setup dismissed by %s", user.name)
    return JSONResponse({"status": "dismissed"})


@app.get("/api/archive")
async def archive_status(request: Request, limit: int = 20) -> JSONResponse:
    """What the daily archive holds, and whether it can be reached.

    Not admin-only: knowing whether a copy of today's numbers exists is part of trusting the
    numbers, and the response carries no cost data — only file names and sizes.
    """
    from . import archive

    current_user(request)
    return JSONResponse(await archive.status(limit=max(1, min(limit, 200))))


@app.post("/api/archive")
async def archive_now(request: Request, metric: str = "FocusCost") -> JSONResponse:
    """Write today's archive from whatever the warehouse currently holds.

    A refresh archives on its own, so this exists for the case where the refresh succeeded but
    the archive did not — a storage outage, a role granted five minutes too late — and someone
    wants today's copy without reloading gigabytes of cost data to get it.
    """
    from . import archive

    require_admin(request)
    try:
        return JSONResponse(await archive.archive_current(metric, source="manual"))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("manual archive failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not write the archive: {str(exc)[:300]}") from exc


@app.get("/api/archive/compare")
async def archive_compare(request: Request, dataset: str = "actual",
                          earlier: str = "", later: str = "",
                          group_by: str = "ServiceName", top: int = 15) -> JSONResponse:
    """What changed between two archived snapshots of the same dataset.

    The dashboard can only ever show the latest read. Azure restates cost data for days after
    the fact, so "what did we think this cost when we reported it" is a question the warehouse
    cannot answer at all — the archive exists to answer it, and this is the endpoint that does.
    """
    from . import archive

    current_user(request)
    if not earlier or not later:
        raise HTTPException(400, "Give two days to compare, as earlier= and later=.")
    try:
        return JSONResponse(await archive.compare(dataset, earlier, later,
                                                  group_by=group_by,
                                                  top=max(1, min(top, 100))))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("archive compare failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not compare the archives: {str(exc)[:300]}") from exc


@app.get("/api/schedules")
async def list_schedules(request: Request, scope: str = "") -> JSONResponse:
    """Scheduled Cost Management exports, and where they write.

    A schedule is how a refresh stops taking minutes: Azure writes the data to blob storage on
    its own timetable, and the refresh becomes a blob read rather than a report it has to wait
    for Azure to generate.
    """
    from . import schedules

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await schedules.listing(picked or allowed))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - the tab must still render
        log.info("could not list schedules: %s", str(exc)[:200])
        return JSONResponse({"schedules": [], "count": 0, "error": str(exc)[:300],
                             "metrics": list(schedules.METRICS), "subscriptions": []})


class ScheduleRequest(BaseModel):
    """A daily export to create, so refreshes stop waiting on the Cost Details API."""

    subscription: str = Field(min_length=1, max_length=64)
    metric: str = Field("AmortizedCost", max_length=24)
    recurrence: str = Field("Daily", max_length=12)
    run_now: bool = True


@app.post("/api/schedules")
async def create_schedule(body: ScheduleRequest, request: Request) -> JSONResponse:
    """Create a scheduled export writing to this deployment's own archive container.

    Runs as the signed-in person where delegated ARM is on, so Azure enforces their rights
    directly — the same reasoning as budget creation, and for the same reason: this writes.
    """
    from . import archive, schedules

    user = require_admin(request)
    allowed = await permitted(user)
    if allowed is not None and body.subscription not in set(allowed):
        raise HTTPException(403, "That subscription is not one you have access to.")
    if not archive.enabled():
        raise HTTPException(400, "No archive storage is configured to write the export to.")

    # The destination is always *this* deployment's archive account, wherever the subscription
    # being exported happens to live. Deriving it from `body.subscription` produced a resource
    # id that simply does not exist as soon as anyone scheduled an export for a second
    # subscription — the account is in one place, the data comes from several.
    account_id = os.getenv("ARCHIVE_ACCOUNT_ID", "").strip()
    if not account_id:
        raise HTTPException(
            400,
            "ARCHIVE_ACCOUNT_ID is not configured, so there is no storage account to write the "
            "export to. Set it to the full resource id of the archive storage account.",
        )
    try:
        async with as_user(user):
            made = await schedules.create(
                body.subscription, body.metric,
                account_id=account_id,
                container=archive.CONTAINER,
                recurrence=body.recurrence,
                run_now=body.run_now,
            )
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("schedule create failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Azure refused the export: {str(exc)[:300]}") from exc

    log.info("schedule %s created on %s by %s", made["name"], body.subscription, user.name)
    return JSONResponse(made, status_code=201)


@app.post("/api/schedules/run")
async def run_schedule(request: Request, subscription: str, name: str) -> JSONResponse:
    """Produce a scheduled export now instead of waiting for its next run."""
    from . import schedules

    user = require_admin(request)
    try:
        async with as_user(user):
            return JSONResponse(await schedules.run_now(subscription, name))
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/schedules")
async def delete_schedule(request: Request, subscription: str, name: str) -> JSONResponse:
    """Remove a schedule. Data it has already written is left in place."""
    from . import schedules

    user = require_admin(request)
    try:
        async with as_user(user):
            return JSONResponse(await schedules.remove(subscription, name))
    except schedules.ScheduleError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/archive/export")
async def export_selection(request: Request, scope: str = "", label: str = "",
                           fmt: str = "csv", since: str = "", until: str = "",
                           source: str = "", dataset: str = "actual") -> JSONResponse:
    """Export chosen subscriptions, over a chosen date range, from a chosen day's read.

    Two different meanings of "date", kept apart on purpose:

      * `since`/`until` narrow the **cost period** — which days of spend end up in the file.
      * `source` picks **which read** they come from: today's warehouse, or an archived snapshot
        from an earlier day. Azure restates cost data for days afterwards, so August as read on
        the 27th is not August as read on the 28th.

    CSV by default, in the same column order a FOCUS export produces, so the file is
    interchangeable with one Azure wrote.
    """
    from . import archive

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    if not picked:
        raise HTTPException(400, "Select at least one subscription to export.")

    try:
        return JSONResponse(await archive.export_selection(
            scope=picked, fmt=fmt, label=label,
            since=since or None, until=until or None,
            source=source or None, dataset=dataset))
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("scoped export failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not write the export: {str(exc)[:300]}") from exc


@app.get("/api/archive/export/download")
async def download_selection(request: Request, scope: str = "", label: str = "",
                             fmt: str = "csv", since: str = "", until: str = "",
                             source: str = "", dataset: str = "actual") -> Response:
    """The same selected export, sent to the browser instead of to storage.

    A GET rather than a POST, and reached from a real link, because that is what makes the
    browser download a file natively — with its own progress and its own downloads list —
    instead of the page having to fetch the bytes and synthesise a click. It is the same
    reasoning the report builder's download links already follow.

    Deliberately does not require the archive to be configured. Writing a copy to storage needs
    an account; handing someone their own data does not, and refusing to export because there is
    nowhere to file a second copy would be a strange thing to insist on.
    """
    from . import archive

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    if not picked:
        raise HTTPException(400, "Select at least one subscription to export.")

    try:
        built = await archive.build_selection(
            scope=picked, fmt=fmt, label=label,
            since=since or None, until=until or None,
            source=source or None, dataset=dataset)
    except archive.ArchiveError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.warning("scoped export download failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not build the export: {str(exc)[:300]}") from exc

    return Response(
        content=built["data"],
        media_type=built["content_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{built["filename"]}"',
            # The row count is worth having without opening the file, and a link download gives
            # the page no other way to learn it.
            "X-CloudLens-Rows": str(built["rows"]),
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/currency")
async def currency_options(request: Request, refresh: bool = False) -> JSONResponse:
    """Which display currencies this deployment can honestly offer, and on what authority.

    USD is always available where the data carries `CostInUsd` — Azure records the USD
    equivalent on the invoice itself, so it needs no exchange rate. Everything else uses a
    published reference rate fetched daily, and the response carries the source and the date so
    a converted figure can be quoted rather than presented as a fact from nowhere.
    """
    from . import currency

    current_user(request)
    if refresh:
        await currency.refresh(force=True)
    return JSONResponse(await currency.available())


@app.get("/api/dashboard/shutdown")
async def dashboard_shutdown(request: Request, days: int = 14, scope: str = "",
                             currency: str = "") -> JSONResponse:
    """VMs billed for 168 hours a week that are only used for about 60.

    Needs hourly CPU rather than the daily average `rightsizing` uses — the whole question is
    *when* a machine is busy, and a daily mean cannot tell 09:00–18:00 from 03:00.
    """
    from . import currency as fx
    from . import shutdown

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await shutdown.find_schedulable(picked or None, days=days)
        return JSONResponse(fx.convert_money(found, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("shutdown tab failed: %s", str(exc)[:200])
        raise HTTPException(502, f"Could not analyse schedules: {str(exc)[:200]}") from exc


@app.get("/api/dashboard/anomalies")
async def dashboard_anomalies(request: Request, days: int = 60, scope: str = "",
                              currency: str = "") -> JSONResponse:
    """Days that did not look like the others.

    Every other tab answers a question someone thought to ask; this one has to speak first. A
    cost spike is expensive because nobody noticed it for three weeks, and nobody notices by
    reading a chart every morning.

    Warehouse-only, so it lands in milliseconds and needs no Azure call.
    """
    from . import anomalies

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        return JSONResponse(await offload(anomalies.analyse, picked or None,
                                          days=max(14, min(days, 365)),
                                          display_currency=currency or None))
    except anomalies.AnomalyError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("anomaly tab failed: %s", str(exc)[:200])
        raise HTTPException(502, f"Could not analyse anomalies: {str(exc)[:200]}") from exc


@app.get("/api/insights")
async def insights(request: Request, days: int = 30, scope: str = "",
                   currency: str = "") -> JSONResponse:
    """Every source read at once, cross-referenced, and ranked by what it is worth.

    The one endpoint in the app that answers a question nobody asked. Every other tab responds to
    a choice someone made; this one has to decide for itself what is worth saying, which is why
    each finding carries the source it came from and the reasoning behind its number.

    Slow by the standards of this app — it runs the live Azure tabs as well as the warehouse
    ones — so it is deliberately not part of any page load. It runs when asked.
    """
    from . import insights as engine

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await engine.analyse(picked or None,
                                                     days=max(7, min(days, 365)),
                                                     currency=currency or None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("insights failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Could not complete the analysis: {str(exc)[:200]}") from exc


@app.get("/api/dashboard/sections")
async def dashboard_sections(request: Request, days: int = 30, scope: str = "",
                             currency: str = "") -> JSONResponse:
    """The tab bar, with what each tab is costing — so the labels carry information."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    return JSONResponse(await offload(get_dashboard().sections, picked or None,
                                      days=max(1, min(days, 365)), currency=currency))


@app.get("/api/dashboard/executive")
async def dashboard_executive(request: Request, days: int = 30, scope: str = "",
                              currency: str = "") -> JSONResponse:
    """The opening view. One warehouse pass, no Azure calls, so it lands in milliseconds."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    return JSONResponse(await offload(get_dashboard().executive, picked or None,
                                      days=max(1, min(days, 365)), currency=currency))


@app.get("/api/dashboard/waste")
async def dashboard_waste(request: Request, days: int = 30, scope: str = "",
                          currency: str = "") -> JSONResponse:
    """Stale and idle resources. Live inventory, so slower than the other tabs — the cost
    export cannot tell you a disk is attached to nothing.

    The money here comes from Azure rather than the warehouse, so it is converted on the way
    out rather than in SQL.
    """
    from . import currency as fx
    from .waste import find_waste

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await find_waste(subscription_ids=picked or None,
                                     days=max(1, min(days, 365)), top=25)
        return JSONResponse(fx.convert_money(found, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("waste tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "findings": []}, status_code=502)


@app.get("/api/dashboard/savings")
async def dashboard_savings(request: Request, days: int = 30, scope: str = "",
                            currency: str = "") -> JSONResponse:
    """Every savings source, merged so each resource is counted once.

    Six tabs used to answer this question separately, and their totals overlapped: the same
    deallocated VM was inside both the orphan figure and the rightsizing figure, so adding the
    tabs up produced a saving that could not be banked. This endpoint is the one place that
    reconciles them.
    """
    from . import currency as fx
    from . import savings as savings_mod

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            built = await savings_mod.build(picked or None,
                                            days=max(1, min(days, 365)),
                                            currency=currency)
        return JSONResponse(fx.convert_money(built, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - one failing tab must not look like an outage
        log.warning("savings tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "opportunities": []}, status_code=502)


@app.get("/api/dashboard/rightsizing")
async def dashboard_rightsizing(request: Request, days: int = 30,
                                scope: str = "", currency: str = "") -> JSONResponse:
    """Per-VM CPU from Azure Monitor, with power state and what each machine cost.

    Stale resources finds what is dead; this finds what is oversized, which is usually the
    larger number.
    """
    from . import currency as fx
    from .waste import vm_utilisation

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            vms = await vm_utilisation(subscription_ids=picked or None,
                                       days=max(1, min(days, 90)))
        return JSONResponse(fx.convert_money(vms, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("rightsizing tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "vms": []}, status_code=502)


@app.get("/api/dashboard/advisor")
async def dashboard_advisor(request: Request, category: str = "Cost",
                            scope: str = "", currency: str = "") -> JSONResponse:
    """Microsoft's own recommendations, with its own savings estimates."""
    from . import currency as fx
    from .waste import advisor_recommendations

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    wanted = category if category in ("Cost", "Security", "HighAvailability",
                                      "OperationalExcellence", "Performance", "All") else "Cost"

    try:
        async with as_user(user):
            recs = await advisor_recommendations(subscription_ids=picked or None,
                                                 category=wanted)
        return JSONResponse(fx.convert_money(recs, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("advisor tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "recommendations": []}, status_code=502)


@app.get("/api/dashboard/governance")
async def dashboard_governance(request: Request, days: int = 30, scope: str = "") -> JSONResponse:
    """Tag coverage and accelerated networking — configuration rather than spend.

    Both halves are gathered together because the tab shows them together, and both are
    Resource Graph queries whose latency is dominated by the round trip rather than the work.
    A failure in one must not lose the other, so they are gathered independently.
    """
    from .governance import accelerated_networking, tagging

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    async with as_user(user):
        tags, accel = await asyncio.gather(
            tagging(subscription_ids=picked or None, days=days),
            accelerated_networking(subscription_ids=picked or None),
            return_exceptions=True,
        )

    def unwrap(result: Any, label: str) -> dict[str, Any]:
        if isinstance(result, Exception):
            log.warning("governance/%s failed: %s", label, str(result)[:200])
            return {"error": str(result)[:300]}
        return result

    return JSONResponse({
        "tagging": unwrap(tags, "tagging"),
        "accelerated_networking": unwrap(accel, "accelerated_networking"),
    })


@app.get("/api/dashboard/rates")
async def dashboard_rates(request: Request, scope: str = "", currency: str = "") -> JSONResponse:
    """Commitment savings Azure is recommending — reservations and savings plans.

    Read from Advisor rather than the Consumption benefit API, because Advisor's figures are
    scoped to the subscription while Consumption's are scoped to the whole billing account.
    """
    from . import currency as fx
    from .rates import rate_optimisation

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            opt = await rate_optimisation(subscription_ids=picked or None)
        return JSONResponse(fx.convert_money(opt, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("rates tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "commitments": []}, status_code=502)


@app.get("/api/dashboard/health")
async def dashboard_health(request: Request, scope: str = "") -> JSONResponse:
    """Retirements, deprecations and required migrations, from Azure Service Health."""
    from .health import advisories

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            return JSONResponse(await advisories(subscription_ids=picked or None))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("service health tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "advisories": []}, status_code=502)


@app.get("/api/dashboard/commitments")
async def dashboard_commitments(request: Request, days: int = 30,
                                scope: str = "", currency: str = "") -> JSONResponse:
    """Reservation, savings plan and Spot coverage, from the local warehouse."""
    from .commitments import get_commitments

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    c = get_commitments()
    days = max(1, min(days, 365))
    # Both are warehouse passes and both block; running them off the event loop together costs
    # the slower of the two rather than their sum.
    cover, spot = await asyncio.gather(
        offload(c.coverage, picked or None, days=days, currency=currency or None),
        offload(c.spot_savings, picked or None, currency=currency or None),
    )
    return JSONResponse({"coverage": cover, "spot": spot})


@app.get("/api/dashboard/uptime")
async def dashboard_uptime(request: Request, days: int = 30, scope: str = "",
                           currency: str = "") -> JSONResponse:
    """Effective hourly rates and measured uptime, for the Cost by hour tab.

    Warehouse only, no Azure calls. Azure bills by the day, so there is no hour-of-day series
    to fetch — what makes this tab possible is that time-metered rows state how many hours
    were consumed, which is uptime by another name.
    """
    from .uptime import hourly_cost

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    return JSONResponse(await offload(hourly_cost, picked or None,
                                      days=max(1, min(days, 365)),
                                      currency=currency or None))


@app.get("/api/dashboard/tags")
async def dashboard_tags(request: Request, days: int = 30, scope: str = "",
                         currency: str = "") -> JSONResponse:
    """Tagged resources with their costs and tag keys, for the Cost by Tag tab.

    Deliberately returns the resources rather than an answer: which resources match depends on
    the tags someone has selected and whether they want all of them or any, and that changes on
    every click. Answering here would put a network round trip behind each one.

    The live key lookup is a second, optional pass. Cost data only knows a tag once usage has
    been metered while it was attached, so a tag set today — or one on a stopped or free
    resource — is invisible here no matter how correct it is. Asking Azure separately lets the
    tab distinguish "no such tag" from "no spend behind that tag yet".
    """
    from .tags import cost_by_tag, live_tag_keys, merge_live_keys

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    subs = picked or allowed or None

    # Both halves at once: the warehouse query is offloaded to a thread while the Azure call for
    # live tag keys is in flight, so the tab costs the slower of the two rather than their sum.
    payload, live = await asyncio.gather(
        offload(cost_by_tag, picked or None, days=max(1, min(days, 365)),
                currency=currency or None),
        live_tag_keys(subs),
    )
    merged = merge_live_keys(payload, live)
    # Say when the loaded data simply has no tag column, rather than letting the tab report an
    # untagged estate. A quick refresh uses the Query API, which cannot return tags at all — so
    # after one the warehouse holds every cost but no tag, and "none of your 217 resources carry
    # a tag" is a confident statement about someone's estate that happens to be false.
    #
    # Read from the ingest log rather than only from `warehouse.state`: state is rebuilt from a
    # summary on boot, so a restart forgot how the data got there and the tab went back to
    # blaming the estate. The log is in the database, so it survives exactly as long as the rows
    # it describes.
    merged["tags_not_loaded"] = await offload(_tags_missing_from_load)
    return JSONResponse(merged)


def _tags_missing_from_load() -> bool:
    """Whether the newest successful load was one that cannot carry tags."""
    from .warehouse import warehouse

    if "Tags" in (warehouse.state.get("omits") or []):
        return True
    try:
        with warehouse.connect() as con:
            row = con.execute(
                "SELECT detail FROM ingest_log WHERE status = 'ok' "
                "ORDER BY ingested_at DESC LIMIT 1").fetchone()
        return bool(row) and "quick" in (row[0] or "").lower()
    except Exception:  # noqa: BLE001 - never let this be why the tab fails to render
        return False


class TagFilter(BaseModel):
    """One tag key and the values to match, already resolved by the tab."""

    key: str = Field(min_length=1, max_length=512)
    values: list[str] = Field(default_factory=list)


class BudgetRequest(BaseModel):
    """A budget to create from the tag selection currently on screen."""

    subscription: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=63)
    amount: float = Field(gt=0)
    time_grain: str = Field("Monthly", max_length=16)
    start: str = Field("", max_length=32)
    end: str = Field("", max_length=32)
    tags: list[TagFilter] = Field(default_factory=list)
    mode: str = Field("all", max_length=8)
    thresholds: list[float] = Field(default_factory=lambda: [80.0, 100.0])
    emails: list[str] = Field(default_factory=list)


def _budget_period(budget: dict) -> tuple[str, str]:
    """The budget's *current* period, not the whole span it was created for.

    Azure gives a budget a start date and an end date years away — `2026-08-01` to `2036-08-01`
    on this estate — and resets the counter every grain. `current_spend` is the figure for the
    period in progress, so a chart drawn across the full span would put one month of spend on a
    ten-year axis and show a flat line at zero.
    """
    from calendar import monthrange

    today = datetime.now(timezone.utc).date()
    months = {"Monthly": 1, "Quarterly": 3, "Annually": 12}.get(
        str(budget.get("time_grain") or "Monthly"), 1)

    try:
        began = date.fromisoformat(str(budget.get("start"))[:10])
    except (TypeError, ValueError):
        began = today.replace(day=1)

    # Walk whole grains forward from the start date until the one containing today. Anchored on
    # the budget's own start rather than the calendar, because a quarterly budget beginning in
    # February runs Feb–Apr, not Jan–Mar.
    start = began
    while True:
        nxt_month = start.month - 1 + months
        nxt = date(start.year + nxt_month // 12, nxt_month % 12 + 1, 1)
        nxt = nxt.replace(day=min(began.day, monthrange(nxt.year, nxt.month)[1]))
        if nxt > today:
            break
        start = nxt
        if start.year > today.year + 1:      # a malformed start date must not spin
            break

    return start.isoformat(), today.isoformat()


def _budget_tags(node: Any) -> list[tuple[str, list[str]]]:
    """The tag clauses of an ARM budget filter, as (key, values) pairs.

    Dimension filters (resource group, service name) are deliberately not translated: the
    warehouse holds those columns, but a budget filtered on one would need its own mapping and
    a half-applied filter is worse than an honestly absent one. `filtered_exactly` on the
    response says which case a given budget is.
    """
    out: list[tuple[str, list[str]]] = []
    if not isinstance(node, dict):
        return out
    if "and" in node:
        for clause in node.get("and") or []:
            out.extend(_budget_tags(clause))
        return out
    clause = node.get("tags")
    if isinstance(clause, dict) and clause.get("name"):
        out.append((str(clause["name"]), [str(v) for v in clause.get("values") or []]))
    return out


async def _attach_budget_trends(payload: dict, currency: str = "") -> None:
    """Give every budget the daily shape behind its single number.

    The warehouse and Azure are two different sources for the same money, and they will not
    agree to the penny: the warehouse holds whatever the last refresh loaded, Azure's budget
    figure is live and lags usage by up to a day. That is fine and expected — but a chart whose
    total visibly contradicts the headline it sits under is not, so each trend carries the
    warehouse total and the UI says so when the two have genuinely diverged rather than quietly
    drawing over the difference.
    """
    from .warehouse import warehouse

    budgets = payload.get("budgets") or []
    if not budgets:
        return

    def build(b: dict) -> dict:
        start, end = _budget_period(b)
        tags = _budget_tags(b.get("filter_raw"))
        # Handed to the browser so a budget can open the Tags tab on its own filter — the
        # return leg of the journey that created it. Pairs rather than the ARM shape, because
        # the UI should not have to learn Azure's filter grammar to follow a link.
        b["tag_filter"] = [[k, v] for k, v in tags]
        # A dimension filter we cannot translate means the line would be drawn over more spend
        # than the budget actually watches. Say so rather than draw it.
        has_filter = bool(b.get("filter"))
        exact = (not has_filter) or bool(tags)
        trend = warehouse.budget_trend(
            b.get("subscription_id") or "", start, end,
            tags=tags or None, currency=currency or None)
        trend["filtered_exactly"] = exact
        return trend

    try:
        trends = await offload(lambda: [build(b) for b in budgets])
    except Exception as exc:  # noqa: BLE001 - the list is useful without the charts
        log.info("could not build budget trends: %s", str(exc)[:200])
        return

    for b, t in zip(budgets, trends):
        # Only worth carrying if there is a shape to draw. One point is not a trend, and an
        # empty one would draw an axis with nothing on it.
        b["trend"] = t if len(t.get("labels") or []) > 1 else None


@app.get("/api/budgets")
async def list_budgets(request: Request, scope: str = "", currency: str = "") -> JSONResponse:
    """Budgets already defined on the subscriptions in scope.

    Read live rather than from the warehouse: budgets are not in a cost export, and a stale
    "no budgets" would invite someone to create a second one with the same job.

    Under a display currency the limit is converted alongside the spend. Converting only one of
    them would leave a rupee figure being measured against a dollar threshold — and the meter
    beside it would draw a percentage that is simply false.
    """
    from . import cost
    from . import currency as fx

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            found = await cost.budgets(picked or None)
        out = fx.convert_money(found, currency)
        if out is not found:
            for b in out.get("budgets", []):
                b["currency"] = currency.upper()
        await _attach_budget_trends(out, currency)
        return JSONResponse(out)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - the tab can offer to create one regardless
        log.info("could not list budgets: %s", str(exc)[:200])
        return JSONResponse({"budgets": [], "count": 0, "error": str(exc)[:300]})


@app.post("/api/budgets")
async def create_budget(body: BudgetRequest, request: Request) -> JSONResponse:
    """Create a tag-filtered budget on one subscription.

    The only write this app makes to Azure, so who it runs as is the whole question.

    With delegated ARM the call carries the signed-in person's own token and Azure enforces
    their RBAC directly — someone who cannot create a budget gets Azure's refusal, which is the
    correct answer from the correct authority. Without it the call would run as the app's
    managed identity, which is generally *more* privileged than the person driving it, so that
    path is restricted to admins rather than quietly lending out the app's access.
    """
    from . import budgets as budget_api

    user = current_user(request)
    auth = auth_service()
    delegated = user.source == "entra" and auth.sso.delegated
    if user.source == "entra" and not delegated and not user.admin:
        raise HTTPException(
            403,
            "Creating a budget here would run as this app rather than as you, so it is limited "
            "to admins. Ask an administrator to enable delegated Azure access (AUTH_DELEGATED_ARM) "
            "and it will run under your own Azure permissions instead.",
        )

    # The subscription has to be one they can already see. Azure would refuse an unauthorised
    # one anyway, but only after we had accepted and forwarded it.
    allowed = await permitted(user)
    if allowed is not None and body.subscription not in set(allowed):
        raise HTTPException(403, "That subscription is not one you have access to.")

    tags = [{"key": t.key, "values": t.values} for t in body.tags]
    try:
        async with as_user(user):
            created = await budget_api.create(
                subscription_id=body.subscription,
                name=body.name,
                amount=body.amount,
                time_grain=body.time_grain,
                start=body.start or None,
                end=body.end or None,
                tags=tags,
                mode=body.mode,
                thresholds=body.thresholds,
                emails=body.emails or ([user.email] if user.email else []),
            )
    except budget_api.BudgetError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("budget create failed: %s", str(exc)[:300])
        raise HTTPException(502, f"Azure refused the budget: {str(exc)[:300]}") from exc

    log.info("budget %s created on %s by %s", created["name"], body.subscription, user.name)
    return JSONResponse(created, status_code=201)


@app.get("/api/costs/raw.csv")
async def download_raw_costs(request: Request, days: int = 30, scope: str = "") -> Response:
    """The underlying cost rows for the selected subscriptions, as CSV.

    The report formats are summaries. This is the detail behind them — one row per charge, the
    shape a FinOps team wants to pivot themselves. Scoped to the caller's entitlement and to
    whatever subscriptions they have picked, which is the point: the Cost Management portal
    hands you the whole billing account's file and leaves you to filter it.
    """
    import csv
    import io

    from .warehouse import warehouse

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)
    days = max(1, min(days, 365))

    # `reader(scope)` physically shadows the costs table, so this cannot return a row the
    # caller is not entitled to even if the SQL below were wrong.
    with warehouse.reader(picked or allowed) as r:
        hi = r.rows('SELECT max("ChargePeriodStart") AS hi FROM costs')
        hi = hi[0]["hi"] if hi and hi[0]["hi"] else None
        if not hi:
            raise HTTPException(404, "No cost data loaded yet.")
        rows = r.rows(
            "SELECT * FROM costs "
            f"WHERE \"ChargePeriodStart\" > date '{hi}' - INTERVAL {days} DAY "
            'ORDER BY "ChargePeriodStart" DESC, "BilledCost" DESC',
            200_000,
        )

    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    log.info("raw cost export: %d rows, %d subscription(s), for %s",
             len(rows), len(picked or allowed or []), user.email or user.name)
    stamp = str(hi).replace("-", "")
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="cloudlens-costs-{stamp}.csv"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/dashboard/esu")
async def dashboard_esu(request: Request, days: int = 30, scope: str = "",
                        currency: str = "") -> JSONResponse:
    """What is out of support, whether it is covered, and what ESU costs or would cost."""
    from . import currency as fx
    from .esu import esu_report

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        async with as_user(user):
            report = await esu_report(subscription_ids=picked or None, days=days)
        return JSONResponse(fx.convert_money(report, currency))
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("ESU tab failed: %s", str(exc)[:200])
        return JSONResponse({"error": str(exc)[:300], "machines": []}, status_code=502)


class ReportRequest(BaseModel):
    """What to put in the report. Absent fields mean 'everything from the warehouse'."""

    format: str = Field("xlsx", max_length=8)
    days: int = Field(30, ge=1, le=365)
    scope: list[str] = Field(default_factory=list)
    summary: bool = True
    sections: list[str] | None = None       # None = every area
    blocks: list[str] = Field(default_factory=lambda: ["trend", "services", "regions", "resources"])
    live: list[str] = Field(default_factory=list)  # waste / rightsizing / esu / advisor
    # The display currency the report was ordered in. A file that disagrees with the screen it
    # was exported from is worse than no file — someone will paste it into a deck.
    currency: str = Field("", max_length=8)


@app.get("/api/report/options")
async def report_options(request: Request, days: int = 30, scope: str = "",
                         currency: str = "") -> JSONResponse:
    """What this person could put in a report — the areas that actually have spend, plus the
    live datasets. Driven by their data and their scope, so nothing offered comes back empty."""
    from .dashboard import get_dashboard
    from .report import BLOCKS, FORMATS, LIVE

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    summary = await offload(get_dashboard().sections, picked or None,
                            days=max(1, min(days, 365)), currency=currency or None)
    return JSONResponse({
        "areas": [{"id": s["id"], "label": s["label"], "cost": s["cost"]}
                  for s in summary.get("sections", []) if s.get("cost")],
        "blocks": list(BLOCKS),
        "live": [{"id": k, "label": v} for k, v in LIVE.items()],
        "formats": [{"id": k, "label": v[0]} for k, v in FORMATS.items()],
        "currency": summary.get("currency"),
        "as_of": summary.get("as_of"),
    })


@app.post("/api/report")
async def build_report(body: ReportRequest, request: Request) -> Response:
    """Build exactly what was selected. Live datasets are fetched only if asked for."""
    return await _report_response(request, body)


async def _report_response(request: Request, body: ReportRequest,
                           probe: bool = False) -> Response:
    """Shared by the POST builder and the GET download link.

    Kept in one place because a browser can only *link* to a GET, and a link is what makes the
    browser download a file natively — with its own progress and its own downloads list —
    instead of the page having to fetch bytes and synthesise a click.
    """
    from . import currency as fx
    from .report import FORMATS, LIVE, build

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in body.scope if s.strip()], allowed)

    if body.format not in FORMATS:
        raise HTTPException(404, f"Unknown format: {body.format}")

    wanted_live = [name for name in body.live if name in LIVE]

    # A probe answers "would this work?" without paying to build anything. The download link
    # uses it so an expired session becomes a message instead of a file that never arrives.
    if probe:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    # These call Azure, so they are gathered here — inside the request's identity context —
    # rather than inside the synchronous report writer.
    live_data: dict[str, Any] = {}
    if wanted_live:
        from .esu import esu_report
        from .waste import advisor_recommendations, find_waste, vm_utilisation

        jobs = {
            "waste": lambda: find_waste(subscription_ids=picked or None, days=body.days, top=25),
            "rightsizing": lambda: vm_utilisation(subscription_ids=picked or None,
                                                  days=min(body.days, 90)),
            "esu": lambda: esu_report(subscription_ids=picked or None, days=body.days),
            "advisor": lambda: advisor_recommendations(subscription_ids=picked or None,
                                                       category="Cost"),
        }
        async with as_user(user):
            results = await asyncio.gather(*(jobs[n]() for n in wanted_live),
                                           return_exceptions=True)
        for name, result in zip(wanted_live, results):
            if isinstance(result, Exception):
                # One unavailable dataset must not lose the rest of the report.
                log.warning("report: live %s failed: %s", name, str(result)[:200])
                continue
            # These come from Azure in USD and never touch the warehouse, so they are converted
            # the same way their tabs are — otherwise a report ordered in rupees would have a
            # rupee summary and dollar savings sitting on the next page.
            live_data[name] = fx.convert_money(result, body.currency)

    selection = {
        "summary": body.summary,
        "sections": body.sections,
        "blocks": body.blocks,
        "live": wanted_live,
    }

    try:
        payload, filename, media = await run_in_threadpool(
            build, body.format, picked or None, body.days, selection, live_data,
            body.currency or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("report generation failed")
        raise HTTPException(500, f"Could not build the report: {str(exc)[:200]}") from exc

    log.info("report %s (%d KB, live=%s) for %s", body.format, len(payload) // 1024,
             ",".join(wanted_live) or "none", user.email or user.name)
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/report/{fmt}")
async def download_report(fmt: str, request: Request, days: int = 30,
                          scope: str = "", summary: bool = True,
                          sections: str | None = None, blocks: str | None = None,
                          live: str = "", probe: int = 0,
                          currency: str = "") -> Response:
    """The dashboard as a file, as a plain link the browser can download natively.

    Everything the POST builder accepts can be expressed here as query parameters, so the UI
    never has to fetch bytes and fabricate a click: the button is a real link, and the browser
    handles the transfer, the filename and the downloads list itself.

    Generated synchronously — a warehouse-only report is a few hundred KB and takes well under
    a second. FastAPI runs the blocking build in a worker thread, so it never stalls the loop.
    """
    from .report import BLOCKS, FORMATS

    # Checked before the model is built, not after. `ReportRequest.format` is capped at eight
    # characters, so a longer value — "badformat" — raised a Pydantic ValidationError inside the
    # handler and surfaced as a bare 500, while "exe" and "pdf" got the clean 404 below. Same
    # class of mistake, two different answers, depending only on how long the word was.
    if fmt not in FORMATS:
        raise HTTPException(404, f"Unknown format: {fmt[:20]}. "
                                 f"Choose from: {', '.join(sorted(FORMATS))}.")

    def split(value: str | None, default: list[str] | None = None) -> list[str] | None:
        if value is None:
            return default
        return [part for part in value.split(",") if part.strip()]

    body = ReportRequest(
        format=fmt,
        days=max(1, min(days, 365)),
        scope=[s for s in scope.split(",") if s.strip()],
        summary=summary,
        sections=split(sections),                 # absent = every area
        blocks=split(blocks, list(BLOCKS)) or [],
        live=split(live, []) or [],
        currency=currency,
    )
    return await _report_response(request, body, probe=bool(probe))


@app.get("/api/dashboard/{section_id}")
async def dashboard_section(section_id: str, request: Request,
                            days: int = 30, scope: str = "",
                            currency: str = "") -> JSONResponse:
    """One tab's figures. Warehouse only, so this is milliseconds and never calls the model."""
    from .dashboard import get_dashboard

    user = current_user(request)
    allowed = await permitted(user)
    picked = narrow([s for s in scope.split(",") if s.strip()], allowed)

    try:
        return JSONResponse(
            await offload(get_dashboard().section, section_id, picked or None,
                          days=max(1, min(days, 365)), currency=currency))
    except KeyError:
        raise HTTPException(404, f"No such section: {section_id}") from None


app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")

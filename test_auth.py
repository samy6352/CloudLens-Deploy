"""Prove the sign-in actually keeps people out, and lets the right ones in.

Runs against the real app object through Starlette's test client, so what is checked is the
same middleware stack the browser hits — not a reimplementation of it. No Azure calls: the
lifespan (agent, ingest) is never started, and every request under test is refused or answered
before it would need one.
"""
import os
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

TMP = Path(tempfile.mkdtemp())

# Must be set before app.main builds its Auth. A fixed secret and a throwaway user file keep
# this test off the real data/ directory entirely. Empty strings rather than deletions, because
# main.py loads .env on import and load_dotenv only fills in names that aren't already set —
# so a real Entra configuration in .env must not leak into this suite.
os.environ["AUTH_SECRET"] = "test-secret-not-used-anywhere-real"
os.environ["AUTH_API_TOKENS"] = "ci:token-for-scripts-abcdef"
os.environ.setdefault("PROJECT_ENDPOINT", "https://example.invalid/api/projects/test")
for name in ("AUTH_DISABLED", "AUTH_PASSWORD", "AUTH_USERS", "AUTH_CLIENT_ID",
             "AUTH_TENANT_ID", "AUTH_CLIENT_SECRET", "AUTH_ALLOW_LOCAL", "AUTH_ADMINS"):
    os.environ[name] = ""

from app import auth as auth_mod  # noqa: E402

auth_mod.USER_FILE = TMP / "users.json"

from fastapi.testclient import TestClient  # noqa: E402

from app import main  # noqa: E402

fails: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  OK   {label}")
    else:
        print(f"  FAIL {label}{': ' + detail if detail else ''}")
        fails.append(label)


def refused(fn) -> bool:
    """True when the call was rejected with a ValueError, which is how this module says no."""
    try:
        fn()
    except ValueError:
        return True
    except Exception as exc:  # noqa: BLE001 - anything else is a bug, not a refusal
        print(f"       (unexpected {type(exc).__name__}: {exc})")
        return False
    return False


# ------------------------------------------------------------------ hashing
print("PASSWORD HASHING")
stored = auth_mod.hash_password("correct horse battery staple")
check("hash is self-describing", stored.startswith("pbkdf2_sha256$"), stored[:30])
check("hash is salted (two hashes of one password differ)",
      stored != auth_mod.hash_password("correct horse battery staple"))
check("right password verifies", auth_mod.verify_password("correct horse battery staple", stored))
check("wrong password does not", not auth_mod.verify_password("correct horse battery stapl", stored))
check("empty password does not", not auth_mod.verify_password("", stored))
check("corrupt record does not crash", not auth_mod.verify_password("x", "nonsense"))
check("an empty password cannot be set", refused(lambda: auth_mod.hash_password("")))


# ------------------------------------------------------------------ accounts
print("\nACCOUNTS")
users = auth_mod.Users(TMP / "users.json")
users.add("ravi", "hunter2-hunter2", admin=True)
users.add("colleague", "another-password", admin=False)

check("admin authenticates", (u := users.check("ravi", "hunter2-hunter2")) is not None and u.admin)
check("member authenticates without admin",
      (m := users.check("colleague", "another-password")) is not None and not m.admin)
check("username is case-insensitive", users.check("RAVI", "hunter2-hunter2") is not None)
check("wrong password is refused", users.check("ravi", "hunter3") is None)
check("unknown user is refused", users.check("nobody", "hunter2-hunter2") is None)
check("duplicate is refused", refused(lambda: users.add("ravi", "x")))
check("accounts survive a reload",
      len(auth_mod.Users(TMP / "users.json").list()) == 2)
check("password is not stored in the clear",
      "hunter2-hunter2" not in (TMP / "users.json").read_text("utf-8"))
check("remove works", users.remove("colleague") and users.get("colleague") is None)
check("removing an unknown user is a no-op", users.remove("colleague") is False)

# The CLI writes the file behind a running server's back, so a second view of the same file
# has to notice. Without this, a colleague added mid-session couldn't sign in until a restart.
watcher = auth_mod.Users(TMP / "users.json")
check("a second reader starts without the new account", watcher.get("added-later") is None)
users.add("added-later", "their-password")
watcher.refresh_if_changed()
check("an account added elsewhere shows up without a restart",
      watcher.check("added-later", "their-password") is not None)
users.remove("added-later")
watcher.refresh_if_changed()
check("and a removal shows up too", watcher.get("added-later") is None)


# ------------------------------------------------------------------ sessions
print("\nSESSION COOKIES")
sessions = auth_mod.Sessions(users, b"unit-test-secret")
token = sessions.issue(users.get("ravi"))
check("a fresh cookie verifies", (s := sessions.verify(token)) is not None and s.name == "ravi")
check("admin flag survives the round trip", sessions.verify(token).admin)
check("a tampered payload is rejected",
      sessions.verify("eyJ1IjoiZXZlIiwiYSI6dHJ1ZSwiZXhwIjo5OTk5OTk5OTk5fQ." + token.split(".")[1])
      is None)
check("a tampered signature is rejected",
      sessions.verify(token.rsplit(".", 1)[0] + ".AAAA") is None)
check("garbage is rejected", sessions.verify("not-a-token") is None)
check("no cookie is rejected", sessions.verify(None) is None)
check("an expired cookie is rejected",
      sessions.verify(sessions.issue(users.get("ravi"), hours=-1)) is None)
check("a cookie signed with another key is rejected",
      auth_mod.Sessions(users, b"different-secret").verify(token) is None)

# Changing the password re-signs: the old cookie must stop working immediately, which is the
# only revocation a stateless session has.
users.add("ravi", "brand-new-password", admin=True, replace=True)
check("changing a password invalidates existing cookies", sessions.verify(token) is None)
check("a cookie issued after the change works",
      sessions.verify(sessions.issue(users.get("ravi"))) is not None)

# A deleted account must not keep a valid cookie until it expires.
users.add("temp", "temporary-password")
temp_token = sessions.issue(users.get("temp"))
check("a deleted account's cookie stops working",
      sessions.verify(temp_token) is not None and users.remove("temp")
      and sessions.verify(temp_token) is None)


# ------------------------------------------------------------------ throttle
print("\nLOGIN THROTTLE")
throttle = auth_mod.Throttle()
for _ in range(auth_mod.MAX_FAILURES - 1):
    throttle.fail("ip:1.2.3.4", "user:ravi")
check("under the limit is not locked", throttle.locked_for("ip:1.2.3.4") == 0)
throttle.fail("ip:1.2.3.4", "user:ravi")
check("at the limit it locks", throttle.locked_for("ip:1.2.3.4") > 0)
check("the lock follows the username too", throttle.locked_for("user:ravi") > 0)
check("another client is unaffected", throttle.locked_for("ip:9.9.9.9") == 0)
throttle.succeed("ip:1.2.3.4", "user:ravi")
check("a success clears the lock", throttle.locked_for("ip:1.2.3.4", "user:ravi") == 0)


# ---------------------------------------------------------------- the server
print("\nHTTP")
service = main.auth_service()
service.users.add("ravi", "hunter2-hunter2", admin=True, replace=True)
service.users.add("member", "member-password", admin=False, replace=True)

client = TestClient(main.app, follow_redirects=False)

r = client.get("/api/warehouse")
check("an API call without a session is 401", r.status_code == 401, str(r.status_code))
check("the 401 says what to do", r.json().get("auth") == "required", r.text[:80])

r = client.get("/", headers={"accept": "text/html"})
check("a browser hitting / is redirected to the login page",
      r.status_code == 303 and r.headers["location"].startswith("/login?next="),
      f"{r.status_code} {r.headers.get('location')}")

r = client.get("/api/overview?scope=abc", headers={"accept": "text/html"})
check("the redirect remembers where they were going",
      "next=%2Fapi%2Foverview%3Fscope%3Dabc" in r.headers.get("location", ""),
      r.headers.get("location", ""))

check("the login page is public", client.get("/login").status_code == 200)
check("stylesheets are public", client.get("/assets/app.css").status_code == 200)
check("the liveness probe is public", client.get("/healthz").status_code == 200)
check("/healthz leaks nothing", client.get("/healthz").json() == {"status": "ok"})

me = client.get("/api/auth/me").json()
check("/api/auth/me reports a signed-out visitor",
      me["authenticated"] is False and me["required"] is True and me["user"] is None, str(me))

service.throttle = auth_mod.Throttle()
r = client.post("/api/auth/login", json={"username": "ravi", "password": "wrong"})
check("a wrong password is 401", r.status_code == 401, str(r.status_code))
check("the error does not reveal whether the user exists",
      r.json()["detail"] == "Wrong username or password.", r.text[:80])
check("an unknown user gives the identical error",
      client.post("/api/auth/login",
                  json={"username": "ghost", "password": "wrong"}).json()["detail"]
      == "Wrong username or password.")
check("a failed login sets no cookie", "set-cookie" not in r.headers)

service.throttle = auth_mod.Throttle()
r = client.post("/api/auth/login", json={"username": "ravi", "password": "hunter2-hunter2"})
check("the right password signs you in", r.status_code == 200, r.text[:120])
check("the response says who you are",
      r.json()["user"]["name"] == "ravi" and r.json()["user"]["admin"] is True, r.text[:120])
cookie = r.headers.get("set-cookie", "")
check("the cookie is HttpOnly", "httponly" in cookie.lower(), cookie[:90])
check("the cookie is SameSite=Lax", "samesite=lax" in cookie.lower(), cookie[:90])
check("the session cookie is set", auth_mod.COOKIE in cookie, cookie[:60])

check("a signed-in API call is no longer refused",
      client.get("/api/warehouse").status_code == 200)
check("/api/auth/me now knows the user",
      client.get("/api/auth/me").json()["user"]["name"] == "ravi")
check("the app shell is served once signed in",
      client.get("/", headers={"accept": "text/html"}).status_code == 200)

# Admin gate. A pretend in-flight job means the admin path answers "already running" instead
# of starting a real ingest against Azure.
class _Running:
    @staticmethod
    def done() -> bool:
        return False


main._ingest_task = _Running()
check("an admin may trigger an ingest", client.post("/api/ingest?months=1").status_code == 200)
check("an admin may trigger an export ingest",
      client.post("/api/exports/ingest",
                  json={"export_id": "x"}).status_code == 200)

r = client.post("/api/auth/logout")
check("signing out clears the cookie",
      "cost_agent_session=" in r.headers.get("set-cookie", ""), r.headers.get("set-cookie", ""))
check("and the API refuses again afterwards", client.get("/api/warehouse").status_code == 401)

client.post("/api/auth/login", json={"username": "member", "password": "member-password"})
check("a member can read", client.get("/api/warehouse").status_code == 200)
check("a member cannot start an ingest", client.post("/api/ingest").status_code == 403)
check("a member cannot start an export ingest",
      client.post("/api/exports/ingest", json={"export_id": "x"}).status_code == 403)
check("the refusal explains itself",
      "admin" in client.post("/api/ingest").json()["detail"].lower())
main._ingest_task = None
client.post("/api/auth/logout")

# A forged cookie must not be accepted, however plausible it looks.
forged = TestClient(main.app, follow_redirects=False,
                    cookies={auth_mod.COOKIE: "eyJ1IjoicmF2aSIsImEiOnRydWUsImV4cCI6OTk5OTk5OTk5OX0.x"})
check("a forged cookie is refused", forged.get("/api/warehouse").status_code == 401)

check("a bearer token works for scripts",
      client.get("/api/warehouse",
                 headers={"authorization": "Bearer token-for-scripts-abcdef"}).status_code == 200)
check("a wrong bearer token does not",
      client.get("/api/warehouse",
                 headers={"authorization": "Bearer nope"}).status_code == 401)


# The login page will redirect you somewhere after signing in, and "somewhere" comes from the
# URL. A link that looks like ours but lands on someone else's site is a phishing tool.
print("\nREDIRECT AFTER SIGN-IN")
service.throttle = auth_mod.Throttle()
client.post("/api/auth/login", json={"username": "ravi", "password": "hunter2-hunter2"})


def lands_on(next_value: str) -> str:
    r = client.get(f"/login?next={quote(next_value, safe='')}")
    return r.headers.get("location", "") if r.status_code == 303 else f"[{r.status_code}]"


check("an ordinary path is honoured", lands_on("/?scope=abc") == "/?scope=abc")
check("a protocol-relative URL is refused", lands_on("//evil.example.com/x") == "/",
      lands_on("//evil.example.com/x"))
check("a backslash-prefixed URL is refused", lands_on("/\\evil.example.com/x") == "/",
      lands_on("/\\evil.example.com/x"))
check("an absolute URL is refused", lands_on("https://evil.example.com") == "/",
      lands_on("https://evil.example.com"))
check("an API path is refused, so nobody lands on raw JSON",
      lands_on("/api/warehouse") == "/", lands_on("/api/warehouse"))
check("the sign-in route itself is refused, so it cannot loop",
      lands_on("/login") == "/", lands_on("/login"))
client.post("/api/auth/logout")

check("safe_next is the one place this is decided",
      main.safe_next("//x") == "/" and main.safe_next("/\\x") == "/"
      and main.safe_next("/report") == "/report" and main.safe_next(None) == "/")


print("\nERRORS AND HEADERS")
service.throttle = auth_mod.Throttle()
blank = client.post("/api/auth/login", json={"username": "", "password": ""})
check("blank credentials are a normal refusal, not a validation error",
      blank.status_code == 401, str(blank.status_code))
check("and the message is a plain sentence a page can show",
      isinstance(blank.json().get("detail"), str), blank.text[:120])

service.throttle = auth_mod.Throttle()
for _ in range(auth_mod.MAX_FAILURES + 1):
    locked = client.post("/api/auth/login", json={"username": "ravi", "password": "wrong"})
check("a lockout says how long to wait in a header, not just prose",
      locked.status_code == 429 and int(locked.headers.get("retry-after", 0)) > 0,
      f"{locked.status_code} retry-after={locked.headers.get('retry-after')}")
service.throttle = auth_mod.Throttle()

page = client.get("/login")
check("the login page refuses to be framed",
      page.headers.get("x-frame-options") == "DENY"
      and "frame-ancestors 'none'" in page.headers.get("content-security-policy", ""),
      f"{page.headers.get('x-frame-options')} / {page.headers.get('content-security-policy')}")
check("the login page is never cached", "no-store" in page.headers.get("cache-control", ""),
      page.headers.get("cache-control", ""))

client.post("/api/auth/login", json={"username": "ravi", "password": "hunter2-hunter2"})
shell = client.get("/", headers={"accept": "text/html"})
check("the signed-in app shell is never cached either",
      "no-store" in shell.headers.get("cache-control", ""),
      shell.headers.get("cache-control", ""))
check("signing out works even after the session is already gone",
      client.post("/api/auth/logout").status_code == 200
      and client.post("/api/auth/logout").status_code == 200)

# Lockout, through the real endpoint.
service.throttle = auth_mod.Throttle()
codes = [client.post("/api/auth/login",
                     json={"username": "ravi", "password": "wrong"}).status_code
         for _ in range(auth_mod.MAX_FAILURES + 1)]
check("repeated wrong passwords end in a lockout", codes[-1] == 429, str(codes))
check("the lockout is not instant", codes[0] == 401, str(codes))
locked = client.post("/api/auth/login", json={"username": "ravi", "password": "hunter2-hunter2"})
check("even the right password is refused while locked out", locked.status_code == 429,
      locked.text[:100])
check("the message says how long to wait", "second" in locked.json()["detail"], locked.text[:100])
service.throttle = auth_mod.Throttle()
check("it recovers once the lockout is cleared",
      client.post("/api/auth/login",
                  json={"username": "ravi", "password": "hunter2-hunter2"}).status_code == 200)


print(f"\n  {'FAILED: ' + ', '.join(fails) if fails else 'all checks passed'}")
sys.exit(1 if fails else 0)

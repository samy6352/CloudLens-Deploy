"""Authentication, so the app can be handed to other people.

Everything the agent can see is what *the server's* Azure login can see, so an unauthenticated
port on a laptop or a VM is effectively an open window onto the whole estate's spend. This adds
a sign-in in front of it.

Design notes:

- **Stdlib only.** PBKDF2-SHA256 for passwords, HMAC-SHA256 for session cookies. Adding
  `passlib`/`itsdangerous`/`python-jose` for this would be three more dependencies to keep
  patched for two dozen lines of code.
- **Stateless sessions.** The cookie carries a signed payload rather than a server-side session
  id, so restarting the app doesn't sign everyone out and there is no session store to grow.
  The trade-off — you cannot revoke one session early — is handled by keeping lifetimes short
  and letting a password change invalidate that user's cookies (the hash is part of the payload
  signature input).
- **Two roles only.** Anyone signed in can read and ask; only an admin can trigger an ingest,
  because that rewrites shared data and hammers the Azure APIs for everyone.
- **Fails closed.** Auth is on unless someone explicitly sets `AUTH_DISABLED=true`. With no
  users configured, a password is generated on first start and logged once, rather than the
  app quietly serving the estate to anyone who finds the port.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

log = logging.getLogger("cloudlens.auth")

# A zip deploy replaces wwwroot, and data/ is gitignored, so anything kept here is destroyed by
# the next deploy: the account file would be rebuilt with a fresh random password and everyone
# would be locked out of an app that looks fine. AUTH_DATA_DIR points it somewhere persistent
# (on App Service, /home is the mounted share that survives).
DATA = Path(os.getenv("AUTH_DATA_DIR") or Path(__file__).resolve().parents[1] / "data")
USER_FILE = DATA / "users.json"
SECRET_FILE = DATA / ".session_secret"

COOKIE = "cost_agent_session"
ITERATIONS = 260_000

# Long enough not to nag a person working through an afternoon, short enough that a forgotten
# open laptop isn't a standing invitation.
SESSION_HOURS = float(os.getenv("AUTH_SESSION_HOURS", "12"))

# Brute-force ceiling. Five wrong passwords costs a minute, and the wait doubles, so an online
# guessing attack gets nowhere while a person who fat-fingered their password barely notices.
MAX_FAILURES = 5
LOCK_SECONDS = 60
MAX_LOCK_SECONDS = 15 * 60


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ----------------------------------------------------------------- passwords
def hash_password(password: str, *, iterations: int = ITERATIONS) -> str:
    """Return a self-describing hash, so the cost factor can be raised later without a migration."""
    if not password:
        raise ValueError("Password must not be empty.")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(iterations))
    except Exception:  # noqa: BLE001 - a malformed record is simply not a match
        return False
    return hmac.compare_digest(_b64(dk), expected)


# --------------------------------------------------------------------- users
@dataclass(frozen=True)
class User:
    name: str
    admin: bool = False
    source: str = "file"
    email: str = ""
    sid: str = ""  # server-side session id, for Entra sign-ins

    def public(self) -> dict:
        return {"name": self.name, "admin": self.admin, "email": self.email,
                "source": self.source}


class Users:
    """Accounts from `data/users.json`, overlaid with anything set in the environment.

    A file keeps it simple for a laptop; the env vars exist because a container or App Service
    deployment has no writable, persistent disk to put a file on.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or USER_FILE
        self._lock = Lock()
        self._records: dict[str, dict] = {}
        self._stamp: tuple[int, int] | None = None
        self.load()

    # -- storage
    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def refresh_if_changed(self) -> None:
        """Pick up `python -m app.auth add ...` without restarting the server.

        A colleague added while the app is serving has to be able to sign in straight away,
        so this is checked on each sign-in and each request — one `stat` of a local file,
        which is far cheaper than the PBKDF2 that follows it.
        """
        if self._file_stamp() != self._stamp:
            self.load()

    def load(self) -> None:
        records: dict[str, dict] = {}
        stamp = self._file_stamp()

        if self.path.exists():
            try:
                stored = json.loads(self.path.read_text("utf-8"))
                for name, rec in (stored.get("users") or {}).items():
                    records[name.lower()] = {
                        "name": rec.get("name", name),
                        "hash": rec["hash"],
                        "admin": bool(rec.get("admin")),
                        "source": "file",
                    }
            except Exception as exc:  # noqa: BLE001 - never let a bad file lock everyone out
                log.error("could not read %s (%s); ignoring it", self.path, exc)

        # AUTH_USERS=alice:pbkdf2_sha256$...,bob:pbkdf2_sha256$...   (":admin" suffix optional)
        for entry in _split(os.getenv("AUTH_USERS", "")):
            name, _, rest = entry.partition(":")
            if not name or not rest:
                log.warning("ignoring malformed AUTH_USERS entry for %r", name or entry[:20])
                continue
            hashed, admin = rest, False
            if rest.endswith(":admin"):
                hashed, admin = rest[: -len(":admin")], True
            records[name.strip().lower()] = {
                "name": name.strip(), "hash": hashed.strip(), "admin": admin, "source": "env",
            }

        # Quick start for a single operator: AUTH_PASSWORD is a plaintext password in the env.
        password = os.getenv("AUTH_PASSWORD")
        if password:
            name = os.getenv("AUTH_USERNAME", "admin").strip() or "admin"
            records[name.lower()] = {
                "name": name, "hash": hash_password(password), "admin": True, "source": "env",
            }

        with self._lock:
            self._records = records
            self._stamp = stamp

    def _save(self) -> None:
        """Persist only file-backed accounts — env ones are owned by the environment."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        users = {
            key: {"name": rec["name"], "hash": rec["hash"], "admin": rec["admin"]}
            for key, rec in self._records.items()
            if rec["source"] == "file"
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"users": users}, indent=2), "utf-8")
        tmp.replace(self.path)
        _restrict(self.path)
        self._stamp = self._file_stamp()

    # -- queries
    def __len__(self) -> int:
        return len(self._records)

    def list(self) -> list[User]:
        return sorted(
            (User(r["name"], r["admin"], r["source"]) for r in self._records.values()),
            key=lambda u: u.name.lower(),
        )

    def get(self, name: str) -> User | None:
        rec = self._records.get((name or "").strip().lower())
        return User(rec["name"], rec["admin"], rec["source"]) if rec else None

    def check(self, name: str, password: str) -> User | None:
        rec = self._records.get((name or "").strip().lower())
        if rec is None:
            # Hash anyway: an unknown user must not answer measurably faster than a wrong
            # password, or the response time enumerates accounts.
            hashlib.pbkdf2_hmac("sha256", (password or "").encode(), b"decoy", ITERATIONS)
            return None
        if not verify_password(password or "", rec["hash"]):
            return None
        return User(rec["name"], rec["admin"], rec["source"])

    def secret_for(self, name: str) -> str:
        """Per-user signing salt: changing a password invalidates that user's cookies."""
        rec = self._records.get((name or "").strip().lower())
        return rec["hash"] if rec else ""

    # -- mutations
    def add(self, name: str, password: str, *, admin: bool = False, replace: bool = False) -> User:
        key = name.strip().lower()
        if not key:
            raise ValueError("Username must not be empty.")
        with self._lock:
            existing = self._records.get(key)
            if existing and not replace:
                raise ValueError(f"User {name!r} already exists.")
            if existing and existing["source"] == "env":
                raise ValueError(f"User {name!r} comes from the environment; change it there.")
            self._records[key] = {
                "name": name.strip(), "hash": hash_password(password),
                "admin": admin, "source": "file",
            }
            self._save()
        return User(name.strip(), admin, "file")

    def remove(self, name: str) -> bool:
        key = (name or "").strip().lower()
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return False
            if rec["source"] == "env":
                raise ValueError(f"User {name!r} comes from the environment; remove it there.")
            del self._records[key]
            self._save()
        return True

    def bootstrap(self) -> str | None:
        """Create an admin account on first run and return the generated password, once.

        Better than a default password: it can't be guessed, and it can't be left unchanged
        because nobody but whoever reads the startup log ever knows it.
        """
        if self._records:
            return None
        password = secrets.token_urlsafe(12)
        self.add(os.getenv("AUTH_USERNAME", "admin"), password, admin=True, replace=True)
        return password


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.replace("\n", ",").split(",") if p.strip()]


def _restrict(path: Path) -> None:
    """Best effort 0600. Windows ignores chmod's group/other bits, so also strip inheritance."""
    try:
        os.chmod(path, 0o600)
        if os.name == "nt":
            os.system(f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
    except Exception:  # noqa: BLE001 - a tighter ACL is a bonus, not a requirement
        pass


# -------------------------------------------------------------------- secret
def _load_secret() -> bytes:
    """Signing key: from the env if given, else generated once and kept next to the warehouse.

    Persisting matters — a fresh key on every restart would sign everyone out each time the
    app reloads, which in development is every file save.
    """
    env = os.getenv("AUTH_SECRET", "").strip()
    if env:
        return env.encode()
    try:
        if SECRET_FILE.exists():
            existing = SECRET_FILE.read_text("utf-8").strip()
            if existing:
                return existing.encode()
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        SECRET_FILE.write_text(generated, "utf-8")
        _restrict(SECRET_FILE)
        return generated.encode()
    except OSError as exc:
        log.warning("could not persist a session secret (%s); using an in-memory one", exc)
        return secrets.token_bytes(48)


# ------------------------------------------------------------------- session
class Sessions:
    """Signed, expiring cookie values. `<payload>.<hmac>` — readable, but not forgeable."""

    def __init__(self, users: Users, secret: bytes | None = None) -> None:
        self.users = users
        self.secret = secret or _load_secret()

    def _sign(self, payload: str, user_secret: str) -> str:
        mac = hmac.new(self.secret, f"{payload}|{user_secret}".encode(), hashlib.sha256)
        return _b64(mac.digest())

    def issue(self, user: User, hours: float = SESSION_HOURS) -> str:
        body = {
            "u": user.name,
            "a": user.admin,
            "iat": int(time.time()),
            "exp": int(time.time() + hours * 3600),
        }
        payload = _b64(json.dumps(body, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload, self.users.secret_for(user.name))}"

    def verify(self, token: str | None) -> User | None:
        if not token or "." not in token:
            return None
        payload, _, signature = token.partition(".")
        try:
            body = json.loads(_unb64(payload))
        except Exception:  # noqa: BLE001
            return None

        name = body.get("u", "")
        # Signature is checked against the *current* stored hash, so a password change or a
        # deleted account takes effect on the next request rather than at cookie expiry.
        if not hmac.compare_digest(self._sign(payload, self.users.secret_for(name)), signature):
            return None
        if float(body.get("exp", 0)) < time.time():
            return None

        user = self.users.get(name)
        if user is None:
            return None
        # Trust the live record for the role, not the cookie: demoting someone must take effect.
        return user


# ------------------------------------------------------------------ throttle
class Throttle:
    """Escalating lockout, keyed by client IP and by username.

    Keyed by both because either alone is easy to sidestep: one IP spraying many usernames,
    or many IPs against one username.
    """

    def __init__(self) -> None:
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = Lock()

    def locked_for(self, *keys: str) -> int:
        now = time.time()
        with self._lock:
            waits = [self._state.get(k, (0, 0.0))[1] - now for k in keys if k]
        return int(max([0.0, *waits]))

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                if not key:
                    continue
                failures, until = self._state.get(key, (0, 0.0))
                failures = failures + 1 if until > now or failures < MAX_FAILURES else 1
                lock = 0.0
                if failures >= MAX_FAILURES:
                    lock = min(LOCK_SECONDS * 2 ** (failures - MAX_FAILURES), MAX_LOCK_SECONDS)
                self._state[key] = (failures, now + lock if lock else until)

    def succeed(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._state.pop(key, None)


# ---------------------------------------------------------------------- auth
class Auth:
    """Everything the app needs: who exists, who is signed in, and who may write.

    Two modes, chosen by configuration rather than by a flag:

    - **Entra** when `AUTH_CLIENT_ID`/`AUTH_TENANT_ID` are set. People sign in with their work
      account, and the app holds a delegated Azure token for each of them, so what they see is
      decided by their own RBAC.
    - **Local accounts** otherwise, so the app still runs on a laptop with no tenant
      configuration, and so the tests don't need a directory to talk to.
    """

    def __init__(self) -> None:
        self.enabled = os.getenv("AUTH_DISABLED", "").lower() not in ("1", "true", "yes")
        self.users = Users()
        self.sessions = Sessions(self.users)
        self.throttle = Throttle()
        self.tokens = self._load_tokens()
        self.cookie_secure = os.getenv("AUTH_COOKIE_SECURE", "").lower() in ("1", "true", "yes")

        from .entra import EntraSSO

        self.sso = EntraSSO()
        # Local passwords alongside SSO are normally a way around the SSO, so they are off by
        # default. The exception that matters: a tenant that requires admin consent, where
        # nobody can sign in at all until an administrator approves the app. This keeps the
        # app usable in the meantime, and is meant to be turned off once consent is granted.
        self.allow_local = os.getenv("AUTH_ALLOW_LOCAL", "").lower() in ("1", "true", "yes")

        if not self.enabled:
            log.warning("AUTH_DISABLED is set - every request is treated as an admin. "
                        "Do not expose this port to anyone else.")
            return

        if self.sso.enabled and not self.allow_local:
            log.info("sign-in is Microsoft Entra; local password accounts are not used")
            return
        if self.sso.enabled:
            log.warning("AUTH_ALLOW_LOCAL is set: local password accounts work alongside Entra. "
                        "Local accounts see everything the server can see, not a per-user view.")

        generated = self.users.bootstrap()
        if generated:
            name = self.users.list()[0].name
            log.warning(
                "\n%s\n  No accounts were configured, so one was created for you:\n\n"
                "      username: %s\n      password: %s\n\n"
                "  This is shown once. Change it with:  python -m app.auth passwd %s\n"
                "  Add colleagues with:                 python -m app.auth add <name>\n%s",
                "=" * 74, name, generated, name, "=" * 74,
            )
        log.info("auth enabled: %d account(s), %d api token(s)", len(self.users), len(self.tokens))

    @property
    def mode(self) -> str:
        if not self.enabled:
            return "disabled"
        return "entra" if self.sso.enabled else "local"

    @staticmethod
    def _load_tokens() -> dict[str, str]:
        """AUTH_API_TOKENS=ci:<token>,grafana:<token> — for scripts, which can't sign in."""
        tokens: dict[str, str] = {}
        for entry in _split(os.getenv("AUTH_API_TOKENS", "")):
            name, _, value = entry.partition(":")
            if name and value:
                tokens[value.strip()] = name.strip()
        return tokens

    def identify(self, cookie: str | None, authorization: str | None) -> User | None:
        """Resolve a request to a user, or None. Never raises."""
        if not self.enabled:
            return User("local", admin=True, source="disabled")

        if authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:].strip()
            for token, name in self.tokens.items():
                if hmac.compare_digest(token, presented):
                    # A machine token runs as the server, not as a person: there is no delegated
                    # Azure identity behind it, so it sees whatever the server can see.
                    return User(name, admin=True, source="token")
            return None

        if self.sso.enabled:
            from .entra import unsign

            session = self.sso.get(unsign(cookie, self.sessions.secret))
            if session is not None:
                return User(
                    name=session.name,
                    admin=self.sso.is_admin(session.email, session.oid),
                    source="entra",
                    email=session.email,
                    sid=session.sid,
                )
            if not self.allow_local:
                return None

        self.users.refresh_if_changed()
        return self.sessions.verify(cookie)

    def login(self, name: str, password: str, client: str) -> tuple[User | None, str, int]:
        """Returns (user, message, retry_after).

        `retry_after` is seconds and non-zero only when the caller is locked out. It is returned
        rather than inferred from the message: the HTTP layer needs to distinguish "wrong
        password" from "stop trying for a while", and deciding that by searching the wording
        means a copy edit silently changes the status code.
        """
        if not self.enabled:
            return User("local", admin=True, source="disabled"), "", 0
        if self.sso.enabled and not self.allow_local:
            return None, "This app uses Microsoft sign-in.", 0

        self.users.refresh_if_changed()
        keys = (f"ip:{client}", f"user:{(name or '').strip().lower()}")
        wait = self.throttle.locked_for(*keys)
        if wait:
            return None, (f"Too many attempts. Try again in {wait} "
                          f"second{'s' if wait != 1 else ''}."), wait

        user = self.users.check(name, password)
        if user is None:
            self.throttle.fail(*keys)
            # Deliberately the same message for both cases: it must not reveal which usernames
            # are real.
            return None, "Wrong username or password.", 0

        self.throttle.succeed(*keys)
        return user, "", 0

    def cookie_kwargs(self, secure: bool) -> dict:
        return {
            "httponly": True,          # a stolen cookie needs a browser, not just an XSS read
            "samesite": "lax",         # blocks the cross-site POST, so no CSRF token is needed
            "secure": secure or self.cookie_secure,
            "path": "/",
        }


# ------------------------------------------------------------------- the CLI
def _cli(argv: list[str]) -> int:
    import argparse
    import getpass

    parser = argparse.ArgumentParser(
        prog="python -m app.auth", description="Manage CloudLens accounts.")
    sub = parser.add_subparsers(dest="command")

    add = sub.add_parser("add", help="create an account")
    add.add_argument("username")
    add.add_argument("--admin", action="store_true", help="may also trigger ingests")
    add.add_argument("--password", help="prompted for if omitted")

    passwd = sub.add_parser("passwd", help="change a password")
    passwd.add_argument("username")
    passwd.add_argument("--password", help="prompted for if omitted")

    remove = sub.add_parser("remove", help="delete an account")
    remove.add_argument("username")

    sub.add_parser("list", help="show accounts")

    hash_cmd = sub.add_parser("hash", help="print a hash for AUTH_USERS, without storing it")
    hash_cmd.add_argument("--password", help="prompted for if omitted")

    args = parser.parse_args(argv)
    users = Users()

    def ask() -> str:
        given = getattr(args, "password", None)
        if given:
            return given
        first = getpass.getpass("Password: ")
        if first != getpass.getpass("Repeat: "):
            raise SystemExit("Passwords did not match.")
        if len(first) < 8:
            raise SystemExit("Use at least 8 characters.")
        return first

    try:
        if args.command == "add":
            user = users.add(args.username, ask(), admin=args.admin)
            print(f"Created {user.name}{' (admin)' if user.admin else ''} in {USER_FILE}")
        elif args.command == "passwd":
            if users.get(args.username) is None:
                raise SystemExit(f"No such user: {args.username}")
            admin = users.get(args.username).admin
            users.add(args.username, ask(), admin=admin, replace=True)
            print(f"Password changed for {args.username}. Their existing sessions are now invalid.")
        elif args.command == "remove":
            print(f"Removed {args.username}" if users.remove(args.username)
                  else f"No such user: {args.username}")
        elif args.command == "hash":
            print(hash_password(ask()))
        elif args.command == "list":
            if not len(users):
                print("No accounts yet. The app will create one the next time it starts.")
            for user in users.list():
                role = "admin" if user.admin else "member"
                print(f"  {user.name:<24} {role:<8} from {user.source}")
        else:
            parser.print_help()
            return 1
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv[1:]))

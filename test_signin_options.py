"""Does AUTH_HIDE_WORK_SIGNIN hide the work-account button, and only where it is set?"""
import asyncio
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

TENANT = "99999999-8888-7777-6666-555555555555"


def fresh_sso(**env):
    """Build an Sso with a given environment, since the flag is read in __init__."""
    keys = ["AUTH_CLIENT_ID", "AUTH_TENANT_ID", "AUTH_DELEGATED_ARM",
            "AUTH_HOME_TENANT", "AUTH_HIDE_WORK_SIGNIN"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        import importlib

        from app import entra
        importlib.reload(entra)
        return entra.EntraSSO()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    base = {"AUTH_CLIENT_ID": "abc", "AUTH_TENANT_ID": "organizations",
            "AUTH_DELEGATED_ARM": "true", "AUTH_HOME_TENANT": TENANT}

    print("=" * 72)
    print("the flag is off unless set")
    check("default is show", fresh_sso(**base).hide_work_button is False)
    check("empty string is show", fresh_sso(**base, AUTH_HIDE_WORK_SIGNIN="").hide_work_button is False)
    check("'false' is show", fresh_sso(**base, AUTH_HIDE_WORK_SIGNIN="false").hide_work_button is False)

    print("\n" + "=" * 72)
    print("the flag turns on for the usual spellings")
    for v in ("true", "True", "1", "yes"):
        check(f"'{v}' hides", fresh_sso(**base, AUTH_HIDE_WORK_SIGNIN=v).hide_work_button is True)

    print("\n" + "=" * 72)
    print("it changes what is advertised, not what is enforced")
    sso = fresh_sso(**base, AUTH_HIDE_WORK_SIGNIN="true")
    check("the multi-tenant authority is untouched", sso.multi_tenant is True)
    check("delegated ARM is untouched", sso.delegated is True)
    check("the home tenant is still accepted", sso.home_tenant == TENANT)

    print("\n" + "=" * 72)
    print("the login page is told, only when there is somewhere else to send people")
    import inspect

    from app import main as appmain
    src = inspect.getsource(appmain.me)
    check("hide_work_signin is exposed", "hide_work_signin" in src)
    check("it sits inside the home_tenant branch",
          src.index("home_tenant") < src.index("hide_work_signin"))

    page = (ROOT / "web" / "login.html").read_text(encoding="utf-8")
    check("the page reads the flag", "hide_work_signin" in page)
    check("hiding requires a tenant link to fall back on",
          "me.hide_work_signin && tenantHref" in page)
    check("the button is still rendered, pointed at the tenant",
          "btn.href = hideWork ? tenantHref" in page)
    check("the personal-account line is dropped when it would duplicate the button",
          "if (tenantHref && !hideWork)" in page)

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""Does CostAgent work through the API-key path, and does the endpoint helper behave?"""
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

# The endpoint logic is pure and always testable. Reaching a real model needs a key, which is
# not in the repo and must not be -- so that half is skipped rather than failed when there is
# none. A test that fails for want of a credential trains people to ignore a red suite.
KEY = os.getenv("AZURE_AI_API_KEY", "").strip()
if not KEY:
    keyfile = Path(os.environ.get("TEMP", "."), "cl_key.txt")
    if keyfile.exists():
        KEY = keyfile.read_text().strip()

ACCOUNT = os.getenv("TEST_AI_ACCOUNT", "https://cloudlens-ai-452f1h.cognitiveservices.azure.com")
PROJECT = f"{ACCOUNT}/api/projects/cloudlens-proj"
MODEL = os.getenv("TEST_AI_MODEL", "gpt-4.1-mini")


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    os.environ["PROJECT_ENDPOINT"] = PROJECT
    os.environ["MODEL_DEPLOYMENT_NAME"] = "gpt-4.1-mini"

    from app.agent import CostAgent, _account_endpoint

    print("=" * 72)
    print("endpoint helper")
    check("a project endpoint is reduced to the account",
          _account_endpoint(PROJECT) == ACCOUNT + "/", _account_endpoint(PROJECT))
    check("an account endpoint passes through",
          _account_endpoint(ACCOUNT) == ACCOUNT + "/")
    check("a trailing slash does not double up",
          _account_endpoint(ACCOUNT + "/") == ACCOUNT + "/")
    check("services.ai style is handled",
          _account_endpoint("https://x.services.ai.azure.com/api/projects/p")
          == "https://x.services.ai.azure.com/")
    check("empty input does not explode", _account_endpoint("") == "/")

    print("\n" + "=" * 72)
    print("key path end to end")
    if not KEY:
        print("  SKIP no API key available (set AZURE_AI_API_KEY to run this part)")
    else:
        os.environ["AZURE_AI_API_KEY"] = KEY
        os.environ["MODEL_DEPLOYMENT_NAME"] = MODEL
        agent = CostAgent()
        try:
            client = await agent._openai()
            check("a client is returned", client is not None, type(client).__name__)
            check("the key client points at the account, not the project",
                  str(client.base_url).rstrip("/").endswith("cognitiveservices.azure.com")
                  or "/api/projects/" not in str(client.base_url), str(client.base_url))
            r = await client.chat.completions.create(
                model=agent.model,
                messages=[{"role": "user", "content": "Reply with exactly: AGENT KEY OK"}],
                max_tokens=20,
            )
            said = r.choices[0].message.content.strip()
            check("the model answers through CostAgent", "AGENT KEY OK" in said, said)
        except Exception as exc:
            check("the model answers through CostAgent", False,
                  f"{type(exc).__name__}: {exc}"[:200])
        finally:
            await agent.close()

    print("\n" + "=" * 72)
    print("no key means the Entra path is chosen")
    os.environ.pop("AZURE_AI_API_KEY", None)
    agent2 = CostAgent()
    try:
        c2 = await agent2._openai()
        check("Entra path still builds a client", c2 is not None, type(c2).__name__)
    except Exception as exc:
        check("Entra path still builds a client", False, str(exc)[:160])
    finally:
        try:
            await agent2.close()
        except Exception:
            pass

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

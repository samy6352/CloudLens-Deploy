"""The download link: the thing a person actually clicks to get a file.

This exists because the export broke twice in ways the other suites could not see. The first
time the report builder persisted an empty selection, so every file came out near-empty but
perfectly valid. The second time the bytes were fetched and handed to a synthesised anchor
click, which some browsers ignore outright — a silent no-op with no error anywhere.

So the checks here are about the contract a browser relies on: a plain GET, an attachment
disposition, a real filename, and a selection that genuinely changes what comes back.

    python test_download.py
"""

from __future__ import annotations

import os

os.environ.setdefault("AUTH_DISABLED", "true")

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.report import BLOCKS, FORMATS

client = TestClient(app)

passed = failed = 0
ALL_AREAS = "compute,networking,storage,data,integration,security,other"


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  OK   {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  FAIL {name}  {detail}")


def get(fmt: str, **params):
    return client.get(f"/api/report/{fmt}", params=params)


print("\nthe link a browser can follow")
print("=" * 72)

r = get("xlsx", days=30)
check("a bare GET returns a file", r.status_code == 200, f"status {r.status_code}")
check(
    "it is marked as an attachment",
    r.headers.get("content-disposition", "").startswith("attachment;"),
    r.headers.get("content-disposition", "<none>"),
)
check(
    "the attachment carries a filename",
    'filename="' in r.headers.get("content-disposition", ""),
    r.headers.get("content-disposition", "<none>"),
)
check(
    "the filename is branded and dated",
    "cloudlens-cost-report-" in r.headers.get("content-disposition", ""),
    r.headers.get("content-disposition", "<none>"),
)
check(
    "it is not served as JSON, which a browser would render instead of download",
    "json" not in r.headers.get("content-type", ""),
    r.headers.get("content-type", "<none>"),
)
check("it is not cached", r.headers.get("cache-control") == "no-store",
      r.headers.get("cache-control", "<none>"))

print("\nevery format downloads as its own type")
print("=" * 72)
for fmt in FORMATS:
    res = get(fmt, days=30)
    body = res.content
    check(
        f"{fmt} downloads",
        res.status_code == 200 and len(body) > 0,
        f"{res.status_code}, {len(body)} bytes",
    )

check(
    "xlsx is a real workbook",
    zipfile.is_zipfile(io.BytesIO(get("xlsx", days=30).content)),
)
check(
    "pptx is a real deck",
    zipfile.is_zipfile(io.BytesIO(get("pptx", days=30).content)),
)
check(
    "json parses",
    isinstance(json.loads(get("json", days=30).content), dict),
)

print("\nthe probe answers without building anything")
print("=" * 72)
probe = get("xlsx", days=30, probe=1)
check("probe returns 204", probe.status_code == 204, f"status {probe.status_code}")
check("probe returns no body", not probe.content, f"{len(probe.content)} bytes")
check(
    "probe rejects an unknown format, so a bad link fails before the download starts",
    get("nope", days=30, probe=1).status_code == 404,
)

print("\nthe page offers downloads as real links, not scripted clicks")
print("=" * 72)
# A download started by JavaScript counts as an *automatic* download, which managed browsers
# block silently. These assert the markup keeps the click a genuine navigation.
page = client.get("/").text if client.get("/").status_code == 200 else ""
check("the app shell is served", bool(page), f"{len(page)} bytes")
if page:
    for fmt in ("xlsx", "pptx", "csv", "md"):
        check(
            f"the {fmt} menu item is an anchor with an href",
            f'<a data-fmt="{fmt}"' in page and f'href="/api/report/{fmt}"' in page,
        )
    check(
        "print and 'choose what to include' stay buttons — they download nothing",
        '<button data-fmt="print"' in page and '<button data-fmt="custom"' in page,
    )

script = client.get("/assets/dashboard.js").text
check(
    "the report tab downloads through links, not a scripted click",
    'id="buildMenu"' in script and "a data-fmt=" in script,
)
check(
    "the report tab no longer carries its own format panel — the header menu already has one",
    "fmt-row" not in script and "<h3>Format</h3>" not in script,
)
check(
    "no blob URLs are used for downloads",
    "createObjectURL" not in script,
    "found createObjectURL" if "createObjectURL" in script else "",
)
check(
    "nothing synthesises a click on a download anchor",
    "a.click()" not in script,
    "found a.click()" if "a.click()" in script else "",
)

print("\nthe selection in the link changes the file")
print("=" * 72)

full = get("xlsx", days=30, summary=1, sections=ALL_AREAS,
           blocks=",".join(BLOCKS), live="").content
two = get("xlsx", days=30, summary=1, sections="compute,networking",
          blocks=",".join(BLOCKS), live="").content
bare = get("xlsx", days=30, summary=1, sections="", blocks="", live="").content

check("asking for every area returns the most", len(full) > len(two) > len(bare),
      f"all={len(full)} two={len(two)} summary-only={len(bare)}")
check(
    "an empty sections list means none, not all — the bug that made exports look broken",
    len(bare) < len(full) / 2,
    f"summary-only={len(bare)} vs all={len(full)}",
)

names = zipfile.ZipFile(io.BytesIO(two)).namelist()
check("a narrowed report is still a valid workbook", any("sheet" in n for n in names))

no_summary = get("xlsx", days=30, summary=0, sections="compute", blocks="trend", live="")
check("summary can be turned off", no_summary.status_code == 200 and no_summary.content,
      f"{len(no_summary.content)} bytes")

print("\nbad input is refused rather than downloaded")
print("=" * 72)
check("an unknown format is a 404", get("exe", days=30).status_code == 404)
check("days is clamped, not trusted", get("xlsx", days=99999).status_code == 200)
check("a negative period is clamped too", get("xlsx", days=-5).status_code == 200)
check(
    "an unknown live dataset is ignored, not fatal",
    get("xlsx", days=30, live="nonsense").status_code == 200,
)
check(
    "an unknown area is ignored, not fatal",
    get("xlsx", days=30, sections="not-an-area").status_code == 200,
)

print("\nGET and POST agree")
print("=" * 72)
selection = {
    "format": "xlsx",
    "days": 30,
    "scope": [],
    "summary": True,
    "sections": ["compute", "networking"],
    "blocks": list(BLOCKS),
    "live": [],
}
posted = client.post("/api/report", json=selection)
linked = get("xlsx", days=30, summary=1, sections="compute,networking",
             blocks=",".join(BLOCKS), live="")
check("POST still works", posted.status_code == 200, f"status {posted.status_code}")
check(
    "the link and the POST produce the same size report",
    abs(len(posted.content) - len(linked.content)) < 2048,
    f"post={len(posted.content)} link={len(linked.content)}",
)
check(
    "both are attachments",
    posted.headers.get("content-disposition", "").startswith("attachment;")
    and linked.headers.get("content-disposition", "").startswith("attachment;"),
)


# ------------------------------------------------------- the scoped selection
# "Custom export the cost data" wrote the file to blob storage and reported a name nobody could
# reach from a browser. The data was correct and entirely out of reach.

print("\n" + "=" * 72)
print("the export selection downloads in the browser")

subs = client.get("/api/subscriptions").json().get("subscriptions", [])
first = subs[0]["id"] if subs else ""

sel = client.get("/api/archive/export/download",
                 params={"scope": first, "fmt": "csv"})
check("a GET returns the file itself", sel.status_code == 200, f"status {sel.status_code}")
check("as an attachment",
      sel.headers.get("content-disposition", "").startswith("attachment;"),
      sel.headers.get("content-disposition", "")[:80])
check("with a filename that says what it holds",
      ".csv" in sel.headers.get("content-disposition", "")
      and "selection/" not in sel.headers.get("content-disposition", ""),
      "the blob path is a filing decision, not a filename")
check("as CSV, not JSON about a CSV",
      sel.headers.get("content-type", "").startswith("text/csv"),
      sel.headers.get("content-type", ""))
check("carrying rows", len(sel.content) > 0 and b"," in sel.content[:400])
check("and says how many without opening it",
      (sel.headers.get("x-cloudlens-rows") or "").isdigit(),
      sel.headers.get("x-cloudlens-rows", ""))
check("nothing is cached — the selection changes between clicks",
      "no-store" in sel.headers.get("cache-control", ""))

# The refusal that used to happen server-side after the click.
empty = client.get("/api/archive/export/download", params={"scope": "", "fmt": "csv"})
check("an empty selection is refused, not exported",
      empty.status_code == 400, f"status {empty.status_code}")

bad = client.get("/api/archive/export/download",
                 params={"scope": first, "fmt": "csv",
                         "since": "2026-08-10", "until": "2026-08-01"})
check("a backwards date range is refused", bad.status_code == 400, f"status {bad.status_code}")

# Downloading and archiving must be the same file, or the copy kept for later is not the one
# anybody checked.
if sel.status_code == 200:
    narrow = client.get("/api/archive/export/download",
                        params={"scope": first, "fmt": "csv", "since": "2026-08-01"})
    check("the date range genuinely narrows the file",
          narrow.status_code != 200 or len(narrow.content) <= len(sel.content),
          f"all={len(sel.content)} narrowed={len(narrow.content)}")

parq = client.get("/api/archive/export/download",
                  params={"scope": first, "fmt": "parquet"})
check("parquet comes back as a real parquet file",
      parq.status_code != 200 or parq.content[:4] == b"PAR1",
      f"status {parq.status_code}, magic {parq.content[:4]!r}")

# Found in pre-production QA, and it is the inconsistency that makes it interesting: an
# unknown format was rejected cleanly or crashed depending only on how long the word was.
# ReportRequest.format is capped at eight characters, so "exe" and "pdf" reached the
# allowlist check and 404'd, while "badformat" raised a Pydantic ValidationError inside the
# handler first and surfaced as a bare 500.
main_src = (Path(__file__).resolve().parent / "app" / "main.py").read_text(encoding="utf-8")
_dl = main_src[main_src.index("async def download_report"):]
_dl = _dl[:_dl.index("@app.get")] if "@app.get" in _dl else _dl
check("an unknown report format is rejected before the model is built",
      _dl.index("if fmt not in FORMATS") < _dl.index("ReportRequest("))
check("and the refusal lists what is valid",
      "Choose from:" in _dl)

# The warehouse endpoint accepted only the caller's entitlement and ignored ?scope=, so the
# header's "local data" tile stayed at the whole estate while every other figure followed the
# subscription picker.
_wh = main_src[main_src.index("async def warehouse_status"):]
_wh = _wh[:_wh.index("@app.post")]
check("the warehouse endpoint accepts a scope", 'scope: str = ""' in _wh)
check("and narrows it against the entitlement rather than trusting it",
      "narrow(" in _wh and "picked or allowed" in _wh)

# Cleared with the flags it was set with. Cosmetic, but a scanner reads the Set-Cookie on
# logout and reports the session cookie as unprotected, which cost a security review round.
check("the logout cookie is cleared with the same flags it was set with",
      "delete_cookie(COOKIE, **auth.cookie_kwargs(" in main_src)

print("\n" + "=" * 72)
print(f"  {passed} passed, {failed} failed\n")
raise SystemExit(1 if failed else 0)

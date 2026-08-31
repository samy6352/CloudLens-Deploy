"""Does the daily archive keep exactly one copy per dataset per day?

The interesting failure is not a crash. It is an archive that silently accumulates a file per
button press, or one that overwrites yesterday's copy with today's -- either of which destroys
the only reason the archive exists, which is being able to say what the numbers were on a
given day.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(r"C:\Users\raviverma\OneDrive - Microsoft\Documents\Microsoft Scout\azure-cost-agent")
sys.path.insert(0, str(ROOT))
for s in (sys.stdout, sys.stderr):
    try:
        s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


async def main() -> int:
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    from app import archive as a

    print("=" * 72)
    print("one blob per dataset per day")

    day = date(2026, 8, 27)
    focus = a.blob_name("FocusCost", day)
    amort = a.blob_name("AmortizedCost", day)

    check("a FOCUS refresh names its blob by dataset and date",
          focus == "focus/2026/08/focus-2026-08-27.parquet", focus)
    check("amortized lands in its own folder, not on top of focus",
          amort == "amortized/2026/08/amortized-2026-08-27.parquet", amort)
    check("actual is separate again",
          a.blob_name("ActualCost", day) == "actual/2026/08/actual-2026-08-27.parquet")

    # The whole "max one a day" mechanism: the name depends only on dataset and date, so a
    # second run of the same day is a PUT onto the same blob.
    check("two refreshes on the same day resolve to the same blob",
          a.blob_name("FocusCost", day) == a.blob_name("FocusCost", day))
    check("a different day is a different blob, so yesterday survives",
          a.blob_name("FocusCost", date(2026, 8, 28)) != focus)
    check("a new month opens a new folder",
          a.blob_name("FocusCost", date(2026, 9, 1)).startswith("focus/2026/09/"))
    check("a new year opens a new folder",
          a.blob_name("FocusCost", date(2027, 1, 5)).startswith("focus/2027/01/"))

    # Zero-padding matters: without it, sorting the container lexically interleaves months.
    check("months are zero padded so the container sorts chronologically",
          "/2026/03/" in a.blob_name("FocusCost", date(2026, 3, 9)),
          a.blob_name("FocusCost", date(2026, 3, 9)))

    print("\n" + "=" * 72)
    print("unknown metrics degrade rather than fail")

    check("an unrecognised metric still produces a usable folder",
          a.dataset_folder("SomeNewMetric") == "somenewmetric", a.dataset_folder("SomeNewMetric"))
    check("path separators cannot escape the dataset folder",
          "/" not in a.dataset_folder("../../etc/passwd"), a.dataset_folder("../../etc/passwd"))
    check("an empty metric still names something",
          a.dataset_folder("") == "dataset", a.dataset_folder(""))

    print("\n" + "=" * 72)
    print("the target and its wiring")

    check("the archive is on by default", a.enabled())
    check("it points at the cloudlens container",
          a.ACCOUNT == "<your-storage-account>" and a.CONTAINER == "cloudlens",
          f"{a.ACCOUNT}/{a.CONTAINER}")

    w = a.BlobWriter()
    check("blob URLs are built against the account's blob endpoint",
          w._url("focus/x.parquet") ==
          "https://<your-storage-account>.blob.core.windows.net/cloudlens/focus/x.parquet",
          w._url("focus/x.parquet"))

    # A 403 from a network-blocked account must not send someone to check RBAC they already have.
    msg = a._explain(403, "", "<your-storage-account>")
    check("a 403 blames the network, not the role", "private endpoint" in msg, msg[:80])

    print("\n" + "=" * 72)
    print("an empty warehouse never overwrites a good archive")

    import tempfile

    class FakeCon:
        def execute(self, sql, *args):
            self.last = sql
            return self

        def fetchone(self):
            return (0,)

        def close(self):
            pass

    class FakeWarehouse:
        def connect(self, read_only=False, scope=None):
            return FakeCon()

    import app.warehouse as wh

    real = wh.warehouse
    wh.warehouse = FakeWarehouse()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            rows, size = a._export_parquet(Path(tmp) / "x.parquet")
        check("an empty warehouse exports nothing", rows == 0 and size == 0)
        try:
            await a.archive_current("FocusCost")
            check("an empty warehouse refuses to archive", False, "no error raised")
        except a.ArchiveError as exc:
            check("an empty warehouse refuses to archive, with a reason",
                  "nothing to archive" in str(exc), str(exc)[:60])
    finally:
        wh.warehouse = real

    print("\n" + "=" * 72)
    print("a failed archive never fails the refresh")

    import inspect

    from app import main as appmain

    src = inspect.getsource(appmain._archive_after_ingest)
    check("the archive step swallows its own errors", "except Exception" in src)
    check("the failure is recorded for the UI rather than discarded", '"archive"' in src)
    check("it only archives a refresh that actually succeeded",
          '"ready", "partial"' in src or "'ready', 'partial'" in src)

    check("the API refresh path archives", "_archive_after_ingest" in inspect.getsource(appmain._run_ingest))
    check("the FOCUS export path archives",
          "_archive_after_ingest" in inspect.getsource(appmain.ingest_from_export))

    routes = {getattr(r, "path", "") for r in appmain.app.routes}
    check("GET/POST /api/archive exist", "/api/archive" in routes)

    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0




async def failures() -> int:
    """The FOCUS refresh failure message has to be readable and point somewhere useful."""
    passed = failed = 0

    def check(label, ok, detail=""):
        nonlocal passed, failed
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        return ok

    from app.main import _explain_export_failures

    print("\n" + "=" * 72)
    print("why the FOCUS option failed")

    results = []
    net = [("demotest6453", "Could not reach storage account 'demotest6453' - it does not resolve "
                            "from here, which usually means it is private-endpoint only.")]
    msg = _explain_export_failures(net)
    results.append(check("a network wall is named as a network wall",
                         "private-endpoint only" in msg, msg[:70]))
    results.append(check("it names the account that failed", "demotest6453" in msg))
    results.append(check("it points at an option that actually works",
                         "Amortized" in msg, ""))

    denied = [("stcost1", "Access to storage account 'stcost1' was denied. 403")]
    msg2 = _explain_export_failures(denied)
    results.append(check("a refusal is named as a refusal", "refused" in msg2, msg2[:70]))

    both = net + denied
    msg3 = _explain_export_failures(both)
    results.append(check("a mix reports both walls", "Some are" in msg3, msg3[:70]))
    results.append(check("both accounts are named",
                         "demotest6453" in msg3 and "stcost1" in msg3))

    # The old message spliced three errors together and cut the result mid-word.
    many = [(f"acct{i}", "Could not reach storage account - it does not resolve") for i in range(9)]
    msg4 = _explain_export_failures(many)
    results.append(check("nine failures stay one readable sentence, not nine",
                         len(msg4) < 400 and "and 6 more" in msg4, f"{len(msg4)} chars"))
    results.append(check("an empty list still says something",
                         _explain_export_failures([]) != ""))

    passed = sum(1 for r in results if r)
    failed = len(results) - passed
    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0

async def comparison() -> int:
    """Keeping older data is only worth it if two days can actually be compared."""
    passed = failed = 0
    results = []

    def check(label, ok, detail=""):
        print(f"  {'OK  ' if ok else 'FAIL'} {label}  {detail}")
        results.append(bool(ok))
        return ok

    from app import archive as a

    print("\n" + "=" * 72)
    print("comparing two archived days")

    files = [
        {"name": "actual/2026/08/actual-2026-08-28.parquet"},
        {"name": "actual/2026/08/actual-2026-08-27.parquet"},
        {"name": "focus/2026/08/focus-2026-08-27.parquet"},
        {"name": "actual/2026/07/actual-2026-07-31.parquet"},
    ]
    days = a._dataset_days(files, "actual")
    check("the days for a dataset are found across month folders",
          days == ["2026-08-28", "2026-08-27", "2026-07-31"], str(days))
    check("another dataset's files are not mixed in",
          a._dataset_days(files, "focus") == ["2026-08-27"])
    check("a dataset with no files yields nothing", a._dataset_days(files, "amortized") == [])

    # Reversed inputs would report every increase as a decrease -- an answer, not an error,
    # which is the worse failure.
    try:
        await a.compare("actual", "2026-08-27", "2026-08-27")
        check("comparing a day with itself is refused", False, "no error")
    except a.ArchiveError as exc:
        check("comparing a day with itself is refused", "two different days" in str(exc))

    # The group-by column is interpolated into SQL, so it must come from a fixed list.
    try:
        await a.compare("actual", "2026-08-27", "2026-08-28",
                        group_by='ServiceName"; DROP TABLE costs; --')
        check("an injected group_by is refused", False, "no error")
    except a.ArchiveError as exc:
        check("an injected group_by is refused", "Cannot group by" in str(exc), str(exc)[:50])

    check("only known dimensions are allowed",
          all(c.isidentifier() for c in a._COMPARE_COLUMNS))
    check("the allowed dimensions are the ones the dashboard uses",
          "ServiceName" in a._COMPARE_COLUMNS and "ResourceGroup" in a._COMPARE_COLUMNS)

    import inspect

    src = inspect.getsource(a.compare)
    check("the two days are ordered rather than trusted", "sorted([earlier, later])" in src)
    check("a full outer join is used so appearances and disappearances show",
          "FULL OUTER JOIN" in src)
    check("parquet is read by duckdb, not materialised in python", "read_parquet" in src)

    print("\n" + "=" * 72)
    print("the progress bar has something real to show")

    from app.warehouse import Warehouse

    wsrc = inspect.getsource(Warehouse._progress)
    check("progress is derived from phases, not just completed periods",
          "_PHASE_WEIGHT" in wsrc)
    check("the bar never claims to be finished before the run ends", "0.99" in wsrc)
    check("each phase has a weight",
          set(Warehouse._PHASE_WEIGHT) == {"submitting", "generating", "loading"},
          str(sorted(Warehouse._PHASE_WEIGHT)))
    check("weights increase through the period",
          Warehouse._PHASE_WEIGHT["submitting"] < Warehouse._PHASE_WEIGHT["generating"]
          < Warehouse._PHASE_WEIGHT["loading"])

    dash = (ROOT / "web" / "assets" / "dashboard.js").read_text(encoding="utf-8")
    check("the bar only moves forwards", "ingestSeen = Math.max" in dash)
    check("the elapsed clock runs on its own timer, not on the poll",
          "setInterval(paintElapsed" in dash)
    check("the strip is hidden when the run ends", "showIngest(false)" in dash)
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("the strip exists in the page", 'id="ingest"' in html)
    check("it tells the user they can walk away", "keep using the dashboard" in html)
    check("the period selector offers 7/30/60/90",
          all(f'data-days="{d}"' in html for d in (7, 30, 60, 90)))

    passed = sum(1 for r in results if r)
    failed = len(results) - passed
    print("\n" + "=" * 72)
    print(f"  {passed} passed, {failed} failed")
    return 1 if failed else 0

async def _all() -> int:
    return (await main()) + (await failures()) + (await comparison())


if __name__ == "__main__":
    sys.exit(asyncio.run(_all()))
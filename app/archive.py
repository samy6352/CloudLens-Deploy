"""Keep a dated copy of every dataset the warehouse loads.

`exports.py` reads Cost Management's own exports and states in its opening paragraph that
nothing in it writes to storage. That guarantee is worth keeping, so the write lives here --
the same reason `budgets.py` is separate from `cost.py`.

What this is for: the warehouse is a working set, not a record. A refresh *replaces* the
periods it loads, so yesterday's numbers are gone the moment today's arrive -- and Azure's own
cost data is restated for several days after the fact. Without a copy taken at the time, the
question "what did we think this cost when we reported it?" has no answer.

Three decisions shape the naming:

  * **One blob per dataset per day.** The name is derived from the dataset and today's date
    and nothing else, so a second refresh on the same day overwrites the first rather than
    accumulating near-identical copies. Someone pressing Refresh four times in an afternoon is
    correcting something, not asking for four archives.

  * **The date is the day the archive was taken**, not the last day of data in it. Cost data is
    restated, so two archives taken a week apart can cover the same period and differ -- which
    is exactly the fact this archive exists to preserve. Naming by data coverage would make the
    second one overwrite the first and destroy it.

  * **Parquet, not CSV.** It is the format FOCUS exports already use, `exports.py` can read it
    back, and it is roughly a tenth the size -- which matters for a file written every day and
    kept indefinitely.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import httpx

log = logging.getLogger("cloudlens.archive")

BLOB_VERSION = "2021-12-02"
BLOB_RESOURCE = "https://storage.azure.com"

# Where archives go. Both are deployment-specific, and the account has no sensible default: a
# name baked in here would be someone else's storage account, so an unconfigured deployment
# would try to write there and be refused. Empty means the archive is simply off, which
# `enabled()` below reports honestly, and the deployment templates set it.
ACCOUNT = os.getenv("ARCHIVE_ACCOUNT", "").strip()
CONTAINER = os.getenv("ARCHIVE_CONTAINER", "cloudlens").strip()

# A refresh that loaded nothing must not overwrite a good archive with an empty one.
MIN_ROWS = 1

# The datasets a refresh can produce. The key is what the UI asks for; the value is the folder.
DATASETS = {
    "FocusCost": "focus",
    "AmortizedCost": "amortized",
    "ActualCost": "actual",
}

_SAFE = re.compile(r"[^a-z0-9._-]+")


class ArchiveError(RuntimeError):
    """Something went wrong writing the archive, phrased for whoever pressed Refresh."""


def enabled() -> bool:
    """Off when no account is configured, so a deployment without storage still refreshes."""
    return bool(ACCOUNT and CONTAINER)


def dataset_folder(metric: str) -> str:
    """Folder for a metric. Unknown metrics get a sanitised folder rather than an error --
    failing an archive because a new metric appeared would block the refresh that produced it."""
    if metric in DATASETS:
        return DATASETS[metric]
    return _SAFE.sub("-", (metric or "dataset").lower()).strip("-") or "dataset"


def blob_name(metric: str, when: date | None = None) -> str:
    """`focus/2026/08/focus-2026-08-27.parquet`.

    Foldered by year and month because a container with a thousand flat files is unusable in
    the portal, and because lifecycle rules to tier or expire old data are written against
    prefixes.
    """
    day = when or datetime.now(timezone.utc).date()
    folder = dataset_folder(metric)
    return f"{folder}/{day.year:04d}/{day.month:02d}/{folder}-{day.isoformat()}.parquet"


class BlobWriter:
    """The smallest client that can put a blob with a managed identity.

    Deliberately not the azure-storage-blob SDK: `exports.py` already talks to the blob REST
    API directly with httpx, and adding a second, heavier way to reach the same service would
    mean two connection pools, two auth paths and two sets of failure modes to reason about.
    """

    def __init__(self, account: str = ACCOUNT, container: str = CONTAINER) -> None:
        self.account = account
        self.container = container
        self._token: str | None = None
        self._credential: Any = None

    async def _headers(self) -> dict[str, str]:
        if self._token is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            got = await self._credential.get_token(f"{BLOB_RESOURCE}/.default")
            self._token = got.token
        return {"x-ms-version": BLOB_VERSION, "Authorization": "Bearer " + str(self._token)}

    def _url(self, path: str = "", query: str = "") -> str:
        base = f"https://{self.account}.blob.core.windows.net/{self.container}"
        if path:
            base += "/" + path.lstrip("/")
        return base + (f"?{query}" if query else "")

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def ensure_container(self, client: httpx.AsyncClient) -> None:
        """Create the container if missing. 409 means it already exists, which is success."""
        r = await client.put(self._url(query="restype=container"), headers=await self._headers())
        if r.status_code in (201, 409):
            return
        raise ArchiveError(_explain(r.status_code, r.text, self.account))

    async def put(self, name: str, data: bytes, content_type: str) -> dict[str, Any]:
        """Write one blob, replacing any blob of the same name.

        A plain PUT is an overwrite, which is the whole mechanism behind "max one a day": the
        name carries the date, so the second run of the day lands on the first.
        """
        headers = {
            **(await self._headers()),
            "x-ms-blob-type": "BlockBlob",
            "Content-Type": content_type,
        }
        async with httpx.AsyncClient(timeout=300) as client:
            await self.ensure_container(client)
            r = await client.put(self._url(name), headers=headers, content=data)
            if r.status_code not in (201, 202):
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            return {
                "name": name,
                "bytes": len(data),
                "url": self._url(name),
                "etag": r.headers.get("ETag"),
                "written": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

    async def get(self, name: str) -> bytes:
        """One blob's bytes. Used to read an archived snapshot back for comparison."""
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(self._url(name), headers=await self._headers())
            if r.status_code == 404:
                raise ArchiveError(f"No archive at {name}.")
            if r.status_code != 200:
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            return r.content

    async def list(self, prefix: str = "", limit: int = 500) -> list[dict[str, Any]]:
        """Blobs under a prefix, newest first. Used to show what the archive already holds."""
        out: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=90) as client:
            q = f"restype=container&comp=list&maxresults={min(limit, 1000)}"
            if prefix:
                q += f"&prefix={prefix}"
            r = await client.get(self._url(query=q), headers=await self._headers())
            if r.status_code != 200:
                raise ArchiveError(_explain(r.status_code, r.text, self.account))
            for m in re.finditer(r"<Blob>(.*?)</Blob>", r.text, re.S):
                blob = m.group(1)
                out.append({
                    "name": _tag(blob, "Name"),
                    "size": int(_tag(blob, "Content-Length") or 0),
                    "modified": _tag(blob, "Last-Modified"),
                })
        out.sort(key=lambda b: b["name"] or "", reverse=True)
        return out[:limit]


def _tag(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else None


def _explain(status: int, body: str, account: str) -> str:
    """Turn the failures that actually happen into something someone can act on.

    The 403 here is nearly always the network, not the role: in a tenant that forces
    `publicNetworkAccess=Disabled` the blob endpoint refuses before RBAC is even consulted, and
    telling someone to check their permissions sends them to look at the one thing that is
    already correct.
    """
    if status == 403:
        return (
            f"Storage account '{account}' refused the write. If this app has Storage Blob Data "
            "Contributor, the cause is the network rather than the role: the account has public "
            "access disabled and needs a private endpoint reachable from the app's VNet."
        )
    if status == 404:
        return f"Container not found on '{account}', and it could not be created."
    if status == 401:
        return f"Not authorized to write to '{account}'. It needs Storage Blob Data Contributor."
    return f"Storage returned {status}: {body[:200]}"


def _where(scope: list[str] | None, since: str | None, until: str | None) -> str:
    """The row filter shared by every export path.

    Subscription ids and dates are the only things interpolated, and both are validated by the
    caller before they get here — ids come from the scope the person is already permitted to
    see, dates are checked against ISO format. Neither reaches SQL unexamined.
    """
    clauses: list[str] = []
    if scope:
        ids = ", ".join(f"'{s}'" for s in scope)
        clauses.append(f'("SubAccountId" IN ({ids}) OR "SubAccountName" IN ({ids}))')
    if since:
        clauses.append(f"\"ChargePeriodStart\" >= DATE '{since}'")
    if until:
        clauses.append(f"\"ChargePeriodStart\" <= DATE '{until}'")
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _check_day(value: str | None, label: str) -> str | None:
    """A date, or a refusal. Interpolated into SQL, so it is never taken on trust."""
    if not value:
        return None
    text = str(value).strip()[:10]
    try:
        date.fromisoformat(text)
    except ValueError:
        raise ArchiveError(f"{label} must be a date like 2026-08-01.") from None
    return text


def _write_rows(destination: Path, fmt: str, *, source_sql: str,
                scope: list[str] | None, since: str | None, until: str | None,
                con: Any) -> tuple[int, int]:
    """Copy the selected rows out, in the requested format.

    `source_sql` is either the warehouse's `costs` table or a `read_parquet(...)` over an
    archived snapshot — the filtering and the writing are identical either way, which is the
    point of separating them.
    """
    where = _where(scope, since, until)
    rows = con.execute(f"SELECT count(*) FROM {source_sql}{where}").fetchone()[0]
    if rows < MIN_ROWS:
        return 0, 0
    target = destination.as_posix().replace("'", "''")
    options = ("FORMAT PARQUET, COMPRESSION ZSTD" if fmt == "parquet"
               else "FORMAT CSV, HEADER, DELIMITER ','")
    con.execute(
        f'COPY (SELECT * FROM {source_sql}{where} ORDER BY "ChargePeriodStart") '
        f"TO '{target}' ({options})"
    )
    return rows, destination.stat().st_size


def _export_parquet(destination: Path, scope: list[str] | None = None) -> tuple[int, int]:
    """Write the warehouse's cost rows to a Parquet file. Returns (rows, bytes).

    DuckDB writes Parquet natively, so this needs no extra dependency and never materialises
    the table in Python -- which matters because the warehouse can hold millions of rows and
    the app runs on a small instance.
    """
    from .warehouse import warehouse

    con = warehouse.connect(read_only=True, scope=scope)
    try:
        return _write_rows(destination, "parquet", source_sql="costs",
                           scope=None, since=None, until=None, con=con)
    finally:
        con.close()


def _export_csv(destination: Path, scope: list[str] | None = None) -> tuple[int, int]:
    """The same rows as CSV, in the shape a Cost Management export writes.

    Why bother, when Parquet is a tenth the size: because a file that Azure's own export could
    have produced is one that every downstream thing already understands. `exports.py` reads it
    back, FinOps tooling ingests it, and it opens in Excel. The archive keeps Parquet for its
    own use; this exists for handing to someone else.

    The column order is `warehouse.COLUMNS`, which is FOCUS naming — the same header a FOCUS
    export produces, so the two are interchangeable.
    """
    from .warehouse import warehouse

    con = warehouse.connect(read_only=True, scope=scope)
    try:
        return _write_rows(destination, "csv", source_sql="costs",
                           scope=None, since=None, until=None, con=con)
    finally:
        con.close()


# What a scoped export can be written as. Parquet for the archive's own comparisons, CSV for
# handing to anything that expects what Azure's export produces.
FORMATS = {
    "parquet": (_export_parquet, ".parquet", "application/vnd.apache.parquet"),
    "csv": (_export_csv, ".csv", "text/csv"),
}


async def archive_current(metric: str, *, when: date | None = None,
                          source: str | None = None,
                          scope: list[str] | None = None,
                          suffix: str | None = None,
                          fmt: str = "parquet") -> dict[str, Any]:
    """Write today's copy of the warehouse for this dataset.

    Called after a refresh finishes. Returns a description of what was written; raises
    ArchiveError with a sentence if it could not be.

    `scope` narrows it to a set of subscriptions, and `suffix` distinguishes the resulting blob
    from the daily whole-estate one — a scoped export is a different artefact answering a
    different question, so it must not overwrite the archive that exists to preserve the whole.
    """
    if not enabled():
        raise ArchiveError("No archive account is configured (ARCHIVE_ACCOUNT).")
    if fmt not in FORMATS:
        raise ArchiveError(f"Unknown format {fmt!r}. Use {' or '.join(FORMATS)}.")

    write, ext, content_type = FORMATS[fmt]
    name = blob_name(metric, when)
    if suffix:
        name = name.replace(".parquet", f"-{_SAFE.sub('-', suffix.lower()).strip('-')}.parquet")
    if ext != ".parquet":
        name = name.replace(".parquet", ext)

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"archive{ext}"
        rows, size = write(path, scope=scope)
        if not rows:
            raise ArchiveError(
                "The warehouse is empty for that selection, so there is nothing to export."
                if scope else
                "The warehouse is empty, so there is nothing to archive. The refresh that "
                "triggered this loaded no rows."
            )
        data = path.read_bytes()

    writer = BlobWriter()
    try:
        written = await writer.put(name, data, content_type)
    finally:
        await writer.close()

    log.info("archived %s rows to %s/%s/%s", rows, ACCOUNT, CONTAINER, name)
    return {
        **written,
        "rows": rows,
        "metric": metric,
        "format": fmt,
        "dataset": dataset_folder(metric),
        "account": ACCOUNT,
        "container": CONTAINER,
        "source": source,
        "scope": scope,
        "compressed_bytes": size,
    }


async def status(limit: int = 20) -> dict[str, Any]:
    """What the archive holds, and whether it can be reached at all.

    `reachable` is reported separately from the file list because the two failures need
    different actions: an empty archive means no refresh has run yet, while an unreachable one
    means the network or the role is wrong and no refresh will ever populate it.
    """
    base = {"enabled": enabled(), "account": ACCOUNT, "container": CONTAINER}
    if not enabled():
        return {**base, "reachable": False, "error": "No archive account configured.", "files": []}

    writer = BlobWriter()
    try:
        files = await writer.list(limit=limit)
    except Exception as exc:  # noqa: BLE001 - the reason is the point of this endpoint
        return {**base, "reachable": False, "error": str(exc)[:300], "files": []}
    finally:
        await writer.close()

    latest: dict[str, dict[str, Any]] = {}
    for f in files:
        folder = (f["name"] or "").split("/")[0]
        if folder not in latest:
            latest[folder] = f
    # Which days each dataset has, so a comparison can offer real choices rather than a date
    # picker that mostly points at days with nothing behind them.
    days = {d: _dataset_days(files, d) for d in {(f["name"] or "").split("/")[0] for f in files}}
    return {
        **base,
        "reachable": True,
        "count": len(files),
        "files": files,
        "latest": latest,
        "days": {k: v for k, v in days.items() if v},
        "comparable": sorted(k for k, v in days.items() if len(v) > 1),
        "today": blob_name("FocusCost").rsplit("/", 1)[-1],
    }


# --------------------------------------------------------------- comparing two days

# Restatement is the point. Azure revises cost data for days after the fact, so the same period
# read a week apart is not the same number -- and the warehouse only ever holds the latest read.
# Two archives are the only way to see that movement, which is why they are kept.
COMPARE_MAX_ROWS = 4_000_000


def _dataset_days(files: list[dict[str, Any]], dataset: str) -> list[str]:
    """The dates available for one dataset, newest first.

    The day has to parse as a real date, not merely look like one. Without that check the
    scheduled-export folder leaked in: `exports/.../000001.csv` has a ten-character stem, which
    a length test happily accepted and offered as a snapshot from the year 0000.
    """
    out = []
    for f in files:
        name = f.get("name") or ""
        if not name.startswith(f"{dataset}/"):
            continue
        stem = name.rsplit("/", 1)[-1]
        day = stem.replace(f"{dataset}-", "").replace(".parquet", "")
        try:
            date.fromisoformat(day)
        except ValueError:
            continue
        out.append(day)
    return sorted(set(out), reverse=True)


async def compare(dataset: str, earlier: str, later: str,
                  group_by: str = "ServiceName", top: int = 15) -> dict[str, Any]:
    """What changed between two archived snapshots of the same dataset.

    Both files are read straight from blob storage into DuckDB rather than into Python: they are
    columnar and compressed, and the whole question is one join and one aggregate. A comparison
    that materialised two months of rows in the app would be the one thing on a 1.75 GB instance
    that could not survive a large estate.

    The two figures are not "before and after a change we made". They are what Azure said on two
    different days about *the same period* -- so a difference is a restatement, and that is
    exactly what nobody can see from the dashboard alone.
    """
    import duckdb

    if not enabled():
        raise ArchiveError("No archive account is configured.")
    if earlier == later:
        raise ArchiveError("Pick two different days to compare.")
    # Validated before anything is downloaded. The column name is interpolated into SQL, so it
    # has to be checked regardless — doing it after two blob reads just means a rejected request
    # costs a megabyte of transfer and several seconds first.
    if group_by not in _COMPARE_COLUMNS:
        raise ArchiveError(f"Cannot group by {group_by!r}.")
    # Order them rather than trusting the caller: reversed inputs would report every increase as
    # a decrease, which is worse than an error because it looks like an answer.
    earlier, later = sorted([earlier, later])

    folder = dataset_folder(dataset)
    writer = BlobWriter()
    blobs: dict[str, bytes] = {}
    try:
        for day in (earlier, later):
            y, m = day[:4], day[5:7]
            name = f"{folder}/{y}/{m}/{folder}-{day}.parquet"
            try:
                blobs[day] = await writer.get(name)
            except ArchiveError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise ArchiveError(f"Could not read the {day} archive: {str(exc)[:160]}") from exc
    finally:
        await writer.close()

    with TemporaryDirectory() as tmp:
        paths = {}
        for day, raw in blobs.items():
            p = Path(tmp) / f"{day}.parquet"
            p.write_bytes(raw)
            paths[day] = p.as_posix().replace("'", "''")

        con = duckdb.connect()
        try:
            totals = {}
            for day, path in paths.items():
                row = con.execute(
                    f"SELECT count(*), sum(\"BilledCost\"), min(\"ChargePeriodStart\"), "
                    f"max(\"ChargePeriodStart\"), "
                    f"coalesce(max(nullif(\"BillingCurrency\", '')), 'USD') "
                    f"FROM read_parquet('{path}')"
                ).fetchone()
                totals[day] = {
                    "rows": row[0] or 0,
                    "cost": round(row[1] or 0.0, 2),
                    "from": str(row[2]) if row[2] else None,
                    "to": str(row[3]) if row[3] else None,
                    "currency": row[4],
                }

            # A full outer join, so a thing that appeared or vanished between the two reads is
            # reported rather than silently dropped -- an inner join would hide exactly the
            # movements worth looking at.
            rows = con.execute(
                f"""
                WITH a AS (SELECT coalesce(nullif("{group_by}", ''), '(none)') AS k,
                                  sum("BilledCost") AS cost
                           FROM read_parquet('{paths[earlier]}') GROUP BY 1),
                     b AS (SELECT coalesce(nullif("{group_by}", ''), '(none)') AS k,
                                  sum("BilledCost") AS cost
                           FROM read_parquet('{paths[later]}') GROUP BY 1)
                SELECT coalesce(a.k, b.k) AS k,
                       coalesce(a.cost, 0) AS was,
                       coalesce(b.cost, 0) AS now,
                       coalesce(b.cost, 0) - coalesce(a.cost, 0) AS delta
                FROM a FULL OUTER JOIN b ON a.k = b.k
                WHERE abs(coalesce(b.cost, 0) - coalesce(a.cost, 0)) > 0.005
                ORDER BY abs(delta) DESC
                LIMIT {int(top)}
                """
            ).fetchall()
        finally:
            con.close()

    changes = [{
        "key": r[0],
        "was": round(r[1], 2),
        "now": round(r[2], 2),
        "delta": round(r[3], 2),
        "percent": round((r[3] / r[1]) * 100, 1) if r[1] else None,
    } for r in rows]

    was, now = totals[earlier]["cost"], totals[later]["cost"]
    return {
        "dataset": folder,
        "earlier": {"day": earlier, **totals[earlier]},
        "later": {"day": later, **totals[later]},
        "group_by": group_by,
        "total_delta": round(now - was, 2),
        "total_percent": round(((now - was) / was) * 100, 1) if was else None,
        "row_delta": totals[later]["rows"] - totals[earlier]["rows"],
        "changes": changes,
        "restated": bool(changes),
        "currency": totals[later]["currency"],
    }


# What a comparison may group by. A fixed list, because the column name goes into SQL: these are
# the dimensions the dashboard already reasons about, and anything else is a typo or an attempt.
_COMPARE_COLUMNS = (
    "ServiceName", "SubAccountName", "ResourceGroup", "RegionName",
    "ResourceName", "MeterName", "ChargeCategory", "PricingModel",
)


async def build_selection(*, scope: list[str] | None = None, fmt: str = "csv",
                          label: str = "", since: str | None = None,
                          until: str | None = None, source: str | None = None,
                          dataset: str = "actual") -> dict[str, Any]:
    """Build the selected export in memory and hand back the bytes.

    Split out from `export_selection` so the same file can be sent two places. Writing it to the
    archive is one destination; the browser is the other, and it is the one people ask for first
    — an export you cannot open is a strange kind of export. Both go through this, so a
    downloaded file and an archived one are byte-identical rather than two code paths that agree
    by accident.

    Two different meanings of "date" have to be kept apart, because confusing them produces a
    file that looks right and answers the wrong question:

      * **since / until** narrow the *cost period* — which days of spend are in the file.
      * **source** picks *which read* to take them from — today's warehouse, or an archived
        snapshot from an earlier day. Azure restates cost data for days after the fact, so
        August as read on the 27th is not August as read on the 28th.

    Omitting `source` uses the live warehouse, which is what most people want most of the time.
    """
    if fmt not in FORMATS:
        raise ArchiveError(f"Unknown format {fmt!r}. Use {' or '.join(FORMATS)}.")

    since = _check_day(since, "Start date")
    until = _check_day(until, "End date")
    if since and until and since > until:
        raise ArchiveError("The start date must be on or before the end date.")

    _, ext, content_type = FORMATS[fmt]
    folder = dataset_folder(dataset)
    today = datetime.now(timezone.utc).date()

    # The name records everything that shaped the file. A directory of exports called
    # "selection-1.csv" is unusable a week later; one that says what it holds is self-describing.
    bits = [folder, today.isoformat()]
    if source:
        bits.append(f"as-read-{source}")
    if since or until:
        bits.append(f"{since or 'start'}_to_{until or 'end'}")
    if label:
        bits.append(_SAFE.sub("-", label.lower()).strip("-"))
    stem = "-".join(b for b in bits if b)
    name = f"selection/{today.year:04d}/{today.month:02d}/{stem}{ext}"

    with TemporaryDirectory() as tmp:
        path = Path(tmp) / f"export{ext}"

        if source:
            # From an archived snapshot. Downloaded rather than read in place: the blob endpoint
            # needs a bearer token DuckDB has no way to present.
            day = _check_day(source, "Snapshot date")
            blob = f"{folder}/{day[:4]}/{day[5:7]}/{folder}-{day}.parquet"
            writer = BlobWriter()
            try:
                raw = await writer.get(blob)
            finally:
                await writer.close()
            src = Path(tmp) / "source.parquet"
            src.write_bytes(raw)

            import duckdb

            con = duckdb.connect()
            try:
                rows, size = _write_rows(
                    path, fmt,
                    source_sql=f"read_parquet('{src.as_posix()}')",
                    scope=scope, since=since, until=until, con=con)
            finally:
                con.close()
        else:
            from .warehouse import warehouse

            # Scope goes through the warehouse's own shadow table rather than a WHERE clause, so
            # it is enforced the same way every other scoped read in the app is.
            con = warehouse.connect(read_only=True, scope=scope)
            try:
                rows, size = _write_rows(path, fmt, source_sql="costs",
                                         scope=None, since=since, until=until, con=con)
            finally:
                con.close()

        if not rows:
            raise ArchiveError(
                "Nothing matched that selection — no rows for those subscriptions in that date "
                "range. Widen the dates, or check the snapshot you picked covers them."
            )
        data = path.read_bytes()

    return {
        "data": data,
        "name": name,
        # What a browser should call it. The blob path is a filing decision — year and month
        # folders — and makes a poor filename on someone's desktop.
        "filename": f"{stem}{ext}",
        "content_type": content_type,
        "rows": rows,
        "bytes": len(data),
        "format": fmt,
        "dataset": folder,
        "scope": scope,
        "since": since,
        "until": until,
        "read_on": source or "live warehouse",
        "uncompressed_bytes": size,
    }


async def export_selection(*, scope: list[str] | None = None, fmt: str = "csv",
                           label: str = "", since: str | None = None,
                           until: str | None = None, source: str | None = None,
                           dataset: str = "actual") -> dict[str, Any]:
    """Build the selected export and keep a copy in the archive container.

    The durable half of the same operation: the file lands in storage under a name that records
    what shaped it, so it can be found again next quarter. `build_selection` is what decides the
    contents; this only decides where a copy lives.
    """
    if not enabled():
        raise ArchiveError("No archive account is configured (ARCHIVE_ACCOUNT).")

    built = await build_selection(scope=scope, fmt=fmt, label=label, since=since,
                                  until=until, source=source, dataset=dataset)
    writer = BlobWriter()
    try:
        written = await writer.put(built["name"], built["data"], built["content_type"])
    finally:
        await writer.close()

    log.info("exported %s rows to %s/%s/%s", built["rows"], ACCOUNT, CONTAINER, built["name"])
    return {
        **written,
        "rows": built["rows"],
        "format": built["format"],
        "dataset": built["dataset"],
        "account": ACCOUNT,
        "container": CONTAINER,
        "scope": built["scope"],
        "since": built["since"],
        "until": built["until"],
        "read_on": built["read_on"],
        "uncompressed_bytes": built["uncompressed_bytes"],
    }

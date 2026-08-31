"""Ingest Azure Cost Management **exports** from blob storage.

Why this exists
---------------
The `generateCostDetailsReport` API (see warehouse.py) generates a report per subscription per
period on demand. Measured on a real tenant that is ~115 seconds per subscription-month, so a
50-subscription customer wanting 12 months is roughly 19 hours of polling. That is fine for a
laptop demo and useless for an enterprise.

A **Cost Management export** inverts the cost model: Azure writes the data on a schedule, once,
at whatever scope you choose — subscription, resource group, management group or billing
account. Reading it is then just downloading files. One export at billing-account scope covers a
whole tenant, and a daily export keeps it current with no polling at all.

This module discovers exports, finds the newest run, and loads the files into the same `costs`
table the rest of the app already queries.

Access reality
--------------
In a governed tenant the export's storage account usually has `publicNetworkAccess=Disabled`, so
the blob data plane returns 403 from an ordinary workstation even for an owner. Verified on this
tenant. Two supported paths, in order:

  * **direct** — read blobs with the caller's AAD token. Works when the account allows access
    from where you are running (or you are inside the VNet / have a private endpoint).
  * **sas** — the operator supplies a container SAS URL. This is the pragmatic path for locked
    down accounts: a short-lived read-only SAS can be issued without changing network policy.

Both are read-only. Nothing here writes to storage.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

import httpx

from .cost import ARM, CostError, azure, list_subscriptions
from .warehouse import COLUMNS, NUMERIC, canonical_sub_id, insert_rows, warehouse, _to_date

log = logging.getLogger("cloudlens.exports")

EXPORT_API = "2023-08-01"
# Exports created through some portal flows are only visible on the preview API — verified on
# this tenant, where the GA version returns an empty list while the preview returns nine.
# Discovery therefore tries several versions and keeps whichever returns the most.
#
# 2025-03-01 leads because it is the GA version that finally sees FocusCost: on this tenant
# 2023-08-01 and 2023-03-01 each return two exports and omit both FOCUS ones entirely, which
# is a silent omission — the app reported "no FOCUS export" for exports that plainly existed.
EXPORT_API_VERSIONS = ("2025-03-01", "2023-07-01-preview", "2023-08-01", "2023-03-01")
STORAGE_API = "2023-05-01"
BLOB_VERSION = "2021-12-02"
BLOB_RESOURCE = "https://storage.azure.com"

# Cost Management writes a manifest next to each run; the data files sit beside it. FOCUS exports
# default to Parquet in newer portal flows, so both are supported — DuckDB reads Parquet natively
# and it is both smaller over the wire and faster to load than CSV.
_DATA_SUFFIX = (".csv", ".csv.gz", ".parquet", ".parquet.gz", ".snappy.parquet")


class ExportError(CostError):
    """Raised for problems the operator can fix (access, missing export, empty run)."""


# --------------------------------------------------------------------- discovery
async def discover_exports(subscription_ids: list[str] | None = None) -> dict[str, Any]:
    """List Cost Management exports the caller can see, with their destination and schedule."""
    subs = subscription_ids or [s["id"] for s in (await list_subscriptions())["subscriptions"]]

    async def one(sub: str) -> list[dict]:
        payload: dict[str, Any] = {}
        for version in EXPORT_API_VERSIONS:
            try:
                candidate = await azure.request(
                    "GET", f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/exports",
                    params={"api-version": version},
                )
            except Exception as exc:  # noqa: BLE001 - try the next version
                log.debug("export list failed for %s @ %s: %s", sub, version, exc)
                continue
            if len(candidate.get("value") or []) > len(payload.get("value") or []):
                payload = candidate
        if not payload.get("value"):
            return []

        out = []
        for e in payload.get("value", []):
            p = e.get("properties", {})
            dest = (p.get("deliveryInfo") or {}).get("destination", {}) or {}
            defn = p.get("definition", {}) or {}
            account = str(dest.get("resourceId", "")).split("/")[-1]
            out.append({
                "id": e.get("id"),                              # unique; names repeat across subs
                "name": e.get("name"),
                "subscription_id": sub,
                "type": defn.get("type"),                      # FocusCost | ActualCost | AmortizedCost
                "timeframe": defn.get("timeframe"),
                "granularity": ((defn.get("dataSet") or {}).get("granularity")),
                "format": p.get("format"),
                "compressed": (p.get("compressionMode") or "").lower() == "gzip",
                "partitioned": bool(p.get("partitionData")),
                "storage_account": account,
                "storage_resource_id": dest.get("resourceId"),
                "container": dest.get("container"),
                "root_folder": dest.get("rootFolderPath"),
                "schedule": ((p.get("schedule") or {}).get("recurrence")),
                "status": ((p.get("schedule") or {}).get("status")),
                "next_run": p.get("nextRunTimeEstimate"),
            })
        return out

    results = await asyncio.gather(*(one(s) for s in subs))
    exports = [e for r in results for e in r]

    # A FOCUS export is preferred: one stable schema across billing account types.
    ranked = sorted(exports, key=lambda e: (e["type"] != "FocusCost", e["type"] != "AmortizedCost"))
    return {
        "count": len(exports),
        "exports": ranked,
        "note": (
            "No Cost Management exports found. Create one (FOCUS format, daily, to a storage "
            "account you control) and re-run — that is the scalable path for a large tenant."
            if not exports else None
        ),
    }


def covers_current_period(export: dict[str, Any]) -> bool:
    """Whether this export's timeframe includes today.

    Cost Management exports come in two useful flavours and they are not alternatives:
    a MonthToDate export is rewritten daily and covers the month in progress, while a
    TheLastMonth export is written once a month and covers only closed months. Loading either
    one alone leaves a hole — the current month, or all the history before it.

    This mattered in a way that was easy to miss. The refresh loaded the first export that
    worked, which happened to be the closed-month one, so a FOCUS refresh replaced June and
    July and left August exactly as it was. Every figure for the current month stayed at
    whatever had loaded it last, and after a quick refresh (which cannot fetch tags) that meant
    Cost by Tag stayed empty and looked like an export problem. It was a coverage problem.
    """
    return (export.get("timeframe") or "") in {
        "MonthToDate", "BillingMonthToDate", "TheCurrentMonth", "WeekToDate",
        "TheCurrentQuarter", "TheCurrentYear",
    }


async def export_runs(subscription_id: str, export_name: str, limit: int = 5) -> list[dict]:
    """Recent runs of an export, newest first."""
    payload: dict[str, Any] = {}
    for version in EXPORT_API_VERSIONS:
        try:
            payload = await azure.request(
                "GET",
                f"{ARM}/subscriptions/{subscription_id}/providers/Microsoft.CostManagement"
                f"/exports/{export_name}/runHistory",
                params={"api-version": version},
            )
            if payload.get("value"):
                break
        except Exception:  # noqa: BLE001 - try the next version
            continue
    runs = []
    for r in payload.get("value", []):
        p = r.get("properties", {})
        runs.append({
            "id": r.get("name"),
            "status": p.get("status"),
            "submitted": p.get("submittedTime"),
            "processing_start": p.get("processingStartTime"),
            "processing_end": p.get("processingEndTime"),
            "file_name": p.get("fileName"),
        })
    runs.sort(key=lambda r: r.get("processing_end") or r.get("submitted") or "", reverse=True)
    return runs[:limit]


# ----------------------------------------------------------------- blob access
class BlobReader:
    """Minimal read-only blob client supporting an AAD token or a container SAS."""

    def __init__(self, account: str, container: str, sas: str | None = None) -> None:
        self.account = account
        self.container = container
        self.sas = (sas or "").lstrip("?") or None
        self._token: str | None = None
        self._credential: Any = None

    @classmethod
    def from_sas_url(cls, url: str) -> "BlobReader":
        """Accept a full container SAS URL: https://acct.blob.core.windows.net/container?sv=..."""
        parts = urlparse(url)
        account = parts.netloc.split(".")[0]
        container = parts.path.strip("/").split("/")[0]
        if not account or not container:
            raise ExportError("That does not look like a container SAS URL.")
        return cls(account, container, parts.query)

    async def _headers(self) -> dict[str, str]:
        h = {"x-ms-version": BLOB_VERSION}
        if self.sas:
            return h
        if self._token is None:
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
            self._token = (await self._credential.get_token(f"{BLOB_RESOURCE}/.default")).token
        h["Authorization"] = f"Bearer {self._token}"
        return h

    def _url(self, path: str = "", query: str = "") -> str:
        base = f"https://{self.account}.blob.core.windows.net/{self.container}"
        if path:
            base += "/" + path.lstrip("/")
        parts = [q for q in (query, self.sas) if q]
        return base + ("?" + "&".join(parts) if parts else "")

    async def close(self) -> None:
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    async def list(self, prefix: str = "", limit: int = 5000) -> list[dict[str, Any]]:
        """List blobs under a prefix. Returns name, size and last-modified."""
        found: list[dict[str, Any]] = []
        marker = ""
        async with httpx.AsyncClient(timeout=90) as client:
            while len(found) < limit:
                q = f"restype=container&comp=list&maxresults=1000&prefix={prefix}"
                if marker:
                    q += f"&marker={marker}"
                try:
                    r = await client.get(self._url(query=q), headers=await self._headers())
                except httpx.ConnectError as exc:
                    # A private-endpoint-only account does not resolve publicly at all.
                    raise ExportError(
                        f"Could not reach storage account '{self.account}' — it does not resolve "
                        "from here, which usually means it is private-endpoint only. Run this "
                        "from inside the VNet, or supply a container SAS URL."
                    ) from exc

                if r.status_code == 403:
                    raise ExportError(
                        f"Access to storage account '{self.account}' was denied. The account "
                        "likely has public network access disabled, or you lack Storage Blob "
                        "Data Reader. Supply a container SAS URL instead, or run from inside "
                        "the network."
                    )
                if r.status_code == 404:
                    raise ExportError(f"Container '{self.container}' not found on '{self.account}'.")
                if r.status_code >= 400:
                    raise ExportError(f"Listing blobs failed ({r.status_code}): {r.text[:200]}")

                body = r.text
                for m in re.finditer(r"<Blob>(.*?)</Blob>", body, re.S):
                    blob = m.group(1)
                    name = _tag(blob, "Name")
                    if not name:
                        continue
                    found.append({
                        "name": name,
                        "size": int(_tag(blob, "Content-Length") or 0),
                        "modified": _tag(blob, "Last-Modified"),
                    })
                marker = _tag(body, "NextMarker") or ""
                if not marker:
                    break
        return found[:limit]

    async def get(self, name: str) -> bytes:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(self._url(name), headers=await self._headers())
            if r.status_code >= 400:
                raise ExportError(f"Downloading '{name}' failed ({r.status_code}).")
            return r.content


def _tag(xml: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    return m.group(1) if m else None


# --------------------------------------------------------------------- loading
def _map_header(header: list[str]) -> dict[str, int]:
    """Map our stable schema onto whatever column names this export uses.

    FOCUS exports use FOCUS names (BilledCost, ChargePeriodStart, SubAccountId...), while
    ActualCost/AmortizedCost exports use Azure's own (costInBillingCurrency, date,
    SubscriptionId...). warehouse.COLUMNS already lists the Azure aliases; the FOCUS names are
    added here so one loader handles both.
    """
    focus_aliases = {
        "ChargePeriodStart": ["chargeperiodstart"],
        "BilledCost": ["billedcost", "effectivecost"],
        "CostInUsd": ["billedcostinusd", "effectivecostinusd"],
        "BillingCurrency": ["billingcurrency"],
        "SubAccountId": ["subaccountid"],
        "SubAccountName": ["subaccountname"],
        "ResourceGroup": ["x_resourcegroupname"],
        "ResourceId": ["resourceid"],
        "ResourceName": ["resourcename"],
        "ServiceName": ["servicename"],
        "ServiceFamily": ["servicecategory"],
        "ServiceSubcategory": ["x_servicesubcategory"],
        "MeterName": ["x_metername", "skumeter"],
        "ProductName": ["x_skudescription", "skuid"],
        "RegionName": ["regionname", "regionid"],
        "PricingQuantity": ["pricingquantity", "consumedquantity"],
        "UnitOfMeasure": ["pricingunit", "consumedunit"],
        "UnitPrice": ["listunitprice", "contractedunitprice"],
        "ChargeCategory": ["chargecategory"],
        "PricingModel": ["x_pricingmodel", "commitmentdiscountcategory"],
        "BenefitName": ["commitmentdiscountname"],
        "PublisherName": ["publishername"],
        "CostCenter": ["x_costcenter"],
        "Tags": ["tags"],
    }

    lower = {h.strip().lower().replace(" ", "").replace("_", ""): i for i, h in enumerate(header)}
    out: dict[str, int] = {}
    for target, aliases in COLUMNS.items():
        for alias in list(aliases) + [a.replace("_", "") for a in focus_aliases.get(target, [])]:
            if alias in lower:
                out[target] = lower[alias]
                break
    return out


def _rows_from(raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, [])
    return header, reader


def _rows_from_parquet(raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    """Read a Parquet export via DuckDB, which is already a dependency.

    Values are stringified so the single CSV/Parquet code path downstream stays identical;
    the numeric and date coercion in _load_rows then applies uniformly to both.
    """
    import tempfile

    import duckdb

    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)

    with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    try:
        con = duckdb.connect()
        cur = con.execute(f"SELECT * FROM read_parquet('{path.replace(chr(92), '/')}')")
        header = [d[0] for d in cur.description]
        rows = [["" if v is None else str(v) for v in row] for row in cur.fetchall()]
        con.close()
    finally:
        os.unlink(path)
    return header, rows


def _read_any(name: str, raw: bytes) -> tuple[list[str], Iterable[list[str]]]:
    return _rows_from_parquet(raw) if ".parquet" in name.lower() else _rows_from(raw)


def _load_rows(header: list[str], reader: Iterable[list[str]],
               fallback_sub: str | None) -> tuple[list[list[Any]], int]:
    idx = _map_header(header)
    if "BilledCost" not in idx:
        raise ExportError(f"No cost column in the export; first columns were {header[:10]}")

    keys = list(COLUMNS)
    i_rid, i_rname = keys.index("ResourceId"), keys.index("ResourceName")
    i_sid = keys.index("SubAccountId")

    records: list[list[Any]] = []
    skipped = 0
    for row in reader:
        if not row:
            continue
        rec: list[Any] = []
        for target in keys:
            i = idx.get(target)
            val = row[i] if i is not None and i < len(row) else None
            if target in NUMERIC:
                try:
                    val = float(val) if val not in (None, "") else 0.0
                except ValueError:
                    val = 0.0
            elif target == "ChargePeriodStart":
                val = _to_date(val)
            rec.append(val)

        if not rec[keys.index("ChargePeriodStart")]:
            skipped += 1
            continue
        if not rec[i_rname] and rec[i_rid]:
            rec[i_rname] = str(rec[i_rid]).rstrip("/").split("/")[-1]
        # Canonical subscription id. A FOCUS file spells this as `/subscriptions/<guid>` while
        # the Query API and Cost Details reports use the bare GUID, so storing it verbatim put
        # one subscription in the table twice — and every replace-on-write missed the other
        # copy, so both were kept and both were counted.
        rec[i_sid] = canonical_sub_id(rec[i_sid]) or canonical_sub_id(fallback_sub)
        records.append(rec)
    return records, skipped


async def describe_export(account: str | None = None, container: str | None = None,
                          prefix: str = "", sas_url: str | None = None) -> dict[str, Any]:
    """What columns the newest export file actually provides, and which we can use.

    "Cost by Tag is empty after a FOCUS load" is not answerable from the dashboard: the tab
    cannot tell a missing column from an untagged estate, and the loader silently fills any
    column it cannot find with NULL. That silence is reasonable — a missing PublisherName should
    not fail a refresh — but it means an export that omits something important looks exactly
    like an estate that lacks it.

    So this reads one file and reports the mapping rather than the data.
    """
    reader = (BlobReader.from_sas_url(sas_url) if sas_url
              else BlobReader(account or "", container or ""))
    if not reader.account or not reader.container:
        raise ExportError("Provide either sas_url, or both account and container.")
    try:
        blobs = await reader.list(prefix=prefix.strip("/"))
        data = [b for b in blobs if b["name"].lower().endswith(_DATA_SUFFIX)]
        if not data:
            raise ExportError(f"No data files under '{reader.container}/{prefix}'.")
        newest = max(data, key=lambda b: b["modified"] or "")
        header, rows = _read_any(newest["name"], await reader.get(newest["name"]))
        idx = _map_header(header)
        # A column can be present and empty, which reads downstream exactly like a column that
        # is absent: the loader stores NULL either way. Sampling separates "the export has no
        # Tags column" from "the export has one and Azure left it blank", and those call for
        # completely different fixes.
        filled: dict[str, int] = {}
        sampled = 0
        for row in rows:
            if sampled >= 500:
                break
            if not row:
                continue
            sampled += 1
            for col, i in idx.items():
                if i < len(row) and str(row[i]).strip() not in ("", "{}", "[]"):
                    filled[col] = filled.get(col, 0) + 1
        return {
            "source": f"{reader.account}/{reader.container}/{prefix}".rstrip("/"),
            "file": newest["name"],
            "modified": newest["modified"],
            "columns_in_file": len(header),
            "mapped": sorted(idx),
            "missing": sorted(c for c in COLUMNS if c not in idx),
            "sampled_rows": sampled,
            "empty_though_present": sorted(c for c in idx if not filled.get(c)),
            # Named separately because its absence is the one silently misreported as a fact
            # about the customer's estate rather than about the export.
            "has_tags": "Tags" in idx,
            "rows_with_tags_in_sample": filled.get("Tags", 0),
        }
    finally:
        await reader.close()


async def ingest_export(
    account: str | None = None,
    container: str | None = None,
    prefix: str = "",
    sas_url: str | None = None,
    subscription_id: str | None = None,
    replace: bool = True,
    max_files: int = 60,
) -> dict[str, Any]:
    """Load the newest export run into the warehouse.

    Args:
        account: Storage account name (omit if using sas_url).
        container: Container name (omit if using sas_url).
        prefix: Folder path inside the container, e.g. 'focus/<sub-id>'.
        sas_url: A container SAS URL — the way in when the account blocks public access.
        subscription_id: Used only to fill SubAccountId when the file omits it.
        replace: Clear the covered date range first, so re-running does not double-count.
        max_files: Safety cap on partitioned exports.
    """
    reader = (BlobReader.from_sas_url(sas_url) if sas_url
              else BlobReader(account or "", container or ""))
    if not reader.account or not reader.container:
        raise ExportError("Provide either sas_url, or both account and container.")

    started = datetime.now(timezone.utc)
    # Report progress the same way the API path does, so the strip in the header has something
    # to show. An export ingest reads dozens of blobs and can take minutes; without this it was
    # the one refresh route that still looked like nothing was happening.
    warehouse.state = {"status": "running", "started": started.isoformat(timespec="seconds"),
                       "metric": "FocusCost", "total": 0, "done": 0, "rows": 0, "failed": 0,
                       "empty": 0, "progress": 0.0, "source": f"{reader.account}/{reader.container}"}
    try:
        blobs = await reader.list(prefix=prefix.strip("/"))
        data = [b for b in blobs if b["name"].lower().endswith(_DATA_SUFFIX)]
        if not data:
            raise ExportError(
                f"No CSV files under '{reader.container}/{prefix}'. "
                f"Found {len(blobs)} blob(s); check the folder path."
            )

        # Exports are written per run into a dated folder. Take the newest run only, so a
        # container holding months of history does not load everything at once.
        newest = max(b["modified"] or "" for b in data)
        newest_day = newest[:16]
        latest = [b for b in data if (b["modified"] or "")[:16] >= newest_day] or data
        latest.sort(key=lambda b: b["name"])
        latest = latest[:max_files]

        warehouse.state["total"] = len(latest)
        warehouse.state["loading"] = 1
        # What is being read, in the units this route actually has. The API path counts periods;
        # this one counts blobs, and calling them periods made "0/2 periods loaded" appear over a
        # FOCUS run that has no periods in it.
        warehouse.state["unit"] = "file"

        all_records: list[list[Any]] = []
        skipped = 0
        for n, b in enumerate(latest, 1):
            # Claim the file *before* downloading it, not after.
            #
            # An export is a handful of very large blobs — often two — and essentially all of the
            # wall time is inside the download. Updating only on completion meant the bar sat at
            # 0% for minutes and then jumped to 90%, which is indistinguishable from a refresh
            # that has hung. The count now names the file being fetched, and the bar advances to
            # the start of its share, so something moves the moment work begins.
            warehouse.state["done"] = n - 1
            warehouse.state["fetching"] = n
            warehouse.state["fetching_name"] = b["name"].rsplit("/", 1)[-1][:60]
            warehouse.state["fetching_bytes"] = int(b.get("size") or 0)
            warehouse.state["progress"] = round(((n - 1) / len(latest)) * 0.9, 3)

            header, rows = _read_any(b["name"], await reader.get(b["name"]))
            # Parsing is pure Python over every row of a multi-megabyte CSV, so it goes to a
            # thread for the same reason the write does: on the event loop it stops the app
            # answering anything, including the poll that draws this progress bar.
            recs, sk = await asyncio.to_thread(_load_rows, header, rows, subscription_id)
            all_records.extend(recs)
            skipped += sk
            # Downloading is nearly all of the wall time here, so the bar tracks it directly and
            # stops just short of full — the database write still has to happen.
            warehouse.state["done"] = n
            warehouse.state["rows"] = len(all_records)
            warehouse.state["progress"] = round(min(n / len(latest), 1.0) * 0.9, 3)
        warehouse.state.pop("fetching", None)

        if not all_records:
            raise ExportError(
                f"Read {len(latest)} file(s) but found no usable rows — {skipped} row(s) were "
                "skipped for a missing date. The export schema may be unrecognised."
            )

        i_date = list(COLUMNS).index("ChargePeriodStart")
        i_sub = list(COLUMNS).index("SubAccountId")
        dates = [r[i_date] for r in all_records if r[i_date]]
        lo, hi = min(dates), max(dates)
        subs = {r[i_sub] for r in all_records if r[i_sub]}

        # Writing is its own phase and takes real time — tens of thousands of rows through
        # DuckDB — so it says so rather than leaving the bar at 90% with no explanation.
        warehouse.state["loading"] = 0
        warehouse.state["writing"] = len(all_records)
        warehouse.state["progress"] = 0.92

        def _write() -> None:
            with warehouse.connect() as con:
                if replace:
                    if subs:
                        con.execute(
                            'DELETE FROM costs WHERE "ChargePeriodStart" BETWEEN ? AND ? '
                            f'AND "SubAccountId" IN ({",".join("?" * len(subs))})',
                            [lo, hi, *subs],
                        )
                    else:
                        con.execute(
                            'DELETE FROM costs WHERE "ChargePeriodStart" BETWEEN ? AND ?',
                            [lo, hi],
                        )
                # Named columns via the shared bulk helper. This was a positional
                # `INSERT INTO costs VALUES (...)` executed per row: positional couples the
                # write to today's COLUMNS order, which ALTER TABLE quietly breaks, and per-row
                # execution is what made a FOCUS load take minutes.
                insert_rows(con, all_records)
                con.execute(
                    "INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                    [subscription_id or ",".join(sorted(subs))[:200], reader.account,
                     f"{lo}..{hi}", len(all_records), started, "ok",
                     f"export {reader.container}/{prefix} ({len(latest)} file(s))"],
                )

        # On a worker thread, because DuckDB is synchronous and this is the single longest step
        # in the whole refresh.
        #
        # Run inline it blocked the event loop for over five minutes on a real estate: the app
        # served its last request at 11:15:38 and its next at 11:20:51, with nothing in between.
        # For that whole window the dashboard was frozen — the progress poll could not be
        # answered, so the bar stayed where it was, and every other tab hung too. That is the
        # entire substance of "the refresh button doesn't load the data": it was loading, and
        # the app had stopped being able to say so.
        await asyncio.to_thread(_write)
        warehouse.state.pop("writing", None)
        # The database runs on local disk and is copied back to durable storage when a load
        # finishes; see warehouse.publish. Without this a FOCUS load would be lost on restart.
        await warehouse.publish_async()
    finally:
        await reader.close()

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    result = {
        "source": f"{reader.account}/{reader.container}/{prefix}".rstrip("/"),
        "files": len(latest),
        "rows": len(all_records),
        "skipped_rows": skipped,
        "from": lo,
        "to": hi,
        "subscriptions": len(subs),
        "seconds": round(elapsed, 1),
        "warehouse": warehouse.summary(),
    }
    # Say that it finished. Without this the state is left at whatever the *previous* run set —
    # so a successful export ingest inherited a stale "failed" from the last attempt, and the
    # archive step, which only fires on a run that actually succeeded, skipped it silently.
    warehouse.state = {
        "status": "ready",
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": result["source"],
        "files": result["files"],
        "seconds": result["seconds"],
        "progress": 1.0,
        **warehouse.summary(),
    }
    return result


async def ingest_all_exports(subscription_ids: list[str] | None = None,
                             prefer: str = "FocusCost") -> dict[str, Any]:
    """Discover exports and load whichever ones are reachable.

    Best-effort by design: in a governed tenant most export storage accounts will refuse a
    direct read, and that is information the operator needs rather than a hard failure.
    """
    found = await discover_exports(subscription_ids)
    exports = [e for e in found["exports"] if e["container"]]
    if prefer:
        exports.sort(key=lambda e: e["type"] != prefer)

    loaded, failed = [], []
    for e in exports:
        try:
            result = await ingest_export(
                account=e["storage_account"],
                container=e["container"],
                prefix=e["root_folder"] or "",
                subscription_id=e["subscription_id"],
            )
            loaded.append({"export": e["name"], "type": e["type"], **result})
        except Exception as exc:  # noqa: BLE001 - report and continue to the next export
            failed.append({"export": e["name"], "account": e["storage_account"],
                           "reason": str(exc)[:220]})

    return {
        "discovered": found["count"],
        "loaded": loaded,
        "failed": failed,
        "rows_loaded": sum(l["rows"] for l in loaded),
        "note": (
            "No export could be read directly. This is normal in a governed tenant: the storage "
            "accounts have public network access disabled. Supply a container SAS URL "
            "(ingest_export(sas_url=...)) or run from inside the network."
            if not loaded and failed else None
        ),
    }


async def reachable(exports: list[dict[str, Any]], timeout: float = 8.0) -> tuple[list, list]:
    """Split exports into the ones whose storage answers and the ones that do not.

    A pre-flight, run before any ingest starts, because the alternative is a background job that
    spends half a minute discovering the same thing and then reports a failure to someone who
    has been watching a spinner. Where storage is private-endpoint only -- the normal case in a
    governed tenant -- this turns "press refresh, wait, fail" into an immediate, explained no.

    Cheap on purpose: one listing of one blob per account, in parallel, with a short timeout.
    The question is only whether the data plane answers at all.
    """
    async def probe(e: dict[str, Any]) -> tuple[dict, str | None]:
        reader = BlobReader(e.get("storage_account") or "", e.get("container") or "")
        try:
            await asyncio.wait_for(reader.list(prefix=(e.get("root_folder") or "").strip("/"),
                                               limit=1), timeout=timeout)
            return e, None
        except asyncio.TimeoutError:
            return e, (f"Storage account '{e.get('storage_account')}' did not answer within "
                       f"{timeout:.0f}s.")
        except Exception as exc:  # noqa: BLE001 - the reason is what the caller reports
            return e, str(exc)
        finally:
            await reader.close()

    results = await asyncio.gather(*(probe(e) for e in exports), return_exceptions=False)
    ok = [e for e, err in results if err is None]
    bad = [(e, err) for e, err in results if err is not None]
    return ok, bad

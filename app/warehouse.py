"""Local cost warehouse: ingest Azure cost details once, then answer questions from DuckDB.

Why
---
The Cost Management Query API is slow (seconds per call) and aggressively throttled, which caps
how rich a question can be — every breakdown is another round trip, and anything it doesn't
support as a "dimension" simply cannot be asked.

Instead we pull row-level cost detail for every accessible subscription via
`generateCostDetailsReport`, normalise it into a FOCUS-shaped table, and keep it in a local
DuckDB file. After that, questions are SQL against local columnar data: milliseconds, no
throttling, arbitrary grouping, joins and window functions.

The report API takes minutes to produce a file, so ingest runs in the background and the app
stays usable throughout.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb
import httpx

log = logging.getLogger("cloudlens.warehouse")

ARM = "https://management.azure.com"
DETAILS_API = "2023-11-01"

# How long a single report submission may spend being throttled before giving up. Six retries
# with an escalating backoff could sleep for twelve minutes before the first poll, which made a
# doomed refresh look identical to a slow one for half an hour.
SUBMIT_BUDGET = 240.0

# Minimum gap between report submissions for the same subscription. Cost Management's limit is
# a handful per minute per subscription; five seconds keeps three months' worth comfortably
# inside it while adding only ten seconds of stagger to the whole run.
SUBMIT_GAP = 5.0
DB_PATH = Path(os.getenv("COST_DB", Path(__file__).resolve().parents[1] / "data" / "costs.duckdb"))

# Where the database actually *runs*.
#
# On App Service, /home is an Azure Files SMB share — durable, and survives restarts and
# redeploys, which is why the database is configured to live there. It is also catastrophically
# slow for the access pattern DuckDB has. Measured on this instance, 4KB writes with an fsync
# after each:
#
#   /tmp        1,555 ops/s
#   /home/data     38 ops/s      <- 40x slower
#
# DuckDB fsyncs on commit, so that 40x lands directly on every refresh. The app measured 48
# rows/s written against 2,052 rows/s for the same code on a local disk — the same ratio, which
# is how we know the cost is the filesystem and not the CPU or the row count.
#
# So: run on local disk, publish to the durable copy when a write finishes. The copy is one
# sequential bulk write, which is the one thing SMB is good at, instead of thousands of small
# synchronous ones.
#
# The trade is a crash window. If the instance dies between a refresh finishing and the publish
# landing, that refresh is lost — but the data is a cache of Azure's own billing records and is
# recreated by pressing Refresh, so the cost of losing it is minutes, not data. Anything that
# must survive is written to Azure, not here.
#
# Set COST_DB_LOCAL=0 to run directly against the durable path (correct for a local machine,
# where the two are the same disk and copying is pure overhead).
def _local_cache_path(durable: Path) -> Path | None:
    """The fast working copy of `durable`, or None if it should be used directly."""
    if os.getenv("COST_DB_LOCAL", "").strip() in {"0", "false", "no"}:
        return None
    # Only worth doing for the network share. Anywhere else the copy costs more than it saves.
    # `as_posix` so the check behaves the same when the tests run on Windows, where Path
    # rewrites the separator and a plain str() prefix test silently never matches.
    if not durable.as_posix().startswith("/home/"):
        return None
    root = Path(os.getenv("COST_DB_CACHE_DIR", "/tmp/cloudlens"))
    return root / durable.name



# ----------------------------------------------------------------- the quick path
#
# The Cost Details API used by `ingest` is asynchronous: submit a report, wait for Azure to
# build it, then download it. Generation alone is 40s+ per period, submissions are throttled per
# subscription, and three months across three subscriptions is nine of them — measured at ten to
# twenty minutes on this tenant, with one period timing out at the twenty-minute ceiling.
#
# The Query API answers the same question synchronously. Measured on this tenant: one month of
# one subscription, daily granularity, grouped five ways, returned 1,622 rows in 7.8 seconds
# complete — no continuation. That is the whole month at resource-and-day level.
#
# The cost of the trade is columns. A query returns the dimensions it was grouped by and nothing
# else, so quantities, unit prices, benefit attribution and tags are absent — those need the
# detail report or a FOCUS export. `quick_ingest` therefore fills what it can and leaves the
# rest empty rather than inventing it, and the caller is told which columns it did not fill.
QUERY_API = "2023-11-01"

# Cost Management throttles by *client type*, and a caller that does not name itself is filed
# under a shared "DefaultQuota" bucket along with every other anonymous client touching this
# tenant — the portal, the CLI, and whatever else happens to be running.
#
# That bucket is what has been refusing us. Measured against this tenant while a quick refresh
# was failing on all three subscriptions:
#
#   x-ms-ratelimit-remaining-microsoft.costmanagement-clienttype-requests: DefaultQuota:0
#   x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after:        9  (then 35)
#   x-ms-ratelimit-remaining-microsoft.costmanagement-entity-requests:     DefaultQuota:3
#   x-ms-ratelimit-remaining-microsoft.costmanagement-tenant-requests:     DefaultQuota:19
#   x-ms-ratelimit-microsoft.costmanagement-qpu-remaining:                 QueriesPer10Sec:11
#
# Only the client-type allowance was spent; our own entity, tenant and QPU allowances all had
# room. The identical request sent with this header was served immediately.
#
# So naming ourselves is not cosmetic. It is the difference between our own allowance and
# whatever is left of everyone else's — and it is why the "wait several minutes" advice this
# code used to print was wrong: we were not out of quota, we were in the wrong queue.
CLIENT_TYPE = os.getenv("COST_CLIENT_TYPE", "CloudLens")

# Every retry-after header Cost Management may send, most specific first. The client-type one
# is listed first because it is the limit that actually fires, and it was missing here — so a
# 429 that came with precise guidance ("wait 9 seconds") was read as having none, and fell
# through to a synthetic ladder that guessed minutes. The service was answering the question
# and we were not reading the reply.
RETRY_HEADERS = (
    "x-ms-ratelimit-microsoft.costmanagement-clienttype-retry-after",
    "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    "Retry-After",
)

# The query throttle has more than one dimension: per subscription (entity), per tenant, per
# client type, and a QPU allowance. Firing three months of one subscription at once is what
# earns an entity 429, not the total rate, so queries queue per subscription exactly as report
# submissions do — otherwise the retry budget is spent losing a race the caller started with
# itself. `CLIENT_TYPE` above handles the other dimension; this handles this one.
QUERY_GAP = 1.5

# A quick refresh is a promise about *time*, not about completeness.
#
# The point of this path is a live answer now — "whatever can be fetched", against an API that
# returns in about six seconds when it is not throttled. Measured on this tenant: 6.1s for a
# month of a subscription, unthrottled.
#
# So the budget is the design. Waiting out a 429 is the right thing for a background job and
# exactly the wrong thing here: the previous backoff could sleep 161 seconds per subscription,
# which turned the fast option into the slowest one in the menu. Two short retries, then this
# subscription is reported as throttled and the rest carry on. Partial and immediate beats
# complete and late — that is the whole reason someone picked this option.
QUERY_BACKOFF = (2.0, 5.0)
QUICK_DEADLINE = float(os.getenv("QUICK_REFRESH_SECONDS", "75"))

# Five, because five is verified to work in one call. Each dimension multiplies the row count,
# and the Query API refuses a request whose result would be too large — so this is the set that
# buys the most schema for the least risk of that refusal.
#
# ServiceFamily earns its place over MeterCategory, which was here first and was a mistake: the
# whole cost-area breakdown — Compute & Web, AI & Data and the rest — is a GROUP BY on
# ServiceFamily. Leaving it empty did not merely lose a column, it silently moved every
# quick-loaded row into "Other services": on this estate AI & Data vanished entirely and Other
# went from $12 to $1,035. A column the dashboard classifies by is not optional.
QUERY_DIMENSIONS = [
    ("ResourceId", "ResourceId"),
    ("ServiceName", "ServiceName"),
    ("ServiceFamily", "ServiceFamily"),
    ("ResourceGroupName", "ResourceGroup"),
    ("ResourceLocation", "RegionName"),
]

# What a quick load cannot know. Named here so the UI can say so plainly rather than leaving
# someone to notice an empty Tags tab and assume the refresh failed.
QUICK_OMITS = ("PricingQuantity", "UnitPrice", "UnitOfMeasure", "MeterName", "ProductName",
               "ChargeCategory", "PricingModel", "BenefitName", "BenefitId", "PublisherName",
               "CostCenter", "Tags", "ServiceSubcategory")

# FOCUS-aligned target schema. Azure's cost detail export uses its own column names, which also
# differ between EA / MCA / pay-as-you-go accounts, so we map many possible source names onto one
# stable set. Everything downstream — the agent, the SQL tool — only ever sees these.
#
# Verified against a real MCA export on 2026-08-21 (65 source columns). Note `resourceName` is
# NOT one of them, so ResourceName is derived from ResourceId after load.
COLUMNS: dict[str, list[str]] = {
    "ChargePeriodStart": ["date", "usagedate", "chargeperiodstart", "billingperiodstartdate"],
    "BilledCost": ["costinbillingcurrency", "cost", "pretaxcost", "billedcost"],
    "CostInUsd": ["costinusd"],
    "BillingCurrency": ["billingcurrency", "billingcurrencycode", "currency"],
    "SubAccountId": ["subscriptionid", "subscriptionguid", "subaccountid"],
    "SubAccountName": ["subscriptionname", "subaccountname"],
    "ResourceGroup": ["resourcegroupname", "resourcegroup"],
    "ResourceId": ["resourceid", "instanceid"],
    "ResourceName": ["resourcename"],
    "ServiceName": ["metercategory", "consumedservice", "servicename"],
    "ServiceFamily": ["servicefamily"],
    "ServiceSubcategory": ["metersubcategory", "servicecategory"],
    "MeterName": ["metername"],
    "ProductName": ["productname"],
    "RegionName": ["resourcelocation", "location", "regionname", "meterregion"],
    "PricingQuantity": ["quantity", "consumedquantity", "pricingquantity"],
    "UnitOfMeasure": ["unitofmeasure", "pricingunit"],
    "UnitPrice": ["unitprice", "effectiveprice"],
    "ChargeCategory": ["chargetype", "chargecategory"],
    # Amortized and FOCUS exports name these differently from ActualCost, and the whole point
    # of switching metric is to get them — a missed alias here silently produces a column of
    # nulls, which reads as "no reservations" rather than "not mapped".
    "PricingModel": ["pricingmodel", "reservationtype", "commitmentdiscounttype"],
    "BenefitName": ["benefitname", "reservationname", "commitmentdiscountname"],
    "BenefitId": ["benefitid", "reservationid", "commitmentdiscountid"],
    "PublisherName": ["publishername", "publishertype"],
    "CostCenter": ["costcenter"],
    "Tags": ["tags"],
}

NUMERIC = {"BilledCost", "CostInUsd", "PricingQuantity", "UnitPrice"}

# Column name to position, built once. Several call sites need it and rebuilding a dict per row
# is the kind of thing that turns a fast path back into a slow one.
COLUMNS_INDEX = {c: i for i, c in enumerate(COLUMNS)}


_CREDENTIAL: Any = None
_TOKEN: tuple[str, float] | None = None


def _token(url: str = "") -> str:
    """An ARM token for whoever this call is being made on behalf of.

    When the request is running as a signed-in person — delegated ARM, set up by `cost.act_as`
    — their token is used. Otherwise the app's own identity.

    That order is the whole point rather than a nicety. The app's identity can only ever read
    subscriptions somebody granted it a role on, which for a multi-tenant deployment is close to
    none of them: a user signs in from their own directory, the picker correctly lists their
    subscriptions, and every warehouse-backed tab is empty because the ingest that would have
    filled it ran as an identity with no access to their estate. The dashboard is then wrong in
    the most expensive way available — confidently, and about somebody else's money.

    Falling back to the app identity keeps single-tenant deployments and background jobs
    working, where there is no signed-in person to act as.
    """
    global _CREDENTIAL, _TOKEN

    try:
        from .cost import caller_token

        delegated = caller_token(url)
    except Exception:  # noqa: BLE001 - never let this be the reason an ingest cannot start
        delegated = None
    if delegated:
        return delegated

    if _TOKEN and _TOKEN[1] > time.time() + 120:
        return _TOKEN[0]

    if _CREDENTIAL is None:
        from azure.identity import DefaultAzureCredential

        _CREDENTIAL = DefaultAzureCredential(exclude_interactive_browser_credential=False)

    try:
        tok = _CREDENTIAL.get_token(f"{ARM}/.default")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not get an Azure token: {exc}. Locally, run 'az login'. When deployed, check "
            "the app has a managed identity with Reader and Cost Management Reader."
        ) from exc

    _TOKEN = (tok.token, float(tok.expires_on))
    return tok.token


class Reader:
    """A borrowed read-only connection, handing back dicts rather than tuples.

    Deliberately minimal: it does not own the connection and cannot outlive the `reader()`
    block that produced it.
    """

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._con = con

    def rows(self, sql: str, limit: int = 200) -> list[dict[str, Any]]:
        cur = self._con.execute(sql)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchmany(limit)]


class Warehouse:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        # `durable_path` is what survives a restart; `db_path` is what queries actually run
        # against. On App Service they differ (see `_local_cache_path`); everywhere else they
        # are the same file and `_publish` is a no-op.
        self.durable_path = db_path
        self.durable_path.parent.mkdir(parents=True, exist_ok=True)
        cache = _local_cache_path(db_path)
        if cache is not None:
            cache.parent.mkdir(parents=True, exist_ok=True)
            self.db_path = cache
            self._restore()
        else:
            self.db_path = db_path
        self._publish_lock = threading.Lock()
        self._lock = asyncio.Lock()
        # The one connection that owns the file. Everything else is a cursor on it; see
        # connect(). Guarded by a threading lock rather than the asyncio one because it is
        # opened from worker threads as well as the event loop.
        self._db: duckdb.DuckDBPyConnection | None = None
        self._db_lock = threading.Lock()
        # Report submissions are throttled per subscription, so they are queued per subscription.
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._last_submit: dict[str, float] = {}
        # The same pairing for the query path, which is throttled the same way.
        self._query_locks: dict[str, asyncio.Lock] = {}
        self._last_query: dict[str, float] = {}
        self._rebuilt = 0
        self._init_schema()
        # Reflect what is already on disk, so a restart doesn't report an empty warehouse.
        summary = self.summary()
        if self._rebuilt:
            self.state: dict[str, Any] = {
                "status": "empty",
                "detail": (f"{self._rebuilt:,} row(s) were discarded because the stored columns "
                           "no longer matched the schema and their values were shifted. "
                           "Refresh to reload."),
            }
        else:
            self.state = (
                {"status": "ready", **summary} if summary["rows"]
                else {"status": "empty", "detail": "no data ingested yet"}
            )

    # ------------------------------------------------------- durable copy
    def _restore(self) -> None:
        """Seed the fast working copy from the durable one, on startup.

        Only when the durable file is newer: a redeploy restarts the process but keeps /tmp on
        the same instance, so the working copy is often already current and re-copying it would
        only add startup latency.
        """
        try:
            if not self.durable_path.exists():
                return
            if (self.db_path.exists()
                    and self.db_path.stat().st_mtime >= self.durable_path.stat().st_mtime):
                log.info("warehouse cache is current, not restoring")
                return
            t0 = time.monotonic()
            shutil.copy2(self.durable_path, self.db_path)
            log.info("restored warehouse from %s (%.1f MB in %.1fs)", self.durable_path,
                     self.db_path.stat().st_size / 1e6, time.monotonic() - t0)
        except Exception as exc:  # noqa: BLE001 - a bad cache must not stop the app booting
            log.warning("could not restore warehouse cache: %s", exc)

    def publish(self) -> None:
        """Copy the working database back to durable storage.

        DuckDB's own `COPY FROM DATABASE` rather than a file copy. A file copy of a live
        database is not a snapshot — it reads pages while the writer may still be moving them,
        and on Windows it cannot even open the file. This asks the database for a consistent
        copy of every table instead, and writes it sequentially, which is the one thing an SMB
        share is good at.

        Written to a neighbouring temp name and then renamed, because `os.replace` is atomic
        within a filesystem: a reader on another instance, or the next boot after a crash
        mid-copy, sees either the old database or the new one and never a half-written file.

        Called after a refresh rather than after every statement. One sequential copy is cheap;
        the per-commit fsyncs it replaces are what cost 40x.
        """
        if self.db_path == self.durable_path:
            return
        with self._publish_lock:
            tmp = self.durable_path.with_suffix(".duckdb.publishing")
            try:
                t0 = time.monotonic()
                con = self._database()
                name = con.execute("SELECT current_database()").fetchone()[0]
                # A leftover from an interrupted publish would make ATTACH open it instead of
                # creating it, and we would copy into a database that already has the tables.
                tmp.unlink(missing_ok=True)
                # ATTACH does not accept a bound parameter, so the path is inlined. It comes
                # from configuration rather than a request, but the quote is doubled anyway --
                # a directory with an apostrophe in it should not be able to end the statement.
                target = str(tmp).replace("'", "''")
                con.execute(f"ATTACH '{target}' AS publish_target")
                try:
                    con.execute(f'COPY FROM DATABASE "{name}" TO publish_target')
                finally:
                    con.execute("DETACH publish_target")
                os.replace(tmp, self.durable_path)
                log.info("published warehouse (%.1f MB in %.1fs)",
                         self.durable_path.stat().st_size / 1e6, time.monotonic() - t0)
            except Exception as exc:  # noqa: BLE001 - failing to publish must not fail a refresh
                log.warning("could not publish warehouse: %s", exc)
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

    async def publish_async(self) -> None:
        """`publish` off the event loop — it is a multi-megabyte copy over SMB."""
        await asyncio.to_thread(self.publish)

    # ------------------------------------------------------------------ schema
    def _database(self) -> duckdb.DuckDBPyConnection:
        """The single connection that holds the file open, opened on first use.

        Double-checked under a lock: `_init_schema` runs during construction and the ingest
        opens connections from several worker threads, so two callers can arrive here at once.
        """
        if self._db is None:
            with self._db_lock:
                if self._db is None:
                    self._db = duckdb.connect(str(self.db_path))
        return self._db

    def connect(self, read_only: bool = False,
                scope: list[str] | None = None,
                currency: str | None = None) -> duckdb.DuckDBPyConnection:
        """Open a connection, optionally restricted to a set of subscriptions.

        Scope is enforced by shadowing `costs` with a temp table holding only the selected rows,
        so a query physically cannot see anything outside the chosen subscriptions — the model
        forgetting a WHERE clause cannot produce a wrong-scope answer. Building it costs ~30ms on
        this data volume.

        `currency` shadows the table the same way, replacing `BilledCost` with its converted
        value and `BillingCurrency` with the target. Doing it here rather than in each query is
        the difference between one change and twenty: `"BilledCost"` appears in about that many
        places across the dashboard, waste, tags and commitments modules, and every one of them
        would otherwise have to learn about exchange rates. They ask for money; this decides
        which money.

        `read_only` is the caller's statement of intent and is deliberately *not* passed to
        DuckDB. Within one process DuckDB shares a single database instance across connections,
        and it refuses outright to open a second connection whose configuration differs from the
        first — "Can't open a connection to same database file with a different configuration
        than existing connections". So a reader asking for read-only while an ingest holds a
        read-write connection does not queue politely; it fails.

        That never surfaced while writes ran on the event loop, because nothing else could run
        during one. Moving the write to a worker thread — which is what stops a refresh freezing
        the whole app — made reads genuinely concurrent for the first time and the mismatch
        started throwing. Everything opens read-write; DuckDB's MVCC gives readers a consistent
        snapshot while a write is in flight, which is the behaviour these call sites wanted from
        read-only in the first place.

        The file itself is opened exactly once and never let go. This used to call
        `duckdb.connect(path)` per operation and close it again, which meant the database
        instance was constructed and destroyed constantly — and whenever an open raced a close,
        DuckDB refused the new one with "Unique file handle conflict: Cannot attach costs — the
        database file is already attached by database costs". On App Service that is not a rare
        race: the warehouse lives on `/home`, an Azure Files SMB share, where the handle is
        released well after close() returns, so a reopen inside that window sees the file as
        still held. It showed up as an ingest reporting "2 of 9 periods failed" with two whole
        months of a subscription quietly missing, and as intermittent 500s from /api/warehouse
        while a refresh ran.

        A cursor is a full, independent connection that shares the one instance — its own
        transaction, its own temp tables — so callers still get isolation and still close what
        they were given; closing a cursor just no longer closes the database.
        """
        con = self._database().cursor()
        target = (currency or "").upper()
        rate = None
        if target:
            from . import currency as fx

            rate = fx.rate_to(target)

        if scope or rate is not None:
            where = ""
            params: list[str] = []
            if scope:
                ids = ", ".join("?" for _ in scope)
                # Compared in canonical form so a caller passing an ARM path, a differently
                # cased GUID, or the plain GUID all select the same rows.
                where = (f'WHERE lower("SubAccountId") IN ({ids}) '
                         f'OR "SubAccountName" IN ({ids})')
                params = [canonical_sub_id(s) or "" for s in scope] + list(scope)

            if rate is None:
                select = "*"
            else:
                # Pivot on Azure's own USD figure — the number Microsoft put on the invoice —
                # falling back to the billed amount only where that column is empty, which
                # happens on some older export schemas.
                usd = 'coalesce(nullif("CostInUsd", 0), "BilledCost")'
                select = (f'* REPLACE ({usd} * {rate} AS "BilledCost", '
                          f"'{target}' AS \"BillingCurrency\")")

            con.execute(
                f"CREATE TEMP TABLE costs AS SELECT {select} FROM main.costs {where}", params)
        return con

    @contextmanager
    def reader(self, scope: list[str] | None = None,
               currency: str | None = None) -> Iterator["Reader"]:
        """One read-only connection reused across a burst of internal queries.

        Opening a connection is the expensive part — and under a scope it also rebuilds the
        shadow table. A dashboard tab asks seven questions; doing that on seven connections
        made a scoped tab take 849 ms, of which the queries themselves were a small fraction.

        For app-authored SQL only. Model-authored SQL still goes through `query()`, which is
        where the single-statement, no-file-access and currency checks live.
        """
        con = self.connect(read_only=True, scope=scope, currency=currency)
        try:
            yield Reader(con)
        finally:
            con.close()

    def _init_schema(self) -> None:
        cols = ",\n  ".join(
            f'"{c}" {"DOUBLE" if c in NUMERIC else ("DATE" if c == "ChargePeriodStart" else "VARCHAR")}'
            for c in COLUMNS
        )
        with self.connect() as con:
            con.execute(f"CREATE TABLE IF NOT EXISTS costs (\n  {cols}\n)")
            con.execute("CREATE TABLE IF NOT EXISTS ingest_log ("
                        "subscription_id VARCHAR, subscription_name VARCHAR, period VARCHAR, "
                        "rows BIGINT, ingested_at TIMESTAMP, status VARCHAR, detail VARCHAR)")
            # Small facts about the deployment rather than about the spend: whether first-run
            # setup has been settled, and when. It lives here rather than in a file because
            # this database is the thing that already survives a restart -- on App Service the
            # filesystem does not, and `_publish` copies every table in it to durable storage.
            con.execute("CREATE TABLE IF NOT EXISTS meta ("
                        "key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP)")

            # CREATE TABLE IF NOT EXISTS only builds the schema on an empty database, so a
            # warehouse that predates a new column keeps the old shape and every query naming
            # that column fails. Add anything missing rather than requiring a re-ingest —
            # existing rows get NULL, which is honest: that data genuinely was not collected.
            order = [
                r[0] for r in con.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'costs' ORDER BY ordinal_position"
                ).fetchall()
            ]
            existing = set(order)
            for name in COLUMNS:
                if name in existing:
                    continue
                kind = ("DOUBLE" if name in NUMERIC
                        else "DATE" if name == "ChargePeriodStart" else "VARCHAR")
                con.execute(f'ALTER TABLE costs ADD COLUMN "{name}" {kind}')
                order.append(name)
                log.info("warehouse: added missing column %s", name)

            # ALTER TABLE appends, so a column added later sits at the end of the table while
            # the code expects it wherever COLUMNS puts it. Rows were written positionally, so
            # every value between the two positions landed one column off: tags in BenefitId,
            # costCenter in Tags, and so on. Nothing errors -- the columns are all VARCHAR --
            # so the warehouse quietly serves the wrong field under the right name.
            #
            # Inserts are by name now, so this cannot recur. Rows already written under the old
            # order are not recoverable: whether a given row is shifted depends on when it was
            # written, and that is not recorded. Discard them rather than serve them.
            if order != list(COLUMNS):
                stale = con.execute("SELECT count(*) FROM costs").fetchone()[0]
                log.warning(
                    "warehouse: stored column order does not match the code (%s). Rows written "
                    "under the old order have values shifted between columns, so the %d row(s) "
                    "held are being discarded. Run a refresh to reload them correctly.",
                    ", ".join(a for a, b in zip(order, COLUMNS) if a != b) or "length differs",
                    stale,
                )
                con.execute("DROP TABLE costs")
                con.execute(f"CREATE TABLE costs (\n  {cols}\n)")
                self._rebuilt = stale

            self._merge_subscription_spellings(con)

    def _merge_subscription_spellings(self, con: Any) -> None:
        """Collapse rows that spell the same subscription two different ways.

        FOCUS writes `/subscriptions/<guid>`; the Query API and Cost Details reports write the
        bare GUID. Both were stored verbatim, so one subscription existed twice — and because
        every write replaces its range with `WHERE "SubAccountId" = ?`, neither loader ever
        deleted the other's rows. Both copies survived and both were summed: a month Azure
        billed at $476.07 read as $920.98 on the dashboard.

        Writes are canonical now, so this only has to repair what is already stored. It runs on
        every boot and does nothing when there is nothing to merge.

        Duplicates are removed on the identity of the charge itself — day, resource, service,
        meter, quantity and amount — keeping one row per charge. Rows that differ in any of
        those are genuinely different charges and are left alone, so a subscription that
        legitimately has two similar line items on the same day keeps both.
        """
        try:
            mixed = con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" LIKE \'/subscriptions/%\''
                ' OR "SubAccountId" <> lower("SubAccountId")').fetchone()[0]
            if not mixed:
                return
            before = con.execute("SELECT count(*) FROM costs").fetchone()[0]
            con.execute(
                'UPDATE costs SET "SubAccountId" = lower('
                "  CASE WHEN lower(\"SubAccountId\") LIKE '/subscriptions/%'"
                "       THEN regexp_extract(\"SubAccountId\", '([^/]+)$')"
                '       ELSE "SubAccountId" END)'
                ' WHERE "SubAccountId" IS NOT NULL')
            con.execute(
                "CREATE OR REPLACE TABLE costs AS SELECT * EXCLUDE (_rn) FROM ("
                "  SELECT *, row_number() OVER (PARTITION BY "
                '    "SubAccountId", "ChargePeriodStart", "ResourceId", "ServiceName",'
                '    "MeterName", "PricingQuantity", "BilledCost") AS _rn'
                "  FROM costs) WHERE _rn = 1")
            after = con.execute("SELECT count(*) FROM costs").fetchone()[0]
            log.warning(
                "warehouse: merged two spellings of the same subscription id in %d row(s) and "
                "removed %d duplicate charge(s) (%d -> %d rows). Totals were overstated until "
                "now; writes are canonical from here.", mixed, before - after, before, after)
        except Exception as exc:  # noqa: BLE001 - a failed repair must not stop the app booting
            log.warning("warehouse: could not merge subscription spellings: %s", exc)

    # ------------------------------------------------------------------ ingest
    async def _report(self, client: httpx.AsyncClient, sub: str, start: str, end: str,
                      headers: dict, metric: str = "AmortizedCost") -> list[str]:
        """Kick off a cost details report and return download links once it is ready.

        Amortized by default. ActualCost bills a reservation as one lump on the day it was
        bought, which makes that month look like a spike and every later month look artificially
        cheap; amortized spreads the purchase across the term it covers, so a monthly trend is
        comparable and reservation usage is attributed to the resources that actually consumed
        it. It also populates `BenefitName` and `BenefitId`, without which "what are my
        commitments saving me" cannot be answered at all.

        The trade-off is that amortized reports the *effective* rate rather than what left the
        bank account this month. For an estate with no commitments the two are identical, which
        is why switching costs nothing until there is something to amortise.

        Nothing here is quick: submission is rate-limited per subscription and generation is a
        background job on Azure's side. Both waits are reported through `self.state` so a
        refresh that is merely slow can be told apart from one that has stopped — before this,
        a run spent up to half an hour showing `0/3` with no indication of what it was doing.
        """
        url = (f"{ARM}/subscriptions/{sub}/providers/Microsoft.CostManagement/"
               f"generateCostDetailsReport?api-version={DETAILS_API}")
        payload = {"metric": metric, "timePeriod": {"start": start, "end": end}}

        # Submissions are serialised per subscription, with a short gap between them.
        #
        # The limit Cost Management enforces here is per subscription, so firing this month,
        # last month and the month before simultaneously — which is what running the periods
        # concurrently did — guaranteed that two of the three were refused. Each then backed off
        # for a minute or more before its first poll, and a three-month refresh that should take
        # about as long as its slowest report took fifteen minutes.
        #
        # Queuing them costs a few seconds of stagger and avoids the refusal altogether. Only
        # the POST is serialised; generation and download still overlap, which is where the
        # minutes actually are.
        r = None
        self._phase(1, "submitting")
        try:
            spent = 0.0
            for attempt in range(6):
                async with self._submit_slot(sub):
                    r = await client.post(url, headers=headers, json=payload)
                if r.status_code != 429:
                    break
                wait = _retry_after(r, attempt)
                if spent + wait > SUBMIT_BUDGET:
                    log.info("submission still throttled after %.0fs (%s %s); giving up",
                             spent, sub[:8], start)
                    break
                spent += wait
                log.info("submission throttled (%s %s); retrying in %.0fs", sub[:8], start, wait)
                await asyncio.sleep(wait)
        finally:
            self._phase(-1, "submitting")

        if r is None:
            raise RuntimeError("no response from the report API")
        if r.status_code == 204:
            return []
        if r.status_code == 429:
            raise RuntimeError(
                "Cost Management is rate-limiting report requests for this subscription and did "
                "not accept one within a few minutes. Wait and try again — this is a per-"
                "subscription limit, not a permission problem."
            )
        if r.status_code not in (200, 202):
            # A WebDirect subscription (Visual Studio, pay-as-you-go, MSDN) is refused outright
            # by the Cost Details API. Left raw, the 422 reads as a bug in the app or a missing
            # permission, and neither is true -- no amount of retrying or granting will help.
            if r.status_code == 422 and "WebDirect" in r.text:
                raise RuntimeError(
                    "this subscription's offer type (WebDirect: Visual Studio, pay-as-you-go or "
                    "MSDN) is not supported by the Cost Details API. The dashboard's live figures "
                    "still work; for a loaded warehouse, use a Cost Management export and pick "
                    "FOCUS report on Refresh data."
                )
            raise RuntimeError(f"report request failed {r.status_code}: {r.text[:200]}")

        location = r.headers.get("Location")
        if not location:
            body = r.json()
            return [b["blobLink"] for b in body.get("manifest", {}).get("blobs", [])]

        # These reports genuinely take minutes; poll patiently rather than giving up early.
        deadline = time.time() + 1200
        self._phase(1, "generating")
        try:
            while time.time() < deadline:
                await asyncio.sleep(10)
                p = await client.get(location, headers=headers)
                if p.status_code in (202, 429):
                    continue
                if p.status_code != 200:
                    raise RuntimeError(f"poll failed {p.status_code}: {p.text[:200]}")
                body = p.json()
                status = (body.get("status") or "").lower()
                if status in ("completed", "success"):
                    return [b["blobLink"] for b in body.get("manifest", {}).get("blobs", [])]
                if status in ("nodatafound", "no data found"):
                    return []
                if status in ("failed", "error"):
                    raise RuntimeError(_report_failure(body, metric))
        finally:
            self._phase(-1, "generating")
        raise RuntimeError("report timed out after 20 minutes")

    @asynccontextmanager
    async def _submit_slot(self, sub: str) -> AsyncIterator[None]:
        """Serialise report submissions for one subscription, spaced by SUBMIT_GAP.

        Per subscription rather than globally: the throttle is scoped that way, so an estate
        with six subscriptions can still submit six reports at once and only queues within each.
        """
        lock = self._submit_locks.setdefault(sub, asyncio.Lock())
        async with lock:
            gap = SUBMIT_GAP - (time.monotonic() - self._last_submit.get(sub, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                yield
            finally:
                self._last_submit[sub] = time.monotonic()

    def _phase(self, delta: int, name: str) -> None:
        """Count how many jobs are sitting in each wait, for the progress bar.

        `done` only moves when a whole period lands, and a period can be five minutes of waiting
        on Azure. A bar driven by `done` alone therefore sits at 0% for most of a refresh, which
        is barely better than a spinner — it is the thing that makes someone press the button
        again. Counting the phases lets the bar advance continuously through each period.
        """
        if self.state.get("status") != "running":
            return
        self.state[name] = max(0, self.state.get(name, 0) + delta)
        self._progress()

    # How far through a period each phase is. Submission is quick, generation is nearly all of
    # the wall time, and the download is short but not instant. The weights are deliberately
    # conservative: a bar that reaches 90% and stops reads as broken, so each phase claims
    # slightly less than it has really achieved.
    _PHASE_WEIGHT = {"submitting": 0.10, "generating": 0.45, "loading": 0.85}

    def _progress(self) -> None:
        """A single 0–1 figure for the bar, from whole periods plus partial ones."""
        total = self.state.get("total") or 0
        if not total:
            return
        partial = sum(self.state.get(name, 0) * weight
                      for name, weight in self._PHASE_WEIGHT.items())
        done = self.state.get("done", 0)
        # Never let the estimate overtake reality, and never let it reach 1 before the run ends.
        self.state["progress"] = round(min((done + partial) / total, 0.99), 3)

    @staticmethod
    def _rows(text: str) -> tuple[list[str], list[list[str]]]:
        reader = csv.reader(io.StringIO(text))
        header = next(reader, [])
        return header, [r for r in reader if r]

    @staticmethod
    def _mapping(header: list[str]) -> dict[str, int]:
        """Map our target columns onto whichever source names this account happens to use."""
        lower = {h.strip().lower().replace(" ", "").replace("_", ""): i for i, h in enumerate(header)}
        out: dict[str, int] = {}
        for target, aliases in COLUMNS.items():
            for alias in aliases:
                if alias in lower:
                    out[target] = lower[alias]
                    break
        return out

    async def _fetch(self, client: httpx.AsyncClient, links: list[str],
                     sub: dict) -> list[list[Any]]:
        """Download and parse a report's blobs into rows ready for insertion.

        Deliberately does not touch the database. The old version inserted as it downloaded,
        which forced the caller to clear the period *before* the download so the two halves
        lined up — and the download is the part that takes minutes and fails. Separating them
        lets the caller replace a period only once it actually holds the replacement.
        """
        records: list[list[Any]] = []
        for link in links:
            d = await client.get(link)
            d.raise_for_status()
            raw = d.content
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            header, rows = self._rows(raw.decode("utf-8-sig", errors="replace"))
            if not rows:
                continue

            idx = self._mapping(header)
            if "BilledCost" not in idx:
                raise RuntimeError(f"no cost column found in export; saw {header[:12]}")

            keys = list(COLUMNS)
            i_rid, i_rname = keys.index("ResourceId"), keys.index("ResourceName")
            i_sid, i_sname = keys.index("SubAccountId"), keys.index("SubAccountName")

            for row in rows:
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

                # The export has no resourceName column; the leaf of the resource id is it.
                if not rec[i_rname] and rec[i_rid]:
                    rec[i_rname] = str(rec[i_rid]).rstrip("/").split("/")[-1]
                # Fill subscription identity from what we already know, if absent in the file,
                # and canonicalise whatever was there — a FOCUS file spells this as an ARM path.
                rec[i_sid] = canonical_sub_id(rec[i_sid]) or canonical_sub_id(sub["id"])
                if not rec[i_sname]:
                    rec[i_sname] = sub["name"]
                records.append(rec)
        return records

    async def _replace(self, sub_id: str, start: str, end: str,
                       records: list[list[Any]]) -> int:
        """Swap one subscription-period's rows for the ones just downloaded, atomically.

        The delete and the insert are one transaction, so a reader never sees the window empty
        and a failure half-way leaves the previous data untouched. Before this, the delete ran
        minutes earlier — at the *start* of the job — which meant a refresh that failed had
        already destroyed what it was replacing, and the dashboard read zero for the whole run.
        """
        if not records:
            return 0

        def _swap() -> None:
            with self.connect() as con:
                con.execute("BEGIN TRANSACTION")
                try:
                    con.execute(
                        'DELETE FROM costs WHERE "SubAccountId" = ? '
                        'AND "ChargePeriodStart" BETWEEN ? AND ?',
                        [canonical_sub_id(sub_id), start, end],
                    )
                    # Named, not positional. A positional insert silently depends on the table
                    # having been created with exactly today's COLUMNS order, and ALTER TABLE
                    # appends -- so a column added later shifted every value after it into the
                    # neighbouring column. Naming them costs nothing and removes the coupling.
                    insert_rows(con, records)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise

        # The lock still serialises writers; the difference is that waiting for DuckDB no longer
        # stops the event loop. A refresh that writes tens of thousands of rows was freezing the
        # whole app while it did — no progress poll answered, no tab able to load — which is why
        # a working refresh looked like a broken button.
        async with self._lock:
            await asyncio.to_thread(_swap)
        return len(records)

    # ------------------------------------------------------------ the quick path
    async def _query_period(self, client: httpx.AsyncClient, sub: dict,
                            start: str, end: str, headers: dict,
                            metric: str, deadline: float | None = None) -> list[list[Any]]:
        """One synchronous Query API call, mapped into the warehouse schema.

        `deadline` is a monotonic clock reading this must not run past. It is checked before
        every request and before every sleep, because both are places where a quick refresh
        can quietly stop being quick — and the caller would rather have three subscriptions
        loaded and one reported as throttled than four loaded two minutes from now.
        """
        def out_of_time() -> bool:
            return deadline is not None and time.monotonic() >= deadline

        url = (f"{ARM}/subscriptions/{sub['id']}/providers/Microsoft.CostManagement/"
               f"query?api-version={QUERY_API}")
        body = {
            "type": metric,
            "timeframe": "Custom",
            "timePeriod": {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"},
            "dataset": {
                "granularity": "Daily",
                "aggregation": {
                    "totalCost": {"name": "Cost", "function": "Sum"},
                    "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
                },
                "grouping": [{"type": "Dimension", "name": src}
                             for src, _ in QUERY_DIMENSIONS],
            },
        }

        rows: list[list[Any]] = []
        page_url = url
        for _ in range(20):          # the Query API pages with an absolute nextLink
            r = None
            for attempt in range(len(QUERY_BACKOFF) + 1):
                if out_of_time():
                    raise TimeoutError("ran out of time")
                # Serialised per subscription and spaced, so three months of one estate queue
                # instead of racing each other into a refusal.
                async with self._query_slot(sub["id"]):
                    r = await client.post(page_url, headers=headers, json=body)
                if r.status_code != 429:
                    break
                if attempt >= len(QUERY_BACKOFF):
                    break
                wait = _query_wait(r, attempt)
                # Sleeping past the deadline is the same as failing, only later. Stop now and
                # let the caller keep what the other subscriptions returned.
                if deadline is not None and time.monotonic() + wait >= deadline:
                    raise TimeoutError("throttled with no time left to wait")
                await asyncio.sleep(wait)
            if r is None or r.status_code != 200:
                raise RuntimeError(_query_failure(r))
            props = (r.json() or {}).get("properties") or {}
            rows.extend(self._map_query(props, sub))
            nxt = props.get("nextLink")
            if not nxt or out_of_time():
                # Out of time mid-paging keeps the pages already read: they are whole days,
                # and a partial period is caught by the classification guard below rather than
                # being written as if it were complete.
                break
            page_url = nxt
        return rows

    @asynccontextmanager
    async def _query_slot(self, sub: str) -> AsyncIterator[None]:
        """One query at a time per subscription, spaced by QUERY_GAP.

        Per subscription rather than globally, for the same reason `_submit_slot` is: the limit
        is scoped that way, so an estate of six subscriptions can still have six queries in
        flight and only queues within each.
        """
        lock = self._query_locks.setdefault(sub, asyncio.Lock())
        async with lock:
            gap = QUERY_GAP - (time.monotonic() - self._last_query.get(sub, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            try:
                yield
            finally:
                self._last_query[sub] = time.monotonic()

    @staticmethod
    def _map_query(props: dict, sub: dict) -> list[list[Any]]:
        """Turn one Query API page into warehouse rows.

        Columns come back in whatever order the service chose and are matched by name, not
        position — the same reason the CSV loader maps by header. `UsageDate` arrives as the
        integer 20260801, which is not a date to anything downstream until it is one.
        """
        at = {c.get("name"): i for i, c in enumerate(props.get("columns") or [])}
        idx = {c: i for i, c in enumerate(COLUMNS)}
        by_source = dict(QUERY_DIMENSIONS)

        out: list[list[Any]] = []
        for raw in props.get("rows") or []:
            row: list[Any] = [0.0 if c in NUMERIC else "" for c in COLUMNS]

            if "UsageDate" not in at or raw[at["UsageDate"]] is None:
                continue
            text = str(raw[at["UsageDate"]])
            row[idx["ChargePeriodStart"]] = (
                f"{text[:4]}-{text[4:6]}-{text[6:8]}" if len(text) == 8 else text[:10])

            row[idx["BilledCost"]] = float(raw[at["Cost"]] or 0) if "Cost" in at else 0.0
            row[idx["CostInUsd"]] = float(raw[at["CostUSD"]] or 0) if "CostUSD" in at else 0.0
            row[idx["BillingCurrency"]] = (
                str(raw[at["Currency"]] or "") if "Currency" in at else "")
            row[idx["SubAccountId"]] = canonical_sub_id(sub["id"])
            row[idx["SubAccountName"]] = sub.get("name") or sub["id"]

            for source, target in by_source.items():
                if source in at and target in idx:
                    row[idx[target]] = str(raw[at[source]] or "")

            # Derived exactly as the CSV path derives it, so a quick load and a full one agree
            # on what a resource is called rather than one leaving the column blank.
            rid = row[idx["ResourceId"]]
            if rid:
                row[idx["ResourceName"]] = rid.rsplit("/", 1)[-1]
            out.append(row)
        return out

    async def quick_ingest(self, subscriptions: list[dict], months: int = 3,
                           concurrency: int = 3, metric: str = "ActualCost") -> dict[str, Any]:
        """Load the same months through the synchronous Query API instead of a report.

        One call per subscription, not one per subscription per month. A report has to be
        generated per period, so `ingest` has no choice; a query does not — asking for the
        whole range at once returned all 91 days in a single response here, and it is the
        difference between three requests and nine against an endpoint that throttles per
        subscription. Nine of them raced each other into 429s and eight periods failed.
        """
        def headers_for(sub_id: str) -> dict[str, str]:
            token = _token(f"/subscriptions/{sub_id}")
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "ClientType": CLIENT_TYPE}

        # The same window `_periods` covers, as one span rather than a list of months.
        spans = _periods(months)
        start, end = spans[0][0], spans[-1][1]
        label = f"{start} → {end}"

        # One clock for the whole run, not one per subscription: the promise is about how long
        # the person waits, and three subscriptions each taking their own budget is three times
        # the wait they were promised.
        deadline = time.monotonic() + QUICK_DEADLINE

        self.state = {"status": "running", "started": _now(), "metric": metric,
                      "total": len(subscriptions), "done": 0, "rows": 0, "failed": 0,
                      "empty": 0, "progress": 0.0, "unit": "subscription", "mode": "quick"}

        gate = asyncio.Semaphore(concurrency)
        errors: list[str] = []
        timed_out: list[str] = []
        timings: list[dict[str, Any]] = []

        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:

            async def one(sub: dict) -> None:
                async with gate:
                    t_start = time.monotonic()
                    t_fetched = t_start
                    try:
                        self._phase(1, "loading")
                        try:
                            records = await self._query_period(
                                client, sub, start, end, headers_for(sub["id"]), metric,
                                deadline=deadline)
                        finally:
                            t_fetched = time.monotonic()
                            self._phase(-1, "loading")

                        if not records:
                            # Same reasoning as the report path: an empty answer is far more
                            # often a refusal than a period that cost nothing, and deleting on
                            # that basis destroys good data to record an absence.
                            kept = self._period_rows(sub["id"], start, end)
                            note = ("no rows returned; kept the "
                                    f"{kept:,} already held" if kept else "no rows returned")
                            self._log(sub, label, 0, "empty", note)
                            self.state["empty"] = self.state.get("empty", 0) + 1
                            return

                        # Refuse to write rows the dashboard cannot classify.
                        #
                        # The cost-area breakdown is a GROUP BY on ServiceFamily. When this
                        # column was missing from the query, every row still loaded happily and
                        # every one of them landed in "Other services" — AI & Data disappeared
                        # from the estate and nothing anywhere said so. Bad data that looks
                        # like good data is worse than no refresh, so this fails loudly instead.
                        fam = COLUMNS_INDEX["ServiceFamily"]
                        classified = sum(1 for r in records if r[fam])
                        if classified < len(records) * 0.5:
                            raise RuntimeError(
                                f"only {classified:,} of {len(records):,} rows came back with a "
                                "service family, so the cost-area breakdown would be wrong. "
                                "Nothing was written. Use FOCUS or a full-detail refresh.")

                        rows = await self._replace(sub["id"], start, end, records)
                        # Fetch and write timed separately, because they fail differently and
                        # only one of them is Azure's fault. Guessing which half was slow cost
                        # a deploy: the fetch was inside its budget the whole time and the
                        # write was not budgeted at all.
                        t_done = time.monotonic()
                        timings.append({
                            "subscription": sub["name"],
                            "rows": rows,
                            "fetch_s": round(t_fetched - t_start, 1),
                            "write_s": round(t_done - t_fetched, 1),
                        })
                        self._log(sub, label, rows, "ok", "quick load")
                        self.state["rows"] += rows
                        self.state["timings"] = timings
                        log.info("quick-ingested %s %s: %d rows (fetch %.1fs, write %.1fs)",
                                 sub["name"], label, rows,
                                 t_fetched - t_start, t_done - t_fetched)
                    except TimeoutError as exc:
                        # Not a failure of the data — a decision not to keep waiting. Reported
                        # separately so the UI can say "ran out of time" rather than "failed",
                        # which are different things and suggest different next steps.
                        log.info("quick ingest ran out of time for %s: %s", sub["name"], exc)
                        self._log(sub, label, 0, "failed", f"timed out: {exc}")
                        self.state["failed"] += 1
                        timed_out.append(sub["name"])
                    except Exception as exc:  # noqa: BLE001 - one subscription must not stop the rest
                        log.warning("quick ingest failed for %s: %s", sub["name"], exc)
                        self._log(sub, label, 0, "failed", str(exc)[:300])
                        self.state["failed"] += 1
                        errors.append(f"{sub['name']}: {str(exc)[:160]}")
                    finally:
                        self.state["done"] += 1
                        self._progress()

            await asyncio.gather(*(one(s) for s in subscriptions))

        total = len(subscriptions)
        failed = self.state["failed"]
        empty = self.state.get("empty", 0)
        # A run that ran out of time is described as that, not as a failure. The data it did
        # load is real and already written; what is missing is missing because the person was
        # not made to wait for it, which is the deal this option offers.
        late = (f"{len(timed_out)} subscription(s) were still rate-limited after "
                f"{int(QUICK_DEADLINE)}s and were left as they were "
                f"({', '.join(timed_out[:3])}). Their existing data is unchanged — "
                "use FOCUS report for a complete load.") if timed_out else ""

        if failed and failed == total:
            status = "failed"
            detail = late or "; ".join(errors[:3])
        elif failed:
            status = "partial"
            detail = late or (f"{failed} of {total} subscriptions failed. "
                              + "; ".join(errors[:2]))
            if late and errors:
                detail = f"{late} {errors[0]}"
        elif empty == total:
            status = "empty"
            detail = (f"All {total} subscription(s) returned no rows, so nothing was replaced. "
                      "The existing data is unchanged.")
        elif empty:
            status = "partial"
            detail = (f"{empty} of {total} subscription(s) returned no rows and were left as "
                      "they were; the rest reloaded.")
        else:
            status, detail = "ready", None

        self.state = {**self.state, **self.summary(), "status": status, "finished": _now(),
                      "failed": failed, "empty": empty, "total": total, "metric": metric,
                      "unit": "subscription", "mode": "quick", "omits": list(QUICK_OMITS),
                      "timed_out": timed_out, "budget_seconds": int(QUICK_DEADLINE),
                      "progress": 1.0}
        if detail:
            self.state["detail"] = detail
        # Persist once, at the end, rather than per subscription: the copy is the same size
        # either way and doing it three times triples the only slow part that is left.
        await self.publish_async()
        return self.state

    async def ingest(self, subscriptions: list[dict], months: int = 3,
                     concurrency: int = 4, metric: str = "AmortizedCost") -> dict[str, Any]:
        """Pull the last N months of detail for each subscription. Safe to re-run.

        Each report takes minutes to generate server-side, so the jobs are run concurrently —
        sequentially this would be the better part of an hour for a handful of subscriptions.
        """
        # Per subscription, not once for the batch. An ARM token is issued by one tenant and
        # refused by every other, so a person with access in three directories holds three
        # tokens; one header for the batch would read the first estate and 401 on the rest,
        # which is indistinguishable from having no access at all.
        def headers_for(sub_id: str) -> dict[str, str]:
            token = _token(f"/subscriptions/{sub_id}")
            return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                    "ClientType": CLIENT_TYPE}

        periods = _periods(months)
        jobs = [(s, p) for s in subscriptions for p in periods]

        self.state = {"status": "running", "started": _now(), "metric": metric,
                      "total": len(jobs), "done": 0, "rows": 0, "failed": 0, "empty": 0,
                      "progress": 0.0}

        gate = asyncio.Semaphore(concurrency)
        # Kept so the finished state can say *why* it failed. A bare count tells someone the
        # refresh did not work without telling them anything they can act on.
        errors: list[str] = []

        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:

            async def one(sub: dict, period: tuple[str, str, str]) -> None:
                start, end, label = period
                async with gate:
                    try:
                        # Fetch first, replace second. The window is only cleared once the
                        # replacement rows are actually in hand, and the swap is one
                        # transaction — so a report that fails, times out or is throttled
                        # leaves the previous data exactly where it was.
                        headers = headers_for(sub["id"])
                        links = await self._report(client, sub["id"], start, end, headers, metric)
                        self._phase(1, "loading")
                        try:
                            records = await self._fetch(client, links, sub) if links else []
                        finally:
                            self._phase(-1, "loading")

                        if not records:
                            # An empty report is far more often a transient refusal than a
                            # month that genuinely cost nothing, and deleting on that basis
                            # destroys good data to record an absence. Leave the period alone
                            # and say so; a stale row can be corrected, a deleted one cannot.
                            kept = self._period_rows(sub["id"], start, end)
                            note = ("no rows returned; kept the "
                                    f"{kept:,} already held" if kept else "no rows returned")
                            self._log(sub, label, 0, "empty", note)
                            log.info("ingested %s %s: %s", sub["name"], label, note)
                            self.state["empty"] = self.state.get("empty", 0) + 1
                            return

                        rows = await self._replace(sub["id"], start, end, records)
                        self._log(sub, label, rows, "ok", None)
                        self.state["rows"] += rows
                        log.info("ingested %s %s: %d rows", sub["name"], label, rows)
                    except Exception as exc:  # noqa: BLE001 - one period must not stop the rest
                        log.warning("ingest failed for %s %s: %s", sub["name"], label, exc)
                        self._log(sub, label, 0, "failed", str(exc)[:300])
                        self.state["failed"] += 1
                        errors.append(f"{sub['name']} {label}: {str(exc)[:160]}")
                    finally:
                        self.state["done"] += 1
                        self._progress()

            await asyncio.gather(*(one(s, p) for s, p in jobs))

        # Report what actually happened.
        #
        # This used to hardcode "ready" and then spread summary() over it. summary() carries no
        # status of its own, so every outcome came back as success: an ingest where all three
        # subscriptions failed still told the browser it was fine, and the UI -- which only ever
        # checked status == "failed" -- silently redrew the same data. Clicking Refresh appeared
        # to do nothing at all, with no error anywhere, which is the worst way for this to break.
        failed = self.state["failed"]
        empty = self.state.get("empty", 0)
        if failed and failed == len(jobs):
            status: str = "failed"
            detail: str | None = "; ".join(errors[:3])
        elif failed:
            status = "partial"
            detail = f"{failed} of {len(jobs)} periods failed. " + "; ".join(errors[:2])
        elif empty == len(jobs):
            # Nothing errored and nothing came back. The warehouse still holds whatever it had,
            # which is the right outcome, but calling that "ready" would claim a refresh
            # happened when no new data was written.
            status = "empty"
            detail = (f"All {len(jobs)} period(s) returned no rows, so nothing was replaced. "
                      "The existing data is unchanged. This usually means the Cost Details API "
                      "has nothing for this metric and period yet.")
        elif empty:
            status = "partial"
            detail = (f"{empty} of {len(jobs)} period(s) returned no rows and were left as they "
                      "were; the rest reloaded.")
        else:
            status, detail = "ready", None

        self.state = {**self.state, **self.summary(), "status": status, "finished": _now(),
                      "failed": failed, "empty": empty, "total": len(jobs), "metric": metric,
                      "progress": 1.0}
        if detail:
            self.state["detail"] = detail
        await self.publish_async()
        return self.state

    def _period_rows(self, sub_id: str, start: str, end: str) -> int:
        """How many rows the warehouse already holds for one subscription-period.

        Used to say what was kept when a report comes back empty, so "0 rows" is reported as a
        decision not to overwrite rather than as a period that vanished.
        """
        with self.connect(read_only=True) as con:
            row = con.execute(
                'SELECT count(*) FROM costs WHERE "SubAccountId" = ? '
                'AND "ChargePeriodStart" BETWEEN ? AND ?',
                [canonical_sub_id(sub_id), start, end],
            ).fetchone()
        return row[0] or 0

    def _log(self, sub: dict, period: str, rows: int, status: str, detail: str | None) -> None:
        with self.connect() as con:
            con.execute("INSERT INTO ingest_log VALUES (?,?,?,?,?,?,?)",
                        [sub["id"], sub["name"], period, rows,
                         datetime.now(timezone.utc), status, detail])

    # ------------------------------------------------------------------- query
    def budget_trend(self, sub_id: str, start: str, end: str,
                     tags: list[tuple[str, list[str]]] | None = None,
                     currency: str | None = None) -> dict[str, Any]:
        """Daily spend for one budget's current period, from local cost data.

        Azure reports a budget as a single number — spend so far — with no history behind it,
        so "am I about to breach, or did that happen three weeks ago?" cannot be answered from
        the budget API at all. The warehouse has the days; this shapes them to the budget's own
        window, subscription and tag filter so the line means the same thing the headline does.

        `tags` is the budget's filter as (key, values) pairs. Matching is case-insensitive on
        both: Azure stores tag keys with whatever case they were created with — this estate has
        both `Contact` and `contact` — but treats them as one when filtering a budget, so
        matching case-sensitively here would draw an empty chart under a non-zero total.
        """
        where = ['"SubAccountId" = ?', '"ChargePeriodStart" BETWEEN ? AND ?']
        params: list[Any] = [canonical_sub_id(sub_id), start, end]

        for key, values in tags or []:
            if not values:
                # A key with no values is "has this tag at all", not "has it and equals
                # nothing" — the latter matches nothing and would silently empty the chart.
                where.append(
                    'EXISTS (SELECT 1 FROM json_each(TRY_CAST("Tags" AS JSON)) t '
                    "WHERE lower(t.key) = lower(?))")
                params.append(key)
                continue
            placeholders = ", ".join("lower(?)" for _ in values)
            where.append(
                'EXISTS (SELECT 1 FROM json_each(TRY_CAST("Tags" AS JSON)) t '
                f"WHERE lower(t.key) = lower(?) "
                f"AND lower(json_extract_string(t.value, '$')) IN ({placeholders}))")
            params.append(key)
            params.extend(values)

        sql = (f'SELECT "ChargePeriodStart" AS day, SUM(COALESCE("BilledCost", 0)) AS cost '
               f"FROM costs WHERE {' AND '.join(where)} GROUP BY 1 ORDER BY 1")
        try:
            with self.connect(read_only=True, currency=currency) as con:
                rows = con.execute(sql, params).fetchall()
        except Exception as exc:  # noqa: BLE001 - a chart is not worth failing the tab for
            log.info("budget trend failed for %s: %s", sub_id[:8], str(exc)[:200])
            return {"labels": [], "values": [], "cumulative": [], "total": 0.0,
                    "error": str(exc)[:200]}

        labels = [str(r[0])[:10] for r in rows]
        values = [round(float(r[1] or 0), 2) for r in rows]
        running = 0.0
        cumulative = []
        for v in values:
            running += v
            cumulative.append(round(running, 2))

        return {
            "labels": labels,
            "values": values,
            "cumulative": cumulative,
            "total": round(running, 2),
            "from": start,
            "to": end,
        }

    def currencies(self, scope: list[str] | None = None) -> list[str]:
        """Distinct billing currencies present, honouring the subscription scope."""
        with self.connect(read_only=True, scope=scope) as con:
            rows = con.execute(
                'SELECT DISTINCT "BillingCurrency" FROM costs '
                "WHERE \"BillingCurrency\" IS NOT NULL AND \"BillingCurrency\" <> ''"
            ).fetchall()
        return sorted(r[0] for r in rows)

    def summary(self, scope: list[str] | None = None) -> dict[str, Any]:
        """What the warehouse holds. With a scope, only what those subscriptions contribute —
        so a person who can see one subscription isn't told the row count of the whole estate."""
        with self.connect(read_only=True, scope=scope) as con:
            row = con.execute(
                'SELECT count(*), min("ChargePeriodStart"), max("ChargePeriodStart"), '
                'count(DISTINCT "SubAccountId") FROM costs'
            ).fetchone()
            # Totals are only meaningful within a single currency, so never pre-sum across them.
            by_currency = con.execute(
                'SELECT coalesce(nullif("BillingCurrency", \'\'), \'unknown\'), sum("BilledCost") '
                "FROM costs GROUP BY 1 ORDER BY 2 DESC"
            ).fetchall()

        totals = [{"currency": c, "cost": round(v or 0.0, 2)} for c, v in by_currency]
        known = [t for t in totals if t["currency"] != "unknown"]
        mixed = len(known) > 1
        return {
            "rows": row[0] or 0,
            "from": str(row[1]) if row[1] else None,
            "to": str(row[2]) if row[2] else None,
            "subscriptions": row[3] or 0,
            # Left as None when mixed: a single cross-currency total would be a meaningless number.
            "total_cost": totals[0]["cost"] if len(totals) == 1 else None,
            "total_by_currency": totals,
            "currency": known[0]["currency"] if len(known) == 1 else None,
            "currencies": [t["currency"] for t in known],
            "mixed_currency": mixed,
        }

    def query(self, sql: str, limit: int = 200, scope: list[str] | None = None) -> dict[str, Any]:
        """Run a read-only SELECT against the warehouse, optionally scoped to subscriptions.

        The connection is opened read_only, so DuckDB itself refuses writes; this check exists to
        reject them with a clear message, to block file-reading functions that would otherwise
        reach outside the warehouse, and to block catalog-qualified table names that would
        sidestep the scope filter.
        """
        cleaned = sql.strip().rstrip(";")
        lowered = cleaned.lower()

        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise ValueError("Only SELECT queries are allowed.")
        if ";" in cleaned:
            raise ValueError("Only a single statement is allowed.")

        banned = ("insert", "update", "delete", "drop", "create", "alter", "attach", "detach",
                  "copy", "export", "import", "install", "load", "pragma", "call", "set",
                  "read_csv", "read_parquet", "read_json", "read_text", "read_blob", "glob",
                  "sniff_csv", "parquet_scan", "csv_scan")
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in banned):
            raise ValueError(
                "That query uses a statement or function that is not allowed here. "
                "Only plain SELECT/WITH queries against the `costs` table are permitted."
            )

        # `costs.main.costs` resolves past the scope temp table straight to the base table.
        # Unqualified `costs` is the only supported spelling.
        if re.search(r"\b[\w\"]+\s*\.\s*costs\b", lowered):
            raise ValueError(
                "Refer to the table as plain `costs`, without a database or schema prefix."
            )

        self._check_currency(lowered, scope)

        started = time.perf_counter()
        with self.connect(read_only=True, scope=scope) as con:
            cur = con.execute(cleaned)
            names = [d[0] for d in cur.description]
            rows = cur.fetchmany(limit)
            more = cur.fetchone() is not None

        return {
            "columns": names,
            "rows": [dict(zip(names, r)) for r in rows],
            "row_count": len(rows),
            "truncated": more,
            "scope": scope or "all subscriptions",
            "ms": int((time.perf_counter() - started) * 1000),
        }

    # Columns that hold money. Summing these across different currencies is arithmetic on
    # incompatible units — the result looks authoritative and means nothing.
    _MONEY = ("billedcost", "effectivecost", "listcost", "contractedcost")

    def _check_currency(self, lowered: str, scope: list[str] | None) -> None:
        """Refuse to aggregate money across mixed currencies.

        Both legitimate patterns — grouping by currency, or filtering to one — mention the
        column, so requiring it is enough to let correct queries through.
        """
        if "billingcurrency" in lowered:
            return
        if not re.search(r"\b(sum|avg|total|median)\s*\(", lowered):
            return
        if not any(col in lowered for col in self._MONEY):
            return

        found = self.currencies(scope)
        if len(found) > 1:
            raise ValueError(
                f"This data spans more than one billing currency ({', '.join(found)}), so "
                "totalling a money column across all of it would be meaningless. Either add "
                '"BillingCurrency" to the GROUP BY so each currency is reported separately, '
                'or filter to one currency with WHERE "BillingCurrency" = \'...\'.'
            )

    def subscriptions(self, scope: list[str] | None = None) -> list[dict[str, Any]]:
        """Subscriptions present in the warehouse, with their total spend."""
        with self.connect(read_only=True, scope=scope) as con:
            rows = con.execute(
                'SELECT "SubAccountId" AS id, "SubAccountName" AS name, '
                'coalesce(nullif("BillingCurrency", \'\'), \'unknown\') AS currency, '
                'round(sum("BilledCost"),2) AS cost FROM costs '
                'WHERE "SubAccountId" IS NOT NULL GROUP BY 1,2,3 ORDER BY cost DESC'
            ).fetchall()
        return [{"id": r[0], "name": r[1], "currency": r[2], "cost": r[3]} for r in rows]

    # ------------------------------------------------------------------ meta
    # Small durable facts about the deployment. Deliberately not part of `state`, which is
    # rebuilt from a summary on every boot and is about the last ingest rather than about the
    # installation.

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """One stored fact, or `default` if it was never written."""
        try:
            with self.connect(read_only=True) as con:
                row = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchone()
            return row[0] if row else default
        except Exception as exc:  # noqa: BLE001 - a missing fact must never break a request
            log.debug("meta read failed for %s: %s", key, str(exc)[:200])
            return default

    def set_meta(self, key: str, value: str) -> None:
        """Record one fact. Last write wins; there is no history worth keeping here."""
        try:
            with self.connect() as con:
                con.execute(
                    "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, now()) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    [key, value],
                )
            # Durable immediately. This records that a one-time decision has been made, and a
            # restart that forgot it would put the first-run prompt back in front of someone
            # who has already answered it.
            self.publish()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not record meta %s: %s", key, str(exc)[:200])

    def row_count(self) -> int:
        """How many cost rows exist at all. The question "is this deployment empty"."""
        try:
            with self.connect(read_only=True) as con:
                return int(con.execute("SELECT count(*) FROM costs").fetchone()[0] or 0)
        except Exception:  # noqa: BLE001
            return 0



def _report_failure(body: dict, metric: str) -> str:
    """What to say when Azure reports the job itself as Failed.

    The raw body is `{"status":"Failed","error":{"code":500,"message":"InternalServerError
    occurred during processing the operation Id:..."}}` — a correlation id and nothing else.
    Printed verbatim that reads like an application bug, and the natural response is to check
    permissions and retry, neither of which helps: the request was accepted, the generation ran
    on Azure's side, and it is Azure that failed. Saying which metric was asked for matters
    because this is commonly metric-specific — ActualCost failing while AmortizedCost succeeds
    on the same subscription and period is exactly what happens.
    """
    err = body.get("error") or {}
    code = err.get("code")
    message = str(err.get("message") or "")

    if str(code) in ("500", "InternalServerError") or "internalservererror" in message.lower():
        return (
            f"Azure Cost Management could not generate the {metric} report — it accepted the "
            "request and then failed internally (HTTP 500). This is a fault on Azure's side, "
            "not a permission or configuration problem here, and it is often specific to one "
            "metric: if this was API data, try Amortized, which is generated separately."
        )
    if message:
        return f"Azure reported the {metric} report as failed: {message[:200]}"
    return f"Azure reported the {metric} report as failed: {str(body)[:200]}"


# How many rows to carry in one INSERT statement when binding parameters.
#
# `executemany` re-executes a prepared statement per row, so its cost is per row rather than
# per batch: 7,000 rows of the real 25-column schema took 17.8s locally. Batching the same rows
# into multi-row VALUES statements cut that to 3.4s. Larger chunks are slower again, because
# the statement is compiled with chunk x 25 placeholders and that overtakes the saving.
INSERT_CHUNK = 200

# Above this many rows, stop binding parameters and let DuckDB's CSV reader do it.
#
# Parameter binding is the actual bottleneck on the deployed instance, and it took three
# measurements to see it. The box is only 5.4x slower than a laptop on a plain Python loop and
# scans the whole 20,667-row table in 8ms -- but it inserted 51 rows/s against 2,200 locally, a
# 43x gap that neither CPU nor disk explains. What is 43x slower is the per-value binding layer
# itself.
#
# So the fast path hands DuckDB a file and gets out of the way: values are written as CSV to
# local disk and read back by DuckDB's C++ parser, which binds nothing. Measured locally on the
# real typed schema, 3,000 rows:
#
#   parameter binding    1.13s    2,648 rows/s
#   csv via read_csv     0.07s   45,240 rows/s     <- 17x
#
# The reason this was not the first choice is that a CSV round-trip is where a loader quietly
# corrupts data: a resource group named with a comma, a tag value containing a newline, or a
# NULL arriving back as an empty string. That risk is retired by evidence rather than by
# assumption -- test_bulk_insert.py writes deliberately hostile values through both paths and
# requires the stored rows to be identical, and NULL is carried as a sentinel that cannot occur
# in Azure billing data rather than as an empty field.
#
# Small batches still bind, because creating and parsing a file is not worth it for a few rows.
CSV_THRESHOLD = int(os.getenv("COST_CSV_THRESHOLD", "400"))

# Chosen so it cannot appear in real data: control characters are not legal in Azure resource
# names, tags or meter descriptions. An empty field must stay an empty string, so it cannot be
# used to mean NULL as well.
CSV_NULL = "\x01\x02NULL\x02\x01"


def _column_types() -> dict[str, str]:
    """The declared type of each column, matching `_init_schema`."""
    return {c: ("DOUBLE" if c in NUMERIC
                else ("DATE" if c == "ChargePeriodStart" else "VARCHAR"))
            for c in COLUMNS}


def insert_rows(con: Any, records: Sequence[Sequence[Any]],
                table: str = "costs", chunk: int = INSERT_CHUNK) -> int:
    """Bulk-insert rows into `table`, naming the columns explicitly.

    Named rather than positional: a positional insert depends on the table having been created
    with exactly today's COLUMNS order, and ALTER TABLE appends — so a column added later
    shifts every value after it one place to the left, silently.
    """
    if not records:
        return 0
    if len(records) >= CSV_THRESHOLD:
        try:
            return _insert_via_csv(con, records, table)
        except Exception as exc:  # noqa: BLE001
            # Correctness over speed: if anything about the file path fails, fall back to the
            # slow path that is known to work rather than losing the batch.
            log.warning("csv bulk load failed (%s); falling back to parameter binding", exc)
    return _insert_via_binding(con, records, table, chunk)


def _insert_via_binding(con: Any, records: Sequence[Sequence[Any]],
                        table: str, chunk: int) -> int:
    names = ",".join(f'"{c}"' for c in COLUMNS)
    one = "(" + ",".join("?" * len(COLUMNS)) + ")"
    for i in range(0, len(records), chunk):
        batch = records[i:i + chunk]
        con.execute(
            f"INSERT INTO {table} ({names}) VALUES " + ",".join([one] * len(batch)),
            [v for row in batch for v in row],
        )
    return len(records)


def _insert_via_csv(con: Any, records: Sequence[Sequence[Any]], table: str) -> int:
    """Write the batch as CSV and let DuckDB read it, binding nothing.

    The file goes to the system temp directory rather than next to the database: on App Service
    that is local disk (measured 2,478 fsync/s) while the database's durable home is an SMB
    share (39 fsync/s), and this file is scratch that must never outlive the call.
    """
    names = ",".join(f'"{c}"' for c in COLUMNS)
    types = _column_types()
    coltypes = ", ".join(f"'{c}': '{types[c]}'" for c in COLUMNS)
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="cloudlens-load-")
    os.close(fd)
    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            writer.writerows(
                [[CSV_NULL if v is None else v for v in row] for row in records])
        quoted = path.replace("'", "''")
        con.execute(
            f"INSERT INTO {table} ({names}) SELECT * FROM read_csv('{quoted}', "
            f"header=false, auto_detect=false, columns={{{coltypes}}}, "
            f"delim=',', quote='\"', escape='\"', nullstr='{CSV_NULL}', "
            "new_line='\\n', strict_mode=false)")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return len(records)


def canonical_sub_id(value: Any) -> str | None:
    """One canonical form for a subscription id, whatever shape it arrived in.

    FOCUS exports identify a subscription by its ARM path — `/subscriptions/<guid>` — while the
    Query API, the Cost Details report and Azure's own ActualCost exports use the bare GUID.
    Both are correct; they are just different spellings of the same subscription.

    Storing them verbatim meant one subscription existed twice. Every write replaces the range
    it covers with `WHERE "SubAccountId" = ?`, so a FOCUS load never deleted the report path's
    rows and the report path never deleted FOCUS's: both survived and both were counted. The
    dashboard read $920.98 for a month Azure billed at $476.07 — not a rounding error, and not
    visibly wrong from the screen either, because a doubled total still looks like money.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Case-folded because ARM treats GUIDs case-insensitively and different APIs have shipped
    # different casings; two spellings of one subscription is the whole bug being fixed here.
    if "/subscriptions/" in text.lower():
        text = text.rstrip("/").rsplit("/", 1)[-1]
    return text.lower()


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait, preferring the service's own guidance over our backoff."""
    for header in RETRY_HEADERS:
        value = resp.headers.get(header)
        if value:
            try:
                return min(float(value) + 2, 120.0)
            except ValueError:
                pass
    # The details API commonly asks for ~60s; start there rather than hammering.
    return min(60.0 * (attempt + 1), 180.0)


def _query_failure(resp: httpx.Response | None) -> str:
    """What went wrong with a query, phrased for the person who pressed the button.

    A raw 429 body — `{"error":{"code":"429","message":"Too many requests. Please retry."}}` —
    is the least useful thing to show here, because it invites exactly the wrong response:
    pressing Refresh again, which spends more of the quota that has just run out.

    The wait is quoted from the service rather than guessed. The previous wording invented a
    recovery time of "several minutes", which was wrong in both directions: the client-type
    limit that actually fires refills in seconds, and the advice sent people away from a button
    that would have worked on the next attempt.
    """
    if resp is None:
        return "no response from the query API"
    if resp.status_code == 429:
        wait = ""
        for header in RETRY_HEADERS:
            value = resp.headers.get(header)
            if value:
                try:
                    wait = f" Azure asks for about {int(float(value))}s."
                    break
                except ValueError:
                    pass
        return ("Cost Management is rate-limiting queries for this subscription." + wait +
                " Retrying sooner than that spends what is left of the allowance."
                " FOCUS report reads blob storage instead and is not affected.")
    if resp.status_code in (401, 403):
        return (f"Azure refused the query ({resp.status_code}). This needs Cost Management "
                "Reader on the subscription.")
    return f"query failed {resp.status_code}: {resp.text[:200]}"


def _query_wait(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait after a 429 from the *query* endpoint.

    Separate from `_retry_after` because the two APIs fail on different timescales. A report
    submission is genuinely minutes of work and asks for ~60s; a query answers in single-digit
    seconds, so borrowing that number spent a minute asleep to save an eight-second call and
    made the fast path slower than the one it was meant to replace.
    """
    for header in RETRY_HEADERS:
        value = resp.headers.get(header)
        if value:
            try:
                # Still capped: the service occasionally asks for minutes on a shared limit,
                # and a fast refresh that sleeps that long has stopped being one.
                return min(float(value) + 1, 60.0)
            except ValueError:
                pass
    return QUERY_BACKOFF[min(attempt, len(QUERY_BACKOFF) - 1)]


def _to_date(value: str | None) -> str | None:
    """Azure emits MM/DD/YYYY in MCA exports and ISO elsewhere; accept both."""
    if not value:
        return None
    v = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _periods(months: int) -> list[tuple[str, str, str]]:
    """Whole calendar months, oldest first, ending with the current month to date."""
    today = datetime.now(timezone.utc).date()
    out: list[tuple[str, str, str]] = []
    first = today.replace(day=1)
    starts = [first]
    for _ in range(months - 1):
        starts.append((starts[-1] - timedelta(days=1)).replace(day=1))
    for s in reversed(starts):
        if s == first:
            out.append((s.isoformat(), today.isoformat(), f"{s:%Y-%m} (MTD)"))
        else:
            last = (s.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            out.append((s.isoformat(), last.isoformat(), f"{s:%Y-%m}"))
    return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


warehouse = Warehouse()

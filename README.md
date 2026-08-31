# CloudLens

**See. Analyze. Optimize.** — total clarity for your cloud spend.

An Azure cost dashboard with an agent attached. Tabs answer the questions you always have;
the chat box handles the ones you didn't expect.

Cost data is pulled once into a local DuckDB warehouse, so queries run in milliseconds and can
slice any way you ask. Waste findings come from live Azure inventory, joined back to what each
resource actually cost.

## Deploy it into your own Azure

**One command** — provisions everything and sets up sign-in with your Azure account:

```bash
git clone https://github.com/samy6352/CloudLens-Deploy && cd CloudLens-Deploy
./scripts/deploy.sh --admin you@yourcompany.com          # macOS / Linux
./scripts/deploy.ps1 -Admin you@yourcompany.com          # Windows
```

Around eight minutes, and it provisions everything — web app, AI model, storage, and the role
assignments without which the app runs perfectly and shows an empty estate. It works in any
Entra tenant; nothing here is specific to one organisation.

There is also a [portal button](DEPLOY.md#1-the-portal-button), which needs nothing installed
locally but requires this repository to be public — Azure fetches the template anonymously.

**You need Owner on the subscription** (or Contributor plus User Access Administrator), because
CloudLens reads cost through a managed identity that has to be granted Cost Management Reader.

Full instructions, and the four things that actually go wrong, are in **[DEPLOY.md](DEPLOY.md)**.

## Or run it locally

```bash
pip install -r requirements.txt
az login

export PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export MODEL_DEPLOYMENT_NAME="gpt-4.1-mini"

python ingest.py --months 3          # build the warehouse (a few minutes)
uvicorn app.main:app --port 8100     # http://localhost:8100
```

First start prints a generated `admin` password to the console. For anything beyond one laptop,
set up [SSO](#sharing-it-with-a-team).

## What you get

The rail groups tabs by the question you are asking, not by where the data comes from — **Spend**
(where the money went), **Optimize** (where to spend less), **Monitor** (what changed, what is at
risk), **Govern** (lifecycle and compliance) and **Reports** (export the data, and schedule the
exports). Only the group you are working in is expanded; the rest collapse to a heading and a
count, so the rail is five lines and the section you want rather than twenty-four entries to read
past. The group you are standing in cannot be collapsed out from under you, and the choice is
remembered.

**Overview** — spend, the change against the previous period, daily trend, and the biggest
movers. Everything the average "how are we doing" question needs.

**Per-area tabs** — Compute, Networking, Storage, AI & Data, Integration, Security. Each with
its trend, top services, top regions and top resources.

**Rightsizing** — per-VM CPU average and peak from Azure Monitor, with power state and cost.

**Stale resources** — unattached disks, unassociated public IPs, VMs stopped but not
deallocated, empty App Service plans, old snapshots, orphaned NICs. Each with what it cost last
month, because "12 unattached disks" is trivia and "$28.50 of unattached disks" is a decision.

**End of support** — OS and SQL versions past or near end of support, and what ESU would cost.

**Commitments** — how spend splits across on-demand, reserved and Spot, month by month, and what Spot actually saved against the on-demand rate for the same meter.

**Rate optimization** — reservation and savings plan opportunity from Advisor, deduplicated across its 7/30/60-day lookbacks.

**Retirements** — what Azure is changing that affects you, from Service Health, sorted by deadline.

**Governance** — tag coverage, untagged spend, and VMs without accelerated networking.

**Advisor** — Microsoft's own recommendations and savings estimates.

**Reports** — export any selection as Excel, PowerPoint, CSV, Markdown or JSON.

Rightsizing, Stale resources, End of support and Advisor query Azure live, so they take seconds
and say so. Everything else is a warehouse query and returns in milliseconds.

## Why a local warehouse

The Cost Management Query API takes 2–8 seconds per call, throttles aggressively, and only
groups by its own fixed dimensions. Every breakdown is another round trip.

So CloudLens pulls row-level detail once via `generateCostDetailsReport`, normalises it into a
FOCUS-shaped table, and queries that locally. You get arbitrary grouping, window functions and
full tag analysis, and the responses are milliseconds rather than seconds — on the test estate
(15.5k rows) seven non-trivial queries run in 27 ms total.

Forecasts and budgets aren't in the detail export, so those still hit the live API.

## How it fits together

```
Browser ──► FastAPI ──► gpt-4.1 (plans, explains)
                 │
                 ├─► query_costs   ──► DuckDB (local, milliseconds)
                 ├─► render_chart  ──► DuckDB → chart spec → Chart.js
                 ├─► find_waste    ──► Resource Graph + DuckDB
                 ├─► vm_utilisation──► Azure Monitor
                 └─► forecast / budgets / advisor ──► Azure (live)
```

No MCP, no Foundry Agent Service. The tools are plain Python functions called in-process, and
the model only plans and writes prose. It never invents a figure: chart data always comes from a
warehouse query, and the UI shows the raw result behind every answer.

The frontend is vanilla JS and CSS. No build step, no framework, no CDN. Initial load is about
50 KB gzipped; Chart.js is fetched on demand the first time a chart is drawn.

## Amortized by default

The ingest asks for **AmortizedCost**, not ActualCost. ActualCost bills a reservation as one
lump on the day it was bought, which makes that month look like a spike and every later month
artificially cheap; amortized spreads the purchase across the term it covers and attributes
usage to the resources that consumed it. It also populates `BenefitName` and `BenefitId`,
without which "what are my commitments saving me" cannot be answered at all.

For an estate with no commitments the two are identical, so the switch costs nothing until
there is something to amortise. Where a Cost Management export already exists, CloudLens
prefers **FOCUS** — one schema carrying both views — then AmortizedCost, then ActualCost.

Adding a column to `COLUMNS` migrates an existing warehouse in place rather than requiring a
re-ingest: `CREATE TABLE IF NOT EXISTS` only builds the schema on an empty database, so a
warehouse that predates a column would otherwise keep the old shape and fail every query
naming it. Existing rows get NULL, which is honest — that data genuinely was not collected.
## The map is real geography

The first version of the region view drew a graticule and no coastline, on the reasoning that a
hand-traced map would be recognisable but wrong, and people read landmasses as fact. That
reasoning was right; the answer was not to trace one.

`web/assets/worldmap.js` is generated by `tools/build_worldmap.py` from Natural Earth data
(public domain), projected and simplified once at build time into a single SVG path. Two
encodings keep it small enough to ship: relative linetos, because the delta between neighbouring
coastline points is one or two characters where an absolute coordinate is four or five; and
integer coordinates in a 2000-wide space that renders at about 1000, so rounding error is half a
pixel. That took it from 104 KB to **24 KB raw, about 10 KB gzipped** — no tiles, no CDN, no
mapping bundle.

One path for the whole world, not one per country, so the fill happens in a single operation and
shared borders cannot antialias into faint light seams.

Rings are split where they cross ±180° longitude. Without that, Russia's Chukotka and Kiribati
step from +179 to −179 between consecutive points and draw a hard horizontal line straight
across the map — which is exactly what the first build did.

Regions that are not places — Entra, Defender, marketplace, bandwidth — are counted and named in
the caption rather than plotted, because putting account-level spend somewhere on a map would be
an invention.

## Spend hierarchy

A squarified treemap, laid out against the panel's measured size rather than a notional
rectangle. Squarifying against a fixed box and then stretching the result with percentages throws
away the thing the algorithm works for: the aspect ratios it keeps near 1 get scaled by whatever
the real panel's ratio happens to be. It re-lays out on resize and drops labels that would
overflow their tile.
## The Refresh button

Three choices, because what actually differs between them is where the numbers come from:

**FOCUS report** — a Cost Management export. One schema carrying actual and amortized together,
the best fidelity available. The server picks the export; discovery already ranks FOCUS ahead of
amortized ahead of actual, and skips any export with no storage destination.

**Amortized** — the Cost Details API, spreading reservations across the term they cover. This is
what the dashboard is built on.

**API data** — the Cost Details API, actual cost. What was billed on the day.

The period is fixed at three months, which is what the comparisons need. The menu used to offer
three lookback periods *and* every export it could find — nine of them here, listed by resource
name and storage account — and it fired a live Azure call on every open, so nothing was clickable
for about thirteen seconds. That is a chooser for someone who already knows which blob container
holds their data.

It also used to lie about the result. `ingest()` finished by hardcoding `status: "ready"`, so an
ingest where every subscription failed still reported success, and the browser — which only
tested for `"failed"` — quietly redrew the same numbers. Clicking Refresh appeared to do nothing,
with no error anywhere. It now reports `failed`, `partial` or `ready` honestly, carries the
reason, and shows it in a floating toast rather than a banner inside the tab body that the
reload immediately wipes.

When no export can be read, the reason is now one sentence rather than three exceptions spliced
together and truncated at 300 characters. Every one of those failures has the same shape — the
export's storage account is unreachable or refuses this identity — and what matters is which of
the two it is, because they need opposite fixes. In a tenant that forces
`publicNetworkAccess=Disabled` on storage, FOCUS is simply not available and the message says so
and names the two options that are.

That check now runs **before** anything starts: one blob listing per candidate account, in
parallel, with a short timeout. Where the storage is unreachable the answer comes back in about
six seconds as a refused request, rather than as a background job that spins for half a minute
and then fails. Nothing is started, so the warehouse is not touched.

### A refresh never destroys what it cannot replace

The period being reloaded used to be deleted *first*, at the top of the job, and refilled at the
end. Between those two points — minutes, because the report is generated on Azure's side — the
dashboard read zero. If the report then failed, the data was simply gone: a run against a
subscription whose `ActualCost` report returned HTTP 500 emptied the warehouse and left it that
way.

Now the rows are fetched first and the swap is one transaction: `DELETE` and `INSERT` together,
or neither. A failed, throttled or timed-out report leaves the previous data exactly where it
was, and a reader never sees the window empty.

An empty report is treated the same way. `NoDataFound` is far more often a transient refusal than
a month that genuinely cost nothing, and deleting on that basis destroys good data to record an
absence — so the period is left alone and the run reports `empty`, which is neither the failure
it isn't nor the success it isn't either.

### Reports are queued, not fired in a burst

Cost Management rate-limits report *submission* per subscription. Running three months
concurrently meant three simultaneous submissions, two of which were refused; each then backed
off for a minute or more before its first poll. A three-month refresh took **fifteen minutes**
when it should take about as long as its slowest report.

Submissions are now serialised per subscription with a five-second gap, which stays inside the
limit. Only the POST is queued — generation and download still overlap, which is where the
minutes actually are. Different subscriptions never queue behind each other, since the limit is
not global. The same refresh now takes **four to six minutes**.

Throttling, when it still happens, is bounded in total (`SUBMIT_BUDGET`) rather than by attempt
count: six retries with an escalating backoff could sleep for twelve minutes before the first
poll, which made a doomed refresh indistinguishable from a slow one.

### Progress says what it is waiting on

`done` only moves when a whole period lands, so a refresh spent minutes showing `0/3` with
nothing else to say — and a control that merely looks busy is what makes someone press it again,
or reload the page mid-ingest and assume it failed.

A refresh now gets a **progress strip** under the header: a bar that advances, the wait named in
words, a running clock, and a line saying the dashboard stays usable. The bar is driven by a
weighted estimate — submission counts for 10% of a period, generation 45%, loading 85% — because
a bar driven by completed periods alone sits at zero for most of the run. It never goes
backwards, and never reaches 100% before the run actually ends.

The stripe animation continues even when the width does not, because the width can genuinely sit
still for two minutes while Azure builds a report, and a bar that is both still and silent is
indistinguishable from one that has died.

When Azure reports the job itself as `Failed`, the body is a correlation id and an
`InternalServerError` — which reads like a bug here and invites checking permissions, neither of
which helps. It is reported as what it is: a fault on Azure's side, naming the metric, and
pointing at the other metric, which is generated separately and often works when one does not.

## The daily archive

Every refresh writes a dated copy of what it loaded to blob storage, and the dashboard's Refresh
menu shows where it went.

The warehouse is a working set, not a record: a refresh *replaces* the periods it loads, and
Azure restates cost data for several days after the fact. Without a copy taken at the time,
"what did we think this cost when we reported it?" has no answer.

```
<your-storage-account>/cloudlens/
  focus/2026/08/focus-2026-08-27.parquet
  amortized/2026/08/amortized-2026-08-27.parquet
```

**One blob per dataset per day.** The name is derived from the dataset and the date and nothing
else, so a second refresh the same day overwrites the first rather than accumulating
near-identical copies — someone pressing Refresh four times in an afternoon is correcting
something, not asking for four archives. The overwrite is just a PUT onto the same name.

**The date is the day the archive was taken**, not the last day of data in it. Because cost data
is restated, two archives taken a week apart can cover the same period and differ — which is
exactly the fact the archive exists to preserve. Naming by data coverage would make the second
overwrite the first and destroy it.

**Parquet.** It is what FOCUS exports already use, `exports.py` can read it back, and it is about
a tenth the size of CSV: 12,776 rows is 209 KB. That matters for a file written every day.

Configured with `ARCHIVE_ACCOUNT` and `ARCHIVE_CONTAINER`; unset either to turn it off. The
app's managed identity needs **Storage Blob Data Contributor** on the account.

A failed archive never fails the refresh. By the time it runs the warehouse already holds new
data and the dashboard will show it, so turning an unreachable storage account into a failed
refresh would report a false problem and hide a real success. The outcome is recorded on the
ingest state and surfaced as its own toast instead.

`GET /api/archive` reports what the archive holds and whether it can be reached at all — the two
need different actions, since an empty archive means no refresh has run yet while an unreachable
one means no refresh ever will populate it. `POST /api/archive` writes today's copy from whatever
the warehouse currently holds, for the case where the refresh worked and the archive did not.

### Comparing two days — the History tab

Keeping the snapshots is only worth it if they can be read back, so `GET /api/archive/compare`
and the **History** tab do exactly that: two dated snapshots of the same dataset, joined and
differenced.

Both columns describe the *same period*. Azure revises cost data for several days after the
fact, so a difference is not spend that happened — it is spend that was always there and has
only now been counted. On this estate, one day apart, the June–August total moved by **$9.52
across 30 late-arriving usage records**, concentrated in Foundry Tools. That movement is
invisible from the dashboard, which only ever holds the latest read.

Both files are read straight into DuckDB rather than into Python — they are columnar and
compressed, and the whole question is one join and one aggregate. A `FULL OUTER JOIN`, so a
resource that appears in one read and not the other is reported rather than silently dropped;
an inner join would hide exactly the movements worth looking at. The group-by column is checked
against a fixed list *before* anything is downloaded, since it is interpolated into SQL.

## Showing costs in another currency

The picker in the header converts every figure in the app. It defaults to **As billed**, which
shows exactly what Azure charged and is the only setting that needs no explanation.

Conversion pivots on Azure's own `CostInUsd` — the USD equivalent Microsoft records on the invoice
at the rate they actually applied — rather than re-deriving anything from the billed amount. Non-
USD targets then apply a published reference rate fetched daily from the ECB (via Frankfurter),
falling back to exchangerate-api for pegged currencies the ECB does not publish, such as AED. Set
`FX_RATES` to pin rates instead, for an organisation that books at a fixed internal rate — a live
rate is the *wrong* answer there. If no rate can be obtained the app shows the billed currency and
says why; a number converted at an unknown rate looks authoritative and cannot be reproduced.

There are two conversion paths, because there are two kinds of figure:

* **Warehouse-backed tabs convert in SQL.** `warehouse.connect(currency=…)` shadows the `costs`
  table with converted columns, so every query on that connection is already in the target
  currency. One place, rather than a rate multiplied into twenty different aggregates.
* **Azure-sourced tabs convert at the boundary.** Advisor savings, ESU list prices and the costs
  Resource Graph reports never touch the warehouse, so `currency.convert_money()` walks the
  response and multiplies only the keys on an explicit money allowlist. An allowlist rather than a
  heuristic: a response is full of numbers that must *not* move — counts, percentages, day windows,
  CPU readings — and "looks like money" is not a property a number has.

Thresholds move with the figures they judge. The anomaly detector's floors are money, so they are
scaled by the same rate; leaving a $15 floor at 15 while the data became rupees would flag a
different set of days depending on which currency you happened to be viewing. The estate did not
change, only the units — so the findings do not change either. Likewise a budget's limit is
converted alongside its spend, because a rupee figure measured against a dollar threshold would
draw a percentage that is simply false.

The display currency is a *view*, never storage. The warehouse keeps what Azure billed, so a
converted figure can always be traced back and a change of rate does not rewrite history.

**Exports follow the screen.** A report ordered while the dashboard showed rupees contains rupees
— summary, areas, and the live datasets embedded in it — and the filename carries the code
(`cloudlens-cost-report-20260828-inr.xlsx`), because two files a week apart in different currencies
are otherwise indistinguishable in a downloads folder. The one deliberate exception is **Raw cost
rows**: those are the billed record, and rewriting them would make the export disagree with the
invoice it exists to reconcile against.

### Private storage

Some tenants enforce `publicNetworkAccess=Disabled` and `allowSharedKeyAccess=false` on every
storage account through policy — ARM accepts `Enabled` and returns `Disabled`. Where that is the
case, the archive account has to be reached over a **private endpoint**:

```
vnet-cloudlens (10.0.0.0/16)
  snet-app  10.0.1.0/24  delegated to Microsoft.Web/serverFarms  <- app VNet integration
  snet-pe   10.0.2.0/24  private endpoint -> your storage account
privatelink.blob.core.windows.net  linked to the VNet
```

Two settings make it work: regional VNet integration on the app, and `WEBSITE_DNS_SERVER`
=`168.63.129.16` so name resolution goes through Azure DNS and sees the private zone. The private
IP is RFC1918, so it routes through the VNet without `WEBSITE_VNET_ROUTE_ALL`.

Regional VNet integration needs Basic tier or above, which is why the plan is B1 rather than
Free. The same constraint is why such a storage account cannot be a Cost Management export
destination: exports require shared-key auth, which those policies also forbid.

If your tenant does not enforce those policies — most do not — none of this is needed, and the
templates in `infra/` provision a normal public-endpoint storage account.

## Settings

Things that change how the app gets its data, rather than what the data says.

### Scheduled exports — the fix for a slow refresh

A refresh through the Cost Details API asks Azure to *generate* a report and then waits. One
report is about forty seconds; a three-month load across three subscriptions is nine of them,
queued behind a per-subscription submission limit. That is minutes of waiting for data Azure
could have written to a blob overnight.

A scheduled export inverts it: Azure writes the data on its own timetable, and a refresh becomes
a blob read.

The thing that makes this possible in a governed tenant is **managed identity**. A Cost
Management export normally authenticates to its destination with a storage account key, and this
tenant forces `allowSharedKeyAccess=false` on every account — so the obvious approach fails with
"Key-based authentication is currently disabled on this storage account". Creating the export
with `identity: SystemAssigned` (API version `2023-08-01`) and granting *that* identity Storage
Blob Data Contributor sidesteps keys entirely. Verified working on 2026-08-28.

The Cost exports tab creates them, runs them on demand, and deletes them. One Azure limit shapes
what it offers:

* **One export covers one subscription.** Scheduling across an estate is one export each.

It used to record a second, that FOCUS could not be scheduled at subscription scope. That was
wrong. `2023-08-01` answers a FocusCost request with `Invalid definition type 'FocusCost'; valid
values: 'ActualCost, 'AmortizedCost'` — a 400 that names the type rather than the version, so it
read as a scope restriction. The identical body against `2025-03-01` creates the export and mints
its identity; Azure fills in the FOCUS `dataVersion` (`1.0r2`) itself. The old version also
*omits* FocusCost exports from its list, so ones created in the portal were invisible here.
All three report types are offered now.

`dataOverwriteBehavior: OverwritePreviousReport` matters: without it every run writes a new
folder and the container grows without bound — the same mistake the daily archive avoids by
naming blobs after the date.

### Exporting a selection

The daily archive covers the whole estate, because that is what makes two days comparable. The
other question — "give me just these subscriptions" — is answered here, with its own checkboxes
rather than the header's scope picker, since what you want to export is rarely what you happen to
be looking at.

CSV by default, in the same column order a FOCUS export produces, so the file is interchangeable
with one Azure wrote: `exports.py` can read it back, FinOps tooling ingests it, and it opens in
Excel. Parquet is offered too, at about a tenth the size. The result is written under
`selection/` with the date and a label, so it can never overwrite the daily archive.

The picker in the header narrows everything, and your choice sticks across reloads.

Scope is enforced rather than suggested. When a scope is active, the warehouse connection
shadows the `costs` table with a temp table containing only those subscriptions, so a query
physically cannot see anything else. The model forgetting a `WHERE` clause, or being told to
ignore the filter, still returns in-scope numbers. Live Azure tools get the same scope as an
explicit subscription list.

`test_scope.py` covers the escape routes (`costs.main.costs`, `WHERE 1=1 OR ...`, and friends);
`test_waste_scope.py` does the same for the live tools.

## Sharing it with a team

Sign-in is required for every page and every `/api/*` route. Two modes.

### SSO (recommended)

People sign in with their work account and see only the subscriptions their own Azure RBAC
allows. No accounts to create, no allow-list, no invitations.

```bash
az ad app create --display-name "CloudLens (SSO)" --sign-in-audience AzureADMyOrg
az ad sp create --id <appId>
az ad app update --id <appId> --is-fallback-public-client true \
  --public-client-redirect-uris "https://<your-host>/auth/callback"
```

```bash
AUTH_CLIENT_ID=<appId>
AUTH_TENANT_ID=<tenant the subscriptions live in>
AUTH_REDIRECT_URI=https://<your-host>/auth/callback
AUTH_ADMINS=you@yourcompany.com     # who may trigger an ingest
```

It's a public client with PKCE, so there's no secret to rotate. Set `AUTH_CLIENT_SECRET` if you'd
rather run it as a confidential client.

By default the app works out what someone may see by asking Azure which subscriptions carry a
role assignment for them (transitive, so group access counts). That only needs
`openid profile offline_access`, which users can usually consent to themselves.

Setting `AUTH_DELEGATED_ARM=true` is stronger — the app holds each person's token and calls ARM
as them, so Azure itself refuses anything they aren't entitled to. But it needs
`user_impersonation` on Azure Service Management, which in most tenants an administrator has to
approve. Until they do, nobody can sign in at all. Hence the default.

Either way the entitlement is enforced the same everywhere: the picker, the header tiles, the
warehouse (physically shadowed), and any subscription named in a question that the person can't
see is dropped rather than honoured. Someone with no access gets an explicit 403 explaining
which role to ask for, never an empty page implying zero spend.

One catch in the default mode: the app reads role assignments with its own identity, so it can
only offer a subscription it has Reader on itself. If someone reports a missing subscription,
check that first.

**Guests work.** If the subscriptions live in a different tenant from your colleagues' accounts,
point `AUTH_TENANT_ID` at the tenant that owns the subscriptions and add people there as guests.

### Local accounts

Used when `AUTH_CLIENT_ID` is unset, so the app still runs on a laptop with no tenant config.

```bash
python -m app.auth add priya            # prompts for a password
python -m app.auth add sam --admin      # may also trigger ingests
python -m app.auth list / passwd / remove
```

Accounts live in `data/users.json` (gitignored, PBKDF2-SHA256, never plaintext). The app watches
that file, so adding someone takes effect immediately and removing them ends their session.

A local account is the operator: it sees everything the server's own `az login` can see. That's
the difference from SSO, and why SSO is the right way to hand this to a team.

### Both modes

Anyone signed in can ask questions and read what they're entitled to. Only admins can start an
ingest, since that rewrites shared data.

Scripts use a bearer token instead of a session. Tokens run as the *server*, not as a person, so
they bypass RBAC — automation only:

```bash
export AUTH_API_TOKENS="ci:$(python -c 'import secrets;print(secrets.token_urlsafe(32))')"
curl -H "Authorization: Bearer $TOKEN" localhost:8100/api/warehouse
```

Cookies are `HttpOnly` and `SameSite=Lax` (which is why no CSRF token is needed). Five wrong
passwords lock that username and IP for a minute, doubling to fifteen. Wrong password and
unknown user return the same message and pay the same hashing cost, so neither text nor timing
enumerates accounts.

| Setting | Default | |
|---|---|---|
| `AUTH_CLIENT_ID`, `AUTH_TENANT_ID` | — | turn on Entra SSO |
| `AUTH_DELEGATED_ARM` | `false` | call Azure as the signed-in person; needs admin consent |
| `AUTH_REDIRECT_URI` | `http://localhost:8100/auth/callback` | must match the app registration |
| `AUTH_CLIENT_SECRET` | — | optional; makes it a confidential client |
| `AUTH_ADMINS` | — | emails that may trigger an ingest |
| `AUTH_ALLOW_LOCAL` | `false` | local accounts alongside SSO |
| `AUTH_RBAC_TTL_SECONDS` | `300` | how long a subscription list is cached |
| `AUTH_SESSION_HOURS` | `12` | how long a sign-in lasts |
| `AUTH_SECRET` | generated | set explicitly if you run more than one instance |
| `AUTH_USERS` | — | `name:<hash>[:admin],…` for deployments with no writable disk |
| `AUTH_API_TOKENS` | — | `name:token,…` for scripts |
| `AUTH_COOKIE_SECURE` | auto | force `Secure` behind upstream TLS |
| `AUTH_DISABLED` | `false` | switch auth off — localhost only |

Before putting it on a network: terminate TLS in front of it, set `AUTH_COOKIE_SECURE=true`, and
add the public redirect URI to the app registration. Framing is denied, the signed-in shell is
`no-store`, and `next=` redirects are restricted to paths on this site.

## Safety

- Read-only throughout. Nothing here can change an Azure resource.
- SQL is restricted to a single `SELECT`/`WITH` on a read-only connection. File-reading functions
  (`read_csv`, `read_parquet`, `glob`) and multi-statement input are rejected.
- If a tool fails, the agent says so. It is explicitly forbidden from filling the gap with a
  plausible-looking table, which an earlier build of a sibling app did.
- The model is told today's date and the warehouse's loaded range. Without that it guesses — one
  test run labelled a correct July 2026 figure as "May 2024".
- If every subscription query fails, the code raises rather than reporting a confident `$0`.

## Refreshing

```bash
python ingest.py --months 6          # existing periods are replaced, not duplicated

# or from the running app (admin only)
curl -X POST -H "Authorization: Bearer $TOKEN" "localhost:8100/api/ingest?months=3"
```

`AUTO_INGEST=false` stops it ingesting on first start.

## Deploying to your own subscription

`tools/deploy.ps1` does the whole thing and is safe to re-run:

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me `
  -ProjectEndpoint https://<resource>.services.ai.azure.com/api/projects/<project>
```

It creates the group, plan and app, assigns the managed identity, grants Reader and Cost
Management Reader on the subscription and a data-plane role on the Foundry account, pins
`COST_DB` and `AUTH_DATA_DIR` to `/home`, deploys only git-tracked files, then polls until the
app is actually healthy rather than assuming the upload worked.

Four settings it will not let you get wrong, because each produces an app that looks deployed
and is not: one worker (sessions live in process memory), `COST_DB` and the account file outside
`wwwroot` (a zip deploy wipes both), a Foundry role (subscription Reader does not reach that data
plane, so the dashboard loads and every question 401s), and the managed identity's own access,
which is the ceiling for every user.

**`PROJECT_ENDPOINT` is required.** The app raises on startup without it, so you need an Azure AI
Foundry project and a model deployment even if you only want the dashboard.

### Three things that stop a personal subscription cold

None of these are the app, and all three are quicker to check than to debug.

**App Service quota is zero in most regions.** Credit-based subscriptions start with no compute
almost everywhere, and `az appservice list-locations` lists what the region *offers*, not what you
may create — so it will happily name a region that then refuses. Create a plan and see:

```powershell
az appservice plan create -g rg-cloudlens -n probe --is-linux --sku B1 -l <region>
```

**Model quota is per model.** `gpt-4.1` is often zero while `gpt-4.1-mini` has plenty. Check
before deploying, and pass whatever you find to `-ModelDeployment`:

```powershell
az cognitiveservices usage list -l <region> -o json |
  ConvertFrom-Json | Where-Object limit -gt 0 | Select-Object @{n='q';e={$_.name.value}}, limit
```

Avoid the reasoning models here: the agent sends `temperature=0.1`, which they reject.

**WebDirect subscriptions cannot use the Cost Details API.** Visual Studio, pay-as-you-go and MSDN
are all refused with a 422, so **Refresh data** cannot load a warehouse on them. The live
dashboard figures and the agent work normally; for loaded data, point it at a Cost Management
export and choose **FOCUS report**.

### Signing in with an Azure account

Set two things and the local password disappears:

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -ProjectEndpoint https://... `
  -EntraClientId <app-id> -EntraTenantId <tenant-id> -Admins you@example.com -DelegatedArm
```

Register an app in the tenant that owns the subscription, add
`https://<app>.azurewebsites.net/auth/callback` as a **Web** redirect URI, and pass its ids above.
A client secret is optional: without one this is a public client using PKCE, which is what a
tenant that caps secret lifetimes wants anyway.

The authority is `login.microsoftonline.com/<tenant>` — a single tenant, so people outside that
directory cannot sign in.

### Letting anyone with an Azure account sign in

`-MultiTenant` swaps that single tenant for `organizations`, so a work or school account from
**any** directory can sign in and see whatever their own Azure RBAC allows — across every tenant
they belong to. Their account does not need to exist in yours.

```powershell
./tools/deploy.ps1 -ResourceGroup rg-cloudlens -AppName cloudlens-me -ProjectEndpoint https://... `
  -EntraClientId <app-id> -MultiTenant -Admins you@example.com
```

The registration has to allow it:

```powershell
az ad app update --id <app-id> --sign-in-audience AzureADMultipleOrgs
```

`organizations`, not `common`: personal Microsoft accounts cannot hold Azure RBAC, so admitting
them produces a successful sign-in and an empty dashboard, which is worse than a clear refusal.

This implies `-DelegatedArm`, and the deploy refuses the combination without it. Resolving access
*as the app* means reading role assignments, which only works inside the app's own directory —
so without a delegated token every external user would sign in and see nothing, with the app
looking perfectly healthy.

**One token per tenant.** An ARM token is issued by one directory and refused by every other.
After sign-in the app lists the tenants you can reach, gets a token for each, and records which
subscription each token can read. A directory that refuses — no consent there, or Conditional
Access — is reported rather than silently dropped.

**No Azure access is an answer, not an error.** Someone who signs in with no subscriptions gets
an empty dashboard that says so. Nothing is hidden and nothing pretends to have failed.

### When a tenant requires admin approval

Many organisations disable user consent. Sign-in then stops with `AADSTS90094` — an Entra policy,
not something an application can work around. CloudLens shows the reason and a link to
`/auth/admin-consent`, which starts the tenant-wide consent flow. One administrator of that
directory approves it once and everyone there can sign in afterwards, each with their own access.

**What each person sees is their own Azure RBAC**, resolved one of two ways:

- **Default.** The app asks ARM, as itself, which subscriptions carry a role assignment for that
  person's object id. Transitive, so group-granted access counts.
- **`-DelegatedArm`.** The app uses the signed-in person's own ARM token, so Azure refuses rather
  than the app filtering. Stronger, but it needs admin consent for Azure Service Management
  `user_impersonation`. In your own tenant you are the admin, so this is usually the right choice.

`-Admins` lists who may run a refresh. Everyone else can read and ask, which is the common case.

## Deploying

App Service Linux, Python 3.13, single instance:

```bash
RG=<your-resource-group>; APP=<your-app-name>; LOC=centralindia
az appservice plan create -g $RG -n asp-cloudlens-b1 --is-linux --sku B1 -l $LOC
az webapp create -g $RG -p asp-cloudlens-b1 -n $APP --runtime "PYTHON:3.13"
az webapp config set -g $RG -n $APP --always-on true \
  --startup-file "python -m uvicorn app.main:app --host 0.0.0.0 --port 8000"
az webapp deploy -g $RG -n $APP --src-path deploy.zip --type zip
```

**B1, not Free.** Regional VNet integration — which the archive's private endpoint depends on —
needs Basic or above. Always On comes with it, so the app stops cold-starting.

**Deploy with `--type zip`, never `--type static`.** A static deploy targeting a single file
replaces the whole `wwwroot`, discarding the Oryx build output; the app then fails to start with
`No module named uvicorn` and takes ~20 minutes to restore.

**One worker, one instance.** Sessions and the header snapshot live in process memory, so a
second worker would send a signed-in person to a process that's never heard of them. Scaling out
means moving sessions to Redis first.

**It runs as a managed identity, not as you.** There's no `az login` on App Service, so
`DefaultAzureCredential` falls through to the identity:

```bash
MI=$(az webapp identity assign -g $RG -n $APP --query principalId -o tsv)
for SUB in <subscription-ids>; do
  az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
    --role "Reader" --scope "/subscriptions/$SUB"
  az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
    --role "Cost Management Reader" --scope "/subscriptions/$SUB"
done
az role assignment create --assignee-object-id $MI --assignee-principal-type ServicePrincipal \
  --role "Cognitive Services OpenAI User" --scope <ai-resource-id>
```

A subscription the identity wasn't granted is invisible to everyone, however much access they
personally have. Worth checking after any deployment.

**Keep the warehouse out of `wwwroot`.** A zip deploy replaces `/home/site/wwwroot`, and `data/`
is gitignored, so a warehouse at the default path is deleted by the next deploy and the app
comes back with no data. Point `COST_DB` at `/home`, which is persistent Azure Files:

```bash
az webapp config appsettings set -g $RG -n $APP --settings COST_DB=/home/data/costs.duckdb

# seed it once (Kudu VFS is rooted at /home)
TOKEN=$(az account get-access-token --resource https://management.azure.com/ --query accessToken -o tsv)
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "If-Match: *" \
  --data-binary @data/costs.duckdb \
  "https://$APP.scm.azurewebsites.net/api/vfs/data/costs.duckdb"
```

Configuration goes in app settings, not `.env` — the deployment package excludes `.env`,
`data/users.json` and `data/.session_secret`. Publish accounts as `AUTH_USERS` with a PBKDF2 hash
(`python -m app.auth hash`), and set `AUTH_SECRET` explicitly so sessions survive a restart.

## The `costs` table

| Column | Notes |
|---|---|
| `ChargePeriodStart` | DATE — usage date |
| `BilledCost`, `CostInUsd` | DOUBLE |
| `BillingCurrency` | |
| `SubAccountId`, `SubAccountName` | subscription |
| `ResourceGroup`, `ResourceId`, `ResourceName` | `ResourceName` is derived from the id |
| `ServiceName`, `ServiceFamily`, `ServiceSubcategory`, `MeterName`, `ProductName` | |
| `RegionName`, `PricingQuantity`, `UnitOfMeasure`, `UnitPrice` | |
| `ChargeCategory`, `PricingModel`, `BenefitName`, `PublisherName`, `CostCenter`, `Tags` | |

Column names are mapped from whatever the export actually uses — they differ between EA, MCA and
pay-as-you-go — so downstream code sees one stable schema. Verified against a real MCA export
with 65 source columns.

## Endpoints

Everything under `/api/` needs a session cookie or bearer token. A browser without one is
redirected to `/login`.

| | |
|---|---|
| `GET /api/health` | model, subscription count |
| `GET /api/subscriptions` | subscriptions in the picker, with spend |
| `GET /api/overview[?scope=id,id]` | header figures |
| `GET /api/warehouse` | row count, date range, ingest progress |
| `POST /api/ingest?months=N` | refresh the warehouse — admin only |
| `GET /api/exports` | Cost Management exports visible to you |
| `POST /api/exports/ingest` | load an export — admin only |
| `GET /api/archive` | what the daily archive holds, and whether it is reachable |
| `POST /api/archive?metric=` | write today's archive from the current warehouse — admin only |
| `GET /api/archive/compare` | what Azure restated between two archived days |
| `POST /api/archive/export` | export chosen subscriptions as CSV or Parquet |
| `GET /api/schedules` | scheduled Cost Management exports and where they write |
| `POST /api/schedules` | create a scheduled export with a managed identity — admin only |
| `POST /api/schedules/run` | produce a scheduled export now — admin only |
| `DELETE /api/schedules` | remove a schedule — admin only |
| `GET /api/budgets[?scope=id,id]` | budgets with percent used, headroom and status |
| `POST /api/budgets` | create a tag-filtered budget — needs Cost Management Contributor |
| `POST /api/ask` | streamed answer (SSE) |
| `GET /api/dashboard/executive` | the opening view, in one warehouse pass |
| `GET /api/dashboard/sections` | the navigation rail and what each area costs |
| `GET /api/dashboard/{section}` | one tab: KPIs, trend, services, regions, top resources |
| `GET /api/dashboard/waste` | idle and orphaned resources (live) |
| `GET /api/dashboard/rightsizing` | per-VM CPU with power state and cost (live) |
| `GET /api/dashboard/esu` | end-of-support inventory and ESU cost (live) |
| `GET /api/dashboard/advisor` | Advisor recommendations (live) |
| `GET /api/dashboard/commitments` | reservation, savings plan and Spot coverage, month by month |
| `GET /api/dashboard/governance` | tag coverage and accelerated networking (live) |
| `GET /api/dashboard/rates` | reservation and savings plan opportunity, from Advisor (live) |
| `GET /api/dashboard/health` | retirements and required migrations, from Service Health (live) |
| `GET /api/costs/raw.csv` | the underlying cost rows for the selected subscriptions |
| `GET /api/report/{xlsx,pptx,csv,md,json}` | a report as a plain link; `probe=1` validates without building |
| `GET /api/report/options` | what this person could include |
| `POST /api/report` | the same report from a JSON selection |
| `POST /api/auth/login` · `/logout` | sign in and out |
| `GET /auth/login` → `/auth/callback` | Entra sign-in round trip |
| `GET /api/auth/me` | who's signed in and how much they can see |
| `GET /healthz` | liveness for a load balancer |

## Tests

```bash
python test_auth.py test_scope.py test_download.py test_overview.py    # and the rest
```

Sixteen suites, no framework. They cover auth and SSO against the real middleware stack, scope
enforcement including the escape routes, SQL injection attempts, currency handling, the report
and download contract, the executive view's arithmetic, ESU pricing, budget creation and
read-back, and the daily archive's naming and overwrite rules.

Two of them (`test_exports.py`, `test_waste.py`) hit live Azure, so they need credentials and
will fail in a governed tenant where storage has public network access disabled.

## Subscription scope


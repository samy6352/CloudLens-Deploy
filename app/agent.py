"""The agent loop: model plans, we execute cost functions, model explains.

No MCP, no Foundry Agent Service. The tools live in this process and are called directly, which
keeps the whole path inside code we control — and sidesteps the Foundry MCP token hop entirely.

The loop is deliberately small: send the question with tool definitions, run whatever the model
asks for, feed the results back, stream the answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import cost, waste

log = logging.getLogger("cloudlens.agent")

MAX_ROUNDS = 5

SYSTEM_PROMPT = """You are a focused Azure cost and optimisation assistant. You answer questions \
about spend and waste on the Azure subscriptions the signed-in user can access, and nothing else.

## Your data
You have a **local cost warehouse**: row-level Azure cost detail for every accessible \
subscription, loaded into a DuckDB table called `costs`. Query it with `query_costs` using SQL. \
This is fast (milliseconds) and flexible — prefer it for almost everything about spend.

The `costs` table has these columns (FOCUS-aligned):
- `ChargePeriodStart` DATE — the usage date
- `BilledCost` DOUBLE — the cost for that row
- `BillingCurrency` VARCHAR
- `SubAccountId`, `SubAccountName` — subscription
- `ResourceGroup`, `ResourceId`, `ResourceName`
- `ServiceName` — e.g. 'Virtual Machines', 'Storage'
- `ServiceSubcategory`, `MeterName`, `ProductName`
- `RegionName`, `PricingQuantity`, `UnitOfMeasure`, `UnitPrice`
- `ChargeCategory` — e.g. 'Usage', 'Purchase'
- `PublisherName`, `PricingModel`, `BenefitName`, `CostCenter`, `Tags`

Use `warehouse_status` if you need to know what date range is loaded.

## Waste and optimisation
For "what am I wasting", "stale/orphaned resources", "unattached disks", "what can I clean up":
use **`find_waste`**. It inspects live Azure inventory for idle patterns and reports what each \
item *actually cost*, so lead with the money, not the count.

For "are my VMs busy", "idle VMs", "rightsizing": use **`vm_utilisation`** (CPU average and peak \
from Azure Monitor, plus power state and cost).

For "how do I save money", "reserved instances": use **`advisor_recommendations`**.

These read live Azure, so they take a few seconds — that is expected. Do not use SQL for them; \
the warehouse knows what things cost, not whether they are attached to anything.

When you report waste:
- Lead with the total and the biggest single item. "£X across N resources; the largest is Y at £Z."
- Say the cost window ("in the last 30 days").
- A count with no cost attached (orphaned NICs, unattached NSGs) is clutter, not spend — say so \
rather than implying a saving.
- Never tell the user to delete something. Present the finding and let them decide; some idle \
resources exist deliberately (DR, seasonal, staged rollouts).

## Live fallbacks
Use `cost_summary`, `cost_trend`, `cost_changes` only when the warehouse cannot answer — \
`cost_forecast` and `budgets` are never in the warehouse, so always use those tools for them.

## SQL guidance
- Always `SUM("BilledCost")` and `GROUP BY` what was asked for. Quote column names with double quotes.
- Filter dates with `"ChargePeriodStart" >= DATE '2026-07-01'` style predicates.
- Always `ORDER BY` the cost descending and `LIMIT` sensibly (10-25 rows unless asked for more).
- Round in the presentation, not the SQL, so totals stay accurate.
- If a query returns nothing, say so — do not retry endlessly with variations. But "no rows" is \
not the same as "no spend": if the result carries `warehouse_empty`, nothing is loaded and the \
question is still unanswered, so use the live tools and say the figures are live. Only report \
that spend was zero when a loaded warehouse says so.

## Charts
Call `render_chart` when a picture beats a table — anything over time, any ranking of more than \
about four items, or any share-of-total question. Pick the shape deliberately: `line`/`area` for \
time series, `bar`/`hbar` for rankings, `stacked_bar` for a breakdown over time, `pie`/`doughnut` \
for composition. When you draw a chart, write a one- or two-sentence takeaway rather than \
repeating the whole table — but always **name the top item and its figure**, taken from the \
`charted_values` the tool returns ("Foundry Tools led at $398.09, about a fifth of the month"). \
A takeaway like "the largest service dominates" is useless: the reader wants to know which one, \
and by how much.

## Rules that matter most
1. **Only ever state figures you received from a tool result in this conversation.** You have no \
prior knowledge of this estate. If a tool fails or returns nothing, say so plainly and stop — \
never produce an illustrative or remembered number. A plausible-looking fabricated table is far \
worse than "I couldn't retrieve that", because the user cannot tell the difference.
2. **Always give the period and the currency.** "$459.32 USD in July 2026", never a bare number.
3. **Never total money across billing currencies.** If `warehouse_info` reports more than one \
currency, every money aggregate must either `GROUP BY "BillingCurrency"` or filter to a single \
currency, and you must report each currency separately. Do not convert between currencies — you \
have no exchange rate, and inventing one would be a fabricated number. The warehouse enforces \
this and will reject an unsafe query.
4. **Default to all accessible subscriptions** unless the user names one. Say which scope you used.
5. Partial months are partial. If the period includes today, say "month to date".

## Scope
Azure cost and cost-driven optimisation only. If asked something unrelated — security posture, \
how to build something, application debugging — say this assistant covers Azure cost and waste, \
and suggest what they could ask instead.

## Style
Lead with the direct answer in one or two sentences, then a markdown table if there is more than \
a couple of rows. Round money to 2 decimals. Call out anything notable — a service that doubled, \
a budget above 90%, one subscription dominating the bill. Be brief."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_costs",
            "description": (
                "Run a read-only SQL SELECT against the local cost warehouse (DuckDB table "
                "`costs`). This is the primary tool — fast and flexible. Use it for totals, "
                "breakdowns by any column, trends, comparisons and tag analysis."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string",
                            "description": "A DuckDB SELECT against the `costs` table. Quote column names."},
                    "limit": {"type": "integer", "description": "Max rows to return (default 100)."},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "render_chart",
            "description": (
                "Draw a chart from a SQL query against the cost warehouse. Use this whenever a "
                "trend, comparison or composition is easier to see than to read — daily/monthly "
                "spend over time, top services or resources, share of total, subscription "
                "comparisons. The chart is rendered for the user automatically; you should still "
                "summarise the finding in words, but do NOT repeat the whole table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "SELECT against `costs` returning the columns to plot. "
                                       "Keep it small — aggregate and LIMIT to what should be shown.",
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["line", "area", "bar", "hbar", "stacked_bar", "pie", "doughnut"],
                        "description": "line/area for time series, bar/hbar for rankings, "
                                       "stacked_bar for a breakdown over time, pie/doughnut for share of total.",
                    },
                    "label_column": {"type": "string", "description": "Column for the x-axis or slice labels."},
                    "value_column": {"type": "string", "description": "Numeric column to plot."},
                    "series_column": {
                        "type": "string",
                        "description": "Optional. Splits the data into multiple lines/segments, "
                                       "e.g. ServiceName when plotting spend over time per service.",
                    },
                    "title": {"type": "string", "description": "Short chart title."},
                },
                "required": ["sql", "chart_type", "label_column", "value_column", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_waste",
            "description": (
                "Find idle, orphaned and stale resources — unattached disks, unassociated public "
                "IPs, orphaned NICs, stopped/deallocated VMs, empty App Service plans, old "
                "snapshots, unattached NSGs, empty resource groups — each with what it ACTUALLY "
                "cost, joined from the warehouse. Use for 'what am I wasting', 'stale resources', "
                "'unattached disks', 'what can I clean up', 'orphaned resources'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": ["unattached_disks", "unassociated_public_ips", "orphaned_nics",
                                     "stopped_vms", "deallocated_vms", "empty_app_service_plans",
                                     "old_snapshots", "unused_nsgs", "empty_resource_groups"],
                        },
                        "description": "Which patterns to check. Omit for all of them.",
                    },
                    "days": {"type": "integer", "description": "Cost window in days (default 30)."},
                    "top": {"type": "integer", "description": "Max items listed per category."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vm_utilisation",
            "description": (
                "Per-VM CPU utilisation (average and peak) from Azure Monitor, plus power state "
                "and cost, to identify idle or oversized VMs. Use for 'VM utilisation', 'idle "
                "VMs', 'are my VMs busy', 'can I rightsize'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-back window, 1-90 (default 30)."},
                    "cpu_threshold": {
                        "type": "number",
                        "description": "Average CPU %% below which a VM counts as idle (default 5).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "advisor_recommendations",
            "description": (
                "Azure Advisor recommendations with Microsoft's own estimated annual savings. "
                "Use for 'how can I save money', 'optimisation recommendations', 'reserved "
                "instances'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["Cost", "Performance", "HighAvailability", "Security", "OperationalExcellence"],
                        "description": "Advisor category (default Cost).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "warehouse_status",
            "description": "What data the local warehouse holds: date range, row count, subscriptions, currency.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_subscriptions",
            "description": "List every Azure subscription the signed-in user can access.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cost_summary",
            "description": "LIVE fallback: spend for a period grouped by a dimension, straight from the Cost Management API. Slower than query_costs — use only for periods outside the warehouse range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_ids": {"type": "array", "items": {"type": "string"}},
                    "months_back": {"type": "integer",
                                    "description": "0 = this month to date, 1 = last complete month."},
                    "group_by": {"type": "string",
                                 "enum": ["ServiceName", "ResourceGroupName", "ResourceId",
                                          "ResourceLocation", "MeterCategory", "None"]},
                    "metric": {"type": "string", "enum": ["ActualCost", "AmortizedCost"]},
                    "top": {"type": "integer"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cost_forecast",
            "description": "Projected spend over the coming days. Not available from the warehouse — always use this tool for forecasts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_ids": {"type": "array", "items": {"type": "string"}},
                    "days_ahead": {"type": "integer", "description": "1-90 days."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "budgets",
            "description": "Budgets and how spend is tracking against them. Not in the warehouse — always use this tool for budgets.",
            "parameters": {
                "type": "object",
                "properties": {"subscription_ids": {"type": "array", "items": {"type": "string"}}},
                "required": [],
            },
        },
    },
]


async def query_costs(sql: str, limit: int = 100, _scope: list[str] | None = None) -> dict[str, Any]:
    from .warehouse import warehouse

    try:
        result = warehouse.query(sql, limit=min(int(limit), 500), scope=_scope)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surface SQL errors so the model can correct itself
        return {"error": f"SQL failed: {str(exc)[:400]}"}

    # An empty warehouse and a genuinely empty answer are the same result to SQL, and the
    # difference is the whole meaning: one is "no data is loaded here", the other is "you spent
    # nothing". Asked what last month cost against an unloaded warehouse, the model reported no
    # spend at all — beside a dashboard tile reading the true figure, taken live. A wrong number
    # stated confidently is worse than a refusal, and money is the one place that matters most.
    #
    # Answered here rather than in the prompt because the tool can simply know. The row count is
    # already to hand, and a fact attached to the result does not depend on the model recalling
    # a rule about a state it has no other way to detect.
    if not result.get("rows"):
        loaded = warehouse.summary().get("rows") or 0
        if not loaded:
            result["warehouse_empty"] = True
            result["note"] = (
                "The warehouse holds no cost data at all, so this query cannot answer the "
                "question — this is NOT evidence that nothing was spent. Do not report zero or "
                "no spend. Use the live tools (cost_summary, cost_trend, cost_changes) instead, "
                "and say the figures came from the live API rather than loaded history."
            )
    return result


async def warehouse_status(_scope: list[str] | None = None) -> dict[str, Any]:
    from .warehouse import warehouse

    summary = warehouse.summary()
    return {**summary, "ingest": warehouse.state.get("status", "unknown"),
            "scope": _scope or "all subscriptions",
            "note": "Warehouse is empty; use the live tools instead." if not summary["rows"] else None}


async def render_chart(sql: str, chart_type: str, label_column: str, value_column: str,
                       title: str, series_column: str | None = None,
                       _scope: list[str] | None = None) -> dict[str, Any]:
    """Run the query and shape the result into a chart the browser can draw.

    The data always comes from the warehouse, so a chart can never show invented figures — it is
    the same numbers the user could read in the table, arranged visually.
    """
    from .warehouse import warehouse

    try:
        result = warehouse.query(sql, limit=500, scope=_scope)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - let the model see and fix its SQL
        return {"error": f"SQL failed: {str(exc)[:400]}"}

    rows, columns = result["rows"], result["columns"]
    if not rows:
        return {"error": "The query returned no rows, so there is nothing to chart."}

    missing = [c for c in (label_column, value_column, series_column) if c and c not in columns]
    if missing:
        return {"error": f"Column(s) {missing} are not in the result. Available: {columns}"}

    def num(v: Any) -> float:
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return 0.0

    def label_of(v: Any) -> str:
        """Dates arrive as date/datetime objects; render them readably, not as timestamps."""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m") if (v.day == 1 and not v.hour and not v.minute) \
                else v.strftime("%Y-%m-%d")
        if isinstance(v, date):
            return v.strftime("%Y-%m") if v.day == 1 else v.isoformat()
        text = str(v)
        # DuckDB can hand back 'YYYY-MM-DD 00:00:00' as a string too.
        if len(text) == 19 and text.endswith(" 00:00:00"):
            head = text[:10]
            return head[:7] if head.endswith("-01") else head
        return text

    is_temporal = all(isinstance(r[label_column], (date, datetime)) for r in rows)

    if series_column:
        # One dataset per series, aligned on the shared label axis.
        ordered = sorted(rows, key=lambda r: r[label_column]) if is_temporal else rows
        labels: list[str] = []
        for r in ordered:
            key = label_of(r[label_column])
            if key not in labels:
                labels.append(key)
        names: list[str] = []
        for r in ordered:
            key = str(r[series_column])
            if key not in names:
                names.append(key)
        index = {(label_of(r[label_column]), str(r[series_column])): num(r[value_column])
                 for r in ordered}
        datasets = [
            {"label": n, "data": [index.get((lab, n), 0.0) for lab in labels]}
            for n in names[:12]
        ]
    else:
        # A time series read backwards is misleading; put it in chronological order.
        ordered = sorted(rows, key=lambda r: r[label_column]) if is_temporal else rows
        labels = [label_of(r[label_column]) for r in ordered]
        datasets = [{"label": value_column, "data": [num(r[value_column]) for r in ordered]}]

    spec = {
        "type": chart_type,
        "title": title,
        "labels": labels,
        "datasets": datasets,
        "value_label": value_column,
        "points": sum(len(d["data"]) for d in datasets),
    }

    # Hand the charted figures back to the model. Without them it has no tool result to quote,
    # and the "never state a figure you didn't receive" rule correctly forces it into uselessly
    # vague prose ("the largest service dominates"). These are the same numbers the chart draws,
    # so quoting them cannot introduce an invented value.
    if series_column:
        totals = {d["label"]: round(sum(d["data"]), 2) for d in datasets}
        ranked_series = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:10]
        charted = [{"series": k, "total": v} for k, v in ranked_series]
    else:
        pairs = list(zip(labels, datasets[0]["data"]))
        ranked = pairs if is_temporal else sorted(pairs, key=lambda p: p[1], reverse=True)
        charted = [{"label": lab, "value": round(val, 2)} for lab, val in ranked[:12]]

    # `_chart` is the signal to the agent loop to emit a chart event for the UI.
    return {"_chart": spec, "rendered": True, "series": len(datasets),
            "points": spec["points"], "ms": result["ms"],
            "charted_values": charted,
            "charted_total": round(sum(sum(d["data"]) for d in datasets), 2)}


HANDLERS = {
    "query_costs": query_costs,
    "render_chart": render_chart,
    "warehouse_status": warehouse_status,
    "find_waste": waste.find_waste,
    "vm_utilisation": waste.vm_utilisation,
    "advisor_recommendations": waste.advisor_recommendations,
    "list_subscriptions": cost.list_subscriptions,
    "cost_summary": cost.cost_summary,
    "cost_trend": cost.cost_trend,
    "cost_changes": cost.cost_changes,
    "cost_forecast": cost.cost_forecast,
    "budgets": cost.budgets,
    "top_resources": cost.top_resources,
}

# Warehouse tools take the scope as a temp-table filter; the rest take it as a list of
# subscription ids. Either way the model never chooses the scope — the user does.
SCOPED_TOOLS = {"query_costs", "render_chart", "warehouse_status"}
SUBSCRIPTION_ARG_TOOLS = {"cost_summary", "cost_trend", "cost_changes", "cost_forecast",
                          "budgets", "top_resources", "find_waste", "vm_utilisation",
                          "advisor_recommendations"}


@dataclass
class Event:
    type: str
    data: dict[str, Any]

    def sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps(self.data, default=str)}\n\n"


def _account_endpoint(project_endpoint: str) -> str:
    """The Foundry *account* endpoint, given whatever PROJECT_ENDPOINT holds.

    The Entra path takes a project endpoint —
    `https://<account>.services.ai.azure.com/api/projects/<project>` — because a project is
    what it authenticates against. Key auth talks to the account, so the project path has to
    come off; left on, every request 404s against a URL that looks correct.

    An account endpoint passed in unchanged is returned unchanged.
    """
    base = (project_endpoint or "").strip().rstrip("/")
    marker = "/api/projects/"
    if marker in base:
        base = base.split(marker, 1)[0]
    return base + "/"


class CostAgent:
    def __init__(self) -> None:
        self.endpoint = os.environ["PROJECT_ENDPOINT"]
        self.model = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1")
        self._client: Any = None
        self._credential: Any = None

    async def _openai(self):
        """An OpenAI client for the configured Foundry deployment.

        Two ways in, and the choice is not a preference. Entra is the better one — no secret to
        leak, rotate or check into anything — and it is what runs when nothing says otherwise.

        But it needs a data-plane role assignment on the Foundry account, and granting one
        requires `Microsoft.Authorization/roleAssignments/write`. Plenty of people deploying
        this hold Contributor on a subscription and nothing more: they can create the Foundry
        account and read its keys, and cannot grant themselves permission to use it. Without a
        key path the app is undeployable for them, for a reason that has nothing to do with
        their access to the data it reports on.

        So `AZURE_AI_API_KEY`, when set, is used directly. Same endpoint, same deployment.
        """
        if self._client is None:
            key = os.getenv("AZURE_AI_API_KEY", "").strip()
            if key:
                from openai import AsyncAzureOpenAI

                self._client = AsyncAzureOpenAI(
                    azure_endpoint=_account_endpoint(self.endpoint),
                    api_key=key,
                    api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
                )
                log.info("Foundry auth: API key")
                return self._client

            from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
            from openai import AsyncAzureOpenAI

            self._credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)

            # Entra against the *account* endpoint, not the project.
            #
            # `AIProjectClient` talks to the project API surface, and that is governed by its own
            # data-plane permission which several tenants — this one included — do not grant to
            # a managed identity no matter what is assigned. The symptom is a flat
            # `401 PermissionDenied: Principal does not have access to API/Operation`, with role
            # assignments visibly in place at both the account and the project scope.
            #
            # The inference API underneath is plain Azure OpenAI, it lives on the account
            # endpoint, and `Cognitive Services OpenAI User` is exactly the role that covers it.
            # Same credential, same deployment, same model — one fewer API in the way, and the
            # one that is left is the one the role was designed for.
            self._client = AsyncAzureOpenAI(
                azure_endpoint=_account_endpoint(self.endpoint),
                azure_ad_token_provider=get_bearer_token_provider(
                    self._credential, "https://cognitiveservices.azure.com/.default"),
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
            log.info("Foundry auth: Entra identity (account endpoint)")

        # The project client is a wrapper around the OpenAI client; the key client is one.
        if hasattr(self._client, "get_openai_client"):
            return self._client.get_openai_client()
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._credential is not None:
            await self._credential.close()
            self._credential = None
        await cost.azure.close()

    async def health(self) -> dict[str, Any]:
        subs = await cost.list_subscriptions()
        return {"status": "ok", "model": self.model, "subscriptions": subs["count"],
                "tools": list(HANDLERS)}

    async def ask(self, question: str, history: list[dict] | None = None,
                  scope: list[str] | None = None,
                  scope_names: list[str] | None = None) -> AsyncIterator[Event]:
        client = await self._openai()

        prompt = SYSTEM_PROMPT

        # The model has no clock. Without this it guesses at "last month" and can label a correct
        # figure with a wildly wrong period, which is worse than refusing to answer.
        today = datetime.now().astimezone()
        this_start = today.replace(day=1)
        prev_end = this_start - timedelta(days=1)
        prompt += (
            f"\n\n## Today\nToday is {today:%A, %d %B %Y}. "
            f"'This month' / 'month to date' means {this_start:%B %Y} so far; "
            f"'last month' means {prev_end:%B %Y} in full. "
            "Always name the actual month and year in your answer, and derive dates from this — "
            "never from memory."
        )

        try:
            from .warehouse import warehouse

            stored = warehouse.summary()
            if stored["rows"]:
                prompt += (
                    f"\nThe warehouse holds {stored['from']} to {stored['to']}. Anything outside "
                    "that range needs the live tools, so say so rather than reporting zero."
                )
                if stored["mixed_currency"]:
                    per = ", ".join(f"{t['currency']} {t['cost']:,.2f}"
                                    for t in stored["total_by_currency"])
                    prompt += (
                        f"\n\n## Currency (important)\nThis estate bills in MORE THAN ONE "
                        f"currency: {', '.join(stored['currencies'])} ({per}). Never add these "
                        "together and never convert between them. Every money aggregate must "
                        'GROUP BY "BillingCurrency" or filter to one currency, and your answer '
                        "must report each currency on its own line."
                    )
                elif stored["currency"]:
                    prompt += (
                        f"\nAll amounts in the warehouse are in {stored['currency']}; "
                        "state that currency in your answers."
                    )
        except Exception:  # noqa: BLE001 - the date guidance above is the important part
            pass

        if scope:
            names = ", ".join(scope_names or scope)
            prompt += (
                f"\n\n## Active scope\nThe user has narrowed the analysis to: **{names}**.\n"
                "Every tool call is already filtered to those subscriptions, so do NOT add your "
                "own subscription filter to the SQL — just query `costs` normally, and state the "
                "scope when you answer.\n"
                "**Other subscriptions are invisible to you right now.** If the user asks about "
                "one that is outside the scope, or a query returns nothing for it, that means it "
                "is excluded by their current selection — say exactly that and invite them to "
                "widen the scope using the picker at the top. Never suggest the data is missing, "
                "failed to load, or needs re-ingesting: you cannot tell from here, and saying so "
                "would be misleading."
            )
        else:
            prompt += "\n\n## Active scope\nAll accessible subscriptions. Say so when answering."

        messages: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        messages += history or []
        messages.append({"role": "user", "content": question})

        for _ in range(MAX_ROUNDS):
            try:
                stream = await client.chat.completions.create(
                    model=self.model, messages=messages, tools=TOOLS,
                    tool_choice="auto", temperature=0.1, stream=True,
                )
            except Exception as exc:  # noqa: BLE001
                yield Event("error", {"message": _friendly(exc)})
                return

            # Stream text straight through, while reassembling any tool calls from their deltas.
            content: list[str] = []
            pending: dict[int, dict[str, str]] = {}
            try:
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta is None:
                        continue

                    if delta.content:
                        content.append(delta.content)
                        yield Event("delta", {"text": delta.content})

                    for tc in delta.tool_calls or []:
                        slot = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function and tc.function.name:
                            slot["name"] += tc.function.name
                        if tc.function and tc.function.arguments:
                            slot["args"] += tc.function.arguments
            except Exception as exc:  # noqa: BLE001
                yield Event("error", {"message": _friendly(exc)})
                return

            calls = [pending[i] for i in sorted(pending)]

            if not calls:
                answer = "".join(content)
                yield Event("done", {"messages": messages[1:] + [{"role": "assistant", "content": answer}]})
                return

            messages.append({
                "role": "assistant",
                "content": "".join(content) or None,
                "tool_calls": [
                    {"id": c["id"], "type": "function",
                     "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in calls
                ],
            })

            # Independent reads — run them together rather than one at a time.
            async def run(call: dict[str, str]) -> tuple[dict[str, str], Any, float]:
                started = time.perf_counter()
                try:
                    args = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                handler = HANDLERS.get(call["name"])
                if handler is None:
                    return call, {"error": f"Unknown tool {call['name']}"}, 0.0
                # Scope is injected by us, never chosen by the model: warehouse tools get the
                # temp-table filter, live Azure tools get the subscription id list.
                if call["name"] in SCOPED_TOOLS and scope:
                    args["_scope"] = scope
                elif call["name"] in SUBSCRIPTION_ARG_TOOLS and scope:
                    args["subscription_ids"] = scope
                try:
                    result: Any = await handler(**args)
                except cost.CostError as exc:
                    result = {"error": str(exc)}
                except TypeError as exc:
                    result = {"error": f"Bad arguments for {call['name']}: {exc}"}
                except Exception as exc:  # noqa: BLE001
                    log.exception("tool %s failed", call["name"])
                    result = {"error": f"{call['name']} failed: {str(exc)[:300]}"}
                return call, result, time.perf_counter() - started

            for call in calls:
                try:
                    args = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield Event("tool", {"id": call["id"], "name": call["name"],
                                     "arguments": args, "status": "running"})

            for call, result, elapsed in await asyncio.gather(*(run(c) for c in calls)):
                failed = isinstance(result, dict) and "error" in result
                try:
                    args = json.loads(call["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}

                # A chart is drawn by the browser, not described back to the model. Emit the spec
                # as its own event and keep it out of the transcript — the model already knows the
                # figures from the query, and echoing the full series would waste context.
                chart = result.pop("_chart", None) if isinstance(result, dict) else None
                if chart:
                    yield Event("chart", chart)

                yield Event("tool", {
                    "id": call["id"], "name": call["name"], "arguments": args,
                    "status": "failed" if failed else "done",
                    "result": result, "ms": int(elapsed * 1000),
                })
                messages.append({"role": "tool", "tool_call_id": call["id"],
                                 "content": json.dumps(result, default=str)[:20000]})

        yield Event("error", {"message": f"Gave up after {MAX_ROUNDS} tool rounds. Try a narrower question."})


def _friendly(exc: Exception) -> str:
    """Turn an exception into something the reader can act on.

    The advice has to match where this is running. The old message said "run az login" for any
    401 — which is right on a laptop and useless in App Service, where there is no CLI and no
    interactive session. Telling an operator to do something impossible costs more than saying
    nothing, because they try it before looking for the real cause.

    The detail is kept on the end rather than swallowed. It is the difference between a token
    that was refused (a missing role assignment) and one that was never issued at all (the
    identity is off, or outbound is blocked) — and those have completely different fixes.
    """
    text = str(exc)
    log.warning("model call failed: %s", text[:600])

    hosted = bool(os.getenv("WEBSITE_SITE_NAME"))
    detail = text[:200].strip()

    if "401" in text or "Unauthorized" in text or "PermissionDenied" in text or "403" in text:
        if hosted:
            return (
                "The model refused this app's identity. Its managed identity needs a data-plane "
                "role on the Foundry account — 'Azure AI Developer' or 'Cognitive Services "
                "OpenAI User' — and role changes take a few minutes plus an app restart to "
                f"take effect. ({detail})"
            )
        return f"Could not authenticate to the model. Run 'az login' and try again. ({detail})"

    # No token at all is a different failure from a token that was refused, and the message has
    # to say which — one is a role assignment, the other is the identity or the network.
    if "ManagedIdentityCredential" in text or "DefaultAzureCredential" in text \
            or "failed to retrieve a token" in text.lower():
        return (
            "No token could be obtained for this app. Check that the App Service managed "
            f"identity is switched on and can reach the identity endpoint. ({detail})"
        )
    if "429" in text:
        return "The model is rate-limited right now. Try again shortly."
    if "404" in text:
        return (
            f"Model deployment not found. Check MODEL_DEPLOYMENT_NAME and PROJECT_ENDPOINT. "
            f"({detail})"
        )
    return f"Request failed: {text[:300]}"

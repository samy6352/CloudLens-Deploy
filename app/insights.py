"""FinOps analysis: everything the dashboard knows, read at once and ranked by what it is worth.

Every tab in this app answers a question you already had. This module is for the questions you
did not think to ask — it runs the same sources the tabs run, then looks *across* them for the
things that only show up when you hold two answers side by side. A VM that is oversized is a
Rightsizing row; a VM that is oversized *and* idle overnight *and* untagged *and* in the
subscription that blew its budget is a conversation.

Three rules, and they are the whole design:

1. **Nothing is invented.** Every number in a finding is carried from a source that produced it,
   with the source named. There is no model in this path — no summarising, no estimating, no
   "approximately". If a figure cannot be traced to a tab you could open yourself, it does not
   appear. The `basis` on every finding is not documentation, it is the finding's warrant.

2. **Savings are ceilings, never forecasts.** "Stop this and you stop paying for it" is a fact.
   "You will save $X" is a prediction, and this module does not make predictions. Each finding
   states what stopping would avoid *at the observed rate*, and says so.

3. **Confidence is stated, and it is about the evidence, not the arithmetic.** An unattached disk
   is `high`: the resource graph says nothing is attached, and that is not a judgement. A VM that
   looks idle is `medium`: two weeks of CPU is a good sample and not proof. Anything that depends
   on intent — is this dev/test? is this deliberate? — is `low` and says what would settle it.

Sources are gathered concurrently and independently: an estate where Advisor is throttled should
still get its waste findings, so a failure is recorded against that source and the rest proceed.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("cloudlens.insights")

# A finding has to clear this to be worth someone's attention. Below it the reading time costs
# more than the finding saves — and a page of trivia is a page nobody opens twice, which is the
# only way an advisory surface actually fails.
MIN_IMPACT = 5.0

# How many findings to return. Not a page: a shortlist. Twenty ranked findings is a backlog,
# and a backlog gets triaged once and abandoned.
TOP_N = 12

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


class Finding:
    """One thing worth doing, with the evidence for it.

    Deliberately not a dataclass with defaults: every field here is required because a finding
    missing its basis or its confidence is exactly the kind of authoritative-looking claim this
    module exists to avoid.
    """

    __slots__ = ("kind", "title", "detail", "impact", "currency", "severity", "confidence",
                 "basis", "source", "evidence", "action", "tab", "caveat", "rank_impact",
                 "impact_period", "because", "risk", "effort", "steps")

    def __init__(self, *, kind: str, title: str, detail: str, impact: float, currency: str,
                 severity: str, confidence: str, basis: str, source: str,
                 evidence: list[dict[str, Any]] | None = None,
                 action: str = "", tab: str = "", caveat: str = "",
                 impact_period: str = "window", rank_impact: float | None = None,
                 because: str = "", risk: str = "", effort: str = "",
                 steps: list[str] | None = None) -> None:
        self.kind = kind
        self.title = title
        self.detail = detail
        self.impact = round(float(impact or 0.0), 2)
        self.currency = currency
        self.severity = severity
        self.confidence = confidence
        self.basis = basis
        self.source = source
        self.evidence = evidence or []
        self.action = action
        self.tab = tab
        self.caveat = caveat
        # What period the impact covers. Advisor reports annually and everything else reports
        # over the window, so the two are not the same kind of number and the panel must not
        # print them as though they were.
        self.impact_period = impact_period
        # What to sort on. Normally the impact itself — but a figure covering a different period
        # has to be brought onto the same scale first, or an annual estimate outranks a real
        # monthly cost twelve times its size. Displayed value stays as the source reported it.
        self.rank_impact = self.impact if rank_impact is None else round(float(rank_impact), 2)
        # The three things a consultant says that a report does not.
        #
        # `because` is the likely cause — the sentence that turns a number into a situation.
        # Stated as a hypothesis where it is one, because the data shows what happened and
        # rarely why.
        #
        # `risk` is what could go wrong if you act. Anyone can list savings; the value of a
        # senior opinion is knowing which of them will page you at 3am. A recommendation with
        # no stated downside is a recommendation nobody should trust.
        #
        # `effort` sets expectations against the impact, so a reader can sequence the work
        # rather than starting at the top and stalling on the hardest item.
        self.because = because
        self.risk = risk
        self.effort = effort
        self.steps = steps or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "title": self.title, "detail": self.detail,
            "impact": self.impact, "currency": self.currency,
            "impact_period": self.impact_period,
            "severity": self.severity, "confidence": self.confidence,
            "basis": self.basis, "source": self.source,
            "evidence": self.evidence[:8], "action": self.action,
            "tab": self.tab, "caveat": self.caveat,
            "because": self.because, "risk": self.risk,
            "effort": self.effort, "steps": self.steps,
        }


def _money(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------- sources

async def _gather(subs: list[str] | None, days: int,
                  currency: str | None) -> tuple[dict[str, Any], dict[str, str]]:
    """Run every source at once. A source that fails is named, not hidden.

    Concurrently because they are independent and most of the wall time is Azure's round trip —
    sequentially this took the better part of a minute on three subscriptions. `return_exceptions`
    rather than a gather that aborts: an estate where Advisor is throttled should still get its
    warehouse findings, and "we could not read X" is itself worth showing.
    """
    from . import anomalies, shutdown, tags, waste
    from .commitments import get_commitments

    # `main.offload` does exactly this, but importing main from here would make the dependency
    # circular — main imports this module to serve the endpoint. Two lines are cheaper than the
    # deferred-import dance, and DuckDB is safe on a worker thread because each call opens and
    # closes its own connection.
    async def offload(fn: Any, *args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(functools.partial(fn, *args, **kwargs))

    async def budgets_of() -> Any:
        from . import cost
        return await cost.budgets(subs)

    async def anomalies_of() -> Any:
        return await offload(anomalies.analyse, subs, days=max(30, days),
                             display_currency=currency)

    async def tags_of() -> Any:
        return await offload(tags.cost_by_tag, subs, days=days, currency=currency)

    async def commitments_of() -> Any:
        c = get_commitments()
        return await offload(c.coverage, subs, days=days, currency=currency)

    # Per-source timing, returned with the result. Without it "the analysis is slow" is
    # unactionable: eight sources run at once and the wall time is whichever is slowest, so the
    # only useful question is *which one*, and that has to be measured rather than guessed.
    timing: dict[str, int] = {}

    async def timed(name: str, coro: Any) -> Any:
        t0 = time.monotonic()
        try:
            return await coro
        finally:
            timing[name] = int((time.monotonic() - t0) * 1000)

    jobs = {
        "waste": waste.find_waste(subscription_ids=subs, days=days, top=60),
        "rightsizing": waste.vm_utilisation(subscription_ids=subs, days=min(days, 90)),
        "advisor": waste.advisor_recommendations(subscription_ids=subs, category="Cost"),
        "shutdown": shutdown.find_schedulable(subs, days=min(days, 14)),
        "anomalies": anomalies_of(),
        "budgets": budgets_of(),
        "tags": tags_of(),
        "commitments": commitments_of(),
    }

    names = list(jobs)
    started = time.monotonic()
    results = await asyncio.gather(*(timed(n, jobs[n]) for n in names),
                                   return_exceptions=True)

    data: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            failed[name] = str(result)[:200]
            log.info("insights: %s unavailable: %s", name, str(result)[:200])
        else:
            data[name] = result
    total = int((time.monotonic() - started) * 1000)
    log.info("insights: %d sources in %dms — slowest %s", len(data), total,
             ", ".join(f"{k} {v}ms" for k, v in
                       sorted(timing.items(), key=lambda kv: -kv[1])[:3]))
    return data, failed, timing


# -------------------------------------------------------------------- findings

def _from_waste(d: dict[str, Any], cur: str) -> list[Finding]:
    """Resources that exist, cost money, and are attached to nothing.

    The strongest findings in the set, because they rest on inventory rather than inference: a
    disk with no owner is a fact from Resource Graph, not a judgement about how something is
    used. Grouped by check so the page says "12 unattached disks, $340" rather than listing
    twelve rows of $28 that individually clear no threshold and collectively matter.
    """
    out: list[Finding] = []
    for group in d.get("findings", []):
        items = group.get("items", [])
        cost = _money(group.get("cost"))
        if not items or cost < MIN_IMPACT:
            continue
        label = str(group.get("title") or group.get("kind") or "Idle resources")
        out.append(Finding(
            kind="waste",
            title=label,
            detail=f"{len(items)} resource{'s' if len(items) != 1 else ''} "
                   f"billed over the period while attached to nothing.",
            impact=cost,
            currency=cur,
            severity="high" if cost >= 100 else "medium",
            confidence="high",
            basis="Azure Resource Graph reports these as unattached or stopped-but-allocated; "
                  "the cost is their actual billed amount from the warehouse over the window, "
                  "not an estimate. Deleting them stops that charge at the observed rate.",
            source="Orphaned resources",
            evidence=[{"name": i.get("name"), "group": i.get("group"),
                       "cost": _money(i.get("cost")), "detail": i.get("detail")}
                      for i in items[:8]],
            because="Almost always the residue of something deleted in a hurry: a VM removed from"
                    " the portal takes its NIC but leaves the disk and the public IP, and a resource"
                    " group torn down by hand leaves whatever was locked. It is rarely deliberate,"
                    " which is why nobody is watching it.",
            risk="Low, but not zero. A disk detached last week may be someone's rollback plan, and"
                 " a public IP that looks unused may be whitelisted downstream by a partner who"
                 " will not notice until it is gone. Snapshot before deleting anything you did not"
                 " create yourself.",
            effort="Low — an hour, mostly spent confirming ownership rather than deleting.",
            steps=[
                "Export the list and check each against a change record or the tag that owns it.",
                "Snapshot anything with data on it; the snapshot costs a fraction of the disk.",
                "Delete in one batch, then set a policy to catch the next ones automatically.",
            ],
            action="Confirm nothing is waiting to reattach, then delete.",
            tab="waste",
        ))
        return out


def _from_rightsizing(d: dict[str, Any], cur: str) -> list[Finding]:
    """Machines that are running and barely used.

    Weaker than waste and labelled so: low CPU is evidence of low *processor* use, and a machine
    can be busy on memory, disk or network while its CPU sleeps. The finding says what it saw and
    what it cannot see, rather than asserting the VM is oversized.
    """
    vms = [v for v in d.get("vms", []) if v.get("cpu_avg") is not None]
    idle = [v for v in vms
            if _money(v.get("cpu_avg")) < 5 and _money(v.get("cpu_peak")) < 20
            and _money(v.get("cost")) >= 1
            and str(v.get("state") or "").lower().find("dealloc") < 0]
    if not idle:
        return []
    total = sum(_money(v.get("cost")) for v in idle)
    if total < MIN_IMPACT:
        return []
    return [Finding(
        kind="rightsizing",
        title=f"{len(idle)} running VM{'s' if len(idle) != 1 else ''} averaging under 5% CPU",
        detail="Billed at full rate for the whole window while the processor was almost idle.",
        impact=total,
        currency=cur,
        severity="high" if total >= 200 else "medium",
        confidence="medium",
        basis="Average and peak CPU come from Azure Monitor over the window; the cost is the "
              "billed amount from the warehouse. Both are measured. What is *not* measured is "
              "memory, disk and network — a machine can be busy on those while its CPU sleeps, "
              "so this identifies candidates to check rather than machines to resize.",
        source="Rightsizing",
        evidence=[{"name": v.get("name"), "size": v.get("size"),
                   "avg_cpu": v.get("cpu_avg"), "max_cpu": v.get("cpu_peak"),
                   "cost": _money(v.get("cost"))} for v in
                  sorted(idle, key=lambda x: -_money(x.get("cost")))[:8]],
        because="Usually one of three things: a machine sized from a vendor's worst-case sheet"
                " rather than from measurement, a workload that has since moved to a managed"
                " service and left its host behind, or a lift-and-shift that kept the on-prem"
                " spec because nobody wanted to be the one who made it slower.",
        risk="Real, and it is why this is a candidate list rather than a work order. CPU is one"
             " of four dimensions — a machine can be idle on the processor and pinned on memory"
             " or IOPS, and downsizing on CPU alone is how a database gets throttled. Resizing"
             " also needs a reboot, so it is a change window, not a click.",
        effort="Medium — the resize is minutes, the verification is a week of watching.",
        steps=[
            "Pull memory and disk IOPS for the same window before deciding anything.",
            "Move one machine down a single size within the same family; the family keeps the"
            " same storage and network characteristics, so only the CPU and memory change.",
            "Watch it for a full business cycle — including whatever runs at month end.",
            "Repeat for the rest only once the first has survived one.",
        ],
        action="Check memory and I/O before resizing. A smaller SKU in the same family is "
               "usually a restart rather than a rebuild.",
        tab="rightsizing",
        caveat="Peak CPU under 20% means no spike was missed in the window — but a monthly "
               "batch job outside it would not appear.",
    )]


def _from_shutdown(d: dict[str, Any], cur: str) -> list[Finding]:
    """Machines billed for 168 hours a week and used for about 60."""
    items = d.get("candidates", [])
    saving = _money(d.get("total_saving"))
    if not items or saving < MIN_IMPACT:
        return []
    return [Finding(
        kind="schedule",
        title=f"{len(items)} VM{'s' if len(items) != 1 else ''} busy only in business hours",
        detail="Running around the clock with activity confined to a predictable daily window.",
        impact=saving,
        currency=cur,
        severity="medium",
        confidence="medium",
        basis="Hourly CPU from Azure Monitor shows activity concentrated in a repeating window. "
              "The figure is the billed cost of the hours outside it — an upper bound on what a "
              "schedule could avoid, not a forecast: a deallocated VM still bills for its disks, "
              "and anything with a reservation attached keeps paying regardless.",
        source="Shutdown savings",
        evidence=[{"name": v.get("name"), "hours": v.get("busy_hours"),
                   "cost": _money(v.get("monthly_cost") or v.get("cost")),
                   "saving": _money(v.get("saving"))} for v in items[:8]],
        because="Dev and test environments are built to be always-on because production is, and"
                " nobody goes back to change it once the project ships. The pattern here — busy"
                " on weekdays, flat overnight and at weekends — is people, not workload.",
        risk="Moderate, and mostly about trust. The first time someone's environment is down"
             " when they need it at 8pm, they will ask for the schedule to be removed and you"
             " will not get a second attempt. Agree the window with the people who use it, and"
             " give them a documented way to start a machine early.",
        effort="Low to set up, and it is the single highest-leverage change on this list —"
               " roughly two thirds of the week costs nothing to give back.",
        steps=[
            "Confirm each machine is genuinely dev/test — production is excluded by tag here,"
            " but only where the tag exists.",
            "Start with shutdown only, no auto-start: the risk of a machine being off is much"
            " smaller than the risk of one silently coming back on.",
            "Publish the window and the manual override before the first night it runs.",
        ],
        action="Production is already excluded by tag. Confirm the remainder are dev/test, "
               "then attach a start/stop schedule.",
        tab="shutdown",
        caveat="Excludes disks, which keep billing while a VM is deallocated.",
    )]


def _from_anomalies(d: dict[str, Any], cur: str) -> list[Finding]:
    """Days that did not look like the others — the only source here that is time-sensitive.

    Ranked above savings of the same size on purpose. An unattached disk costs the same next
    month as this one; a spike that nobody has explained is still running.
    """
    found = [a for a in d.get("anomalies", []) if _money(a.get("delta")) > 0]
    if not found:
        return []
    out: list[Finding] = []
    for a in sorted(found, key=lambda x: -_money(x.get("delta")))[:3]:
        delta = _money(a.get("delta"))
        if delta < MIN_IMPACT:
            continue
        baseline = _money(a.get("baseline"))
        pct = f"{a.get('percent')}%" if a.get("percent") is not None else "sharply"
        out.append(Finding(
            kind="anomaly",
            title=f"{a.get('key') or 'Spend'} rose {pct} on {a.get('day')}",
            detail=f"{a.get('dimension') or 'Spend'} moved from a typical "
                   f"{baseline:,.2f} to {_money(a.get('cost')):,.2f} in one day.",
            impact=delta,
            currency=cur,
            severity=a.get("severity") or "medium",
            confidence="high" if delta >= 50 else "medium",
            basis="The day is compared against a rolling median of the same kind of day — "
                  "weekdays against weekdays — using median absolute deviation, so one earlier "
                  "spike does not hide a later one. The impact shown is that single day's excess "
                  "over the baseline. If the cause is still running, the recurring cost is "
                  "larger than this figure, not smaller.",
            source="Anomalies",
            evidence=[{"day": a.get("day"), "cost": _money(a.get("cost")),
                       "baseline": baseline, "dimension": a.get("dimension"),
                       "key": a.get("key")}],
            because="A single-day step change on one dimension is almost never organic growth."
                    " It is a deployment, a scale-out that did not scale back, a batch job that"
                    " reran, or a new resource created for a test and left running. The shape"
                    " tells you which: a step that stays up is provisioning, a spike that"
                    " returns is a job.",
            risk="The risk here is inaction, not action. Cost data lands two days late, so if"
                 " this was provisioning rather than a one-off, it has already been running"
                 " unnoticed for longer than the chart shows and will keep running until"
                 " someone looks.",
            effort="Low — this is fifteen minutes in the activity log, not a project.",
            steps=[
                f"Open the activity log for {a.get('day')} filtered to writes, and look for"
                " creates and scale operations.",
                "Check whether the level came back down afterwards. If it did not, this is"
                " still costing you today.",
                "If it was intended, note it — an expected spike that gets flagged every month"
                " trains people to ignore the page.",
            ],
            action="Find what changed that day — a deployment, a scale-out, a new resource — "
                   "and confirm it was intended.",
            tab="anomalies",
        ))
    return out


def _from_budgets(d: dict[str, Any], cur: str) -> list[Finding]:
    """Budgets already breached. Not a saving — a commitment that has been broken."""
    over = [b for b in d.get("budgets", [])
            if b.get("percent_used") is not None and _money(b.get("percent_used")) >= 100]
    if not over:
        return []
    worst = max(over, key=lambda b: _money(b.get("percent_used")))
    excess = sum(max(0.0, _money(b.get("current_spend")) - _money(b.get("amount")))
                 for b in over)
    return [Finding(
        kind="budget",
        title=f"{len(over)} budget{'s' if len(over) != 1 else ''} exceeded",
        detail=f"{worst.get('name')} is at {_money(worst.get('percent_used')):.0f}% of its "
               f"{_money(worst.get('amount')):,.0f} limit.",
        impact=excess,
        currency=cur,
        severity="critical",
        confidence="high",
        basis="Both the limit and the spend against it come from Azure Cost Management's own "
              "budget records — this is Azure's arithmetic, read back, not ours. The impact "
              "shown is the amount over the limit, which is money already spent rather than "
              "money available to save.",
        source="Budgets",
        evidence=[{"name": b.get("name"), "amount": _money(b.get("amount")),
                   "spend": _money(b.get("current_spend")),
                   "percent": _money(b.get("percent_used")),
                   "subscription": b.get("subscription")} for b in over[:8]],
        because="A budget this far over is usually a limit that was set once, for a smaller"
                " estate, and never revisited — not a sudden overspend. Either way the alert"
                " has been firing for a while, and an alert that fires continuously has already"
                " stopped being read.",
        risk="Leaving it is the risk. A permanently-breached budget is worse than no budget:"
             " it trains everyone on the distribution list to delete the mail, so the one"
             " month it matters, nobody looks.",
        effort="Low — a decision, not a project. Half an hour with whoever owns the number.",
        steps=[
            "Decide which is wrong: the limit or the spend. Both are legitimate answers.",
            "If the limit is stale, reset it against the last three months, not against hope.",
            "If the spend is wrong, the findings below are where it went.",
            "Either way, do it before the period rolls over and the alert resets.",
        ],
        action="Either the limit was wrong or the spend was. Decide which before the next "
               "period rolls over and the alert stops firing.",
        tab="budgets",
    )]


def _from_advisor(d: dict[str, Any], cur: str, days: int) -> list[Finding]:
    """Microsoft's own cost recommendations, kept separate and labelled as theirs."""
    recs = d.get("recommendations", [])
    total = _money(d.get("estimated_annual_savings"))
    if not recs or total < MIN_IMPACT:
        return []
    return [Finding(
        kind="advisor",
        title=f"Azure Advisor has {len(recs)} cost recommendation"
              f"{'s' if len(recs) != 1 else ''}",
        detail="Microsoft's own analysis of this estate, with its own savings estimates.",
        impact=total,
        currency=cur,
        impact_period="year",
        # Advisor reports annually and every other finding reports over the window. Ranking on
        # the raw figure put a 5,800/year estimate above a 209 disk that is being billed right
        # now — twelve times the apparent size for a twelfth of the comparability. Scaled to the
        # window for ordering only; the displayed number stays Advisor's own.
        rank_impact=total * (days / 365.0),
        severity="medium",
        confidence="medium",
        basis="These figures are Azure Advisor's, reported unchanged. They are annualised "
              "estimates produced by Microsoft from usage patterns — this app does not "
              "recompute them, and does not vouch for them. They are shown because Advisor sees "
              "things the cost export cannot, such as reservation break-even.",
        source="Advisor",
        evidence=[{"problem": r.get("problem"), "resource": r.get("resource"),
                   "savings": _money(r.get("annual_savings"))} for r in recs[:8]],
        because="Advisor sees things a cost export cannot — reservation break-even against your"
                " actual run rate, SKU deprecations, sizing benchmarks from the fleet. It is"
                " worth reading precisely because it is not looking at the same data as the"
                " rest of this page.",
        risk="Advisor is confident by design and does not know your commitments. It will"
             " recommend a three-year reservation for a workload you are decommissioning next"
             " quarter, because nothing in the telemetry told it so. Read each one against"
             " what you know about the roadmap.",
        effort="Varies per item — some are one click, some are a migration.",
        steps=[
            "Sort by savings and read the top five; the tail is rarely worth the meeting.",
            "Discard anything touching a workload with a known end date.",
            "For reservation advice, check the term against how long you expect to run it.",
        ],
        action="Open each in the portal — Advisor links to the specific resource and the "
               "specific change.",
        tab="advisor",
        caveat=f"Advisor's figures are annual. Every other finding here covers the last "
               f"{days} days, so the two are not directly comparable — this one is ordered "
               f"by its equivalent over the same window.",
    )]


def _from_tags(d: dict[str, Any], cur: str) -> list[Finding]:
    """Spend that cannot be attributed to anyone.

    Not a saving, and says so. Untagged spend is a governance finding: it is the reason the
    other findings cannot be routed to an owner.
    """
    total = _money(d.get("total"))
    # The tab reports these as objects, not scalars: `untagged.cost` and `untagged.resources`.
    # Reading `d["untagged"]` as a number would coerce to zero and quietly report perfect tag
    # coverage on an estate that has none.
    untagged = _money((d.get("untagged") or {}).get("cost"))
    if not total or untagged < MIN_IMPACT:
        return []
    share = untagged / total * 100
    if share < 15:
        return []
    resources = int((d.get("untagged") or {}).get("resources") or 0)
    return [Finding(
        kind="governance",
        title=f"{share:.0f}% of spend carries no tag",
        detail=f"{untagged:,.2f} of {total:,.2f} across {resources} resource"
               f"{'s' if resources != 1 else ''} cannot be attributed to a team, "
               f"application or environment.",
        impact=0.0,   # deliberately not an impact: this saves nothing by itself
        currency=cur,
        severity="medium",
        confidence="high",
        basis="Counted directly from the cost export: every row either carries tags or does "
              "not. This is not a saving and is shown with no monetary impact — untagged spend "
              "costs exactly the same as tagged spend. It ranks here because it is what stops "
              "every other finding on this page from reaching an owner.",
        source="Cost by tag",
        evidence=[{"total": total, "untagged": untagged, "resources": resources,
                   "share_pct": round(share, 1)}],
        because="Tagging decays unless it is enforced at creation. Anything built by hand, by an"
                " older pipeline, or during an incident arrives untagged, and there is never a"
                " good week to go back and label six months of it.",
        risk="None from acting — but understand what fixing it does and does not buy. Tags do"
             " not reduce the bill by a penny. They decide whether the next conversation about"
             " this estate is 'who owns this?' or 'here is your number'.",
        effort="Low per resource, high in aggregate — which is why it is a policy job rather"
               " than a tagging job.",
        steps=[
            "Tag the twenty most expensive untagged resources; on most estates that is the"
            " majority of the money.",
            "Add an Azure Policy that denies creation without an owner tag — deny, not audit,"
            " or you are back here in six months.",
            "Leave the long tail. It is not worth the week it would take.",
        ],
        action="Tag the largest untagged resources first; a policy that enforces tags at "
               "creation stops the problem returning.",
        tab="tags",
    )]


def _from_commitments(d: dict[str, Any], cur: str) -> list[Finding]:
    """Steady compute paying on-demand rates.

    Careful here: this module will not quote a reservation discount, because the real number
    depends on term, region, family and payment option, and a made-up percentage is exactly the
    kind of confident nonsense the whole file exists to prevent.
    """
    compute = _money(d.get("compute_total"))
    covered = _money(d.get("compute_coverage_pct"))
    if compute < 50 or covered >= 20:
        return []
    return [Finding(
        kind="commitment",
        title="Compute is running almost entirely on-demand",
        detail=f"{compute:,.2f} of compute spend with {covered:.0f}% on a commitment.",
        impact=0.0,   # no impact claimed: see basis
        currency=cur,
        severity="low",
        confidence="low",
        basis="The split between on-demand, reserved and Spot is counted from the cost "
              "export's own pricing model field. No saving is claimed, and that is deliberate: "
              "what a reservation would save depends on term, region, instance family and "
              "payment option, and quoting a percentage without those is guesswork. Check the "
              "Rate optimization tab, where Azure prices it against your actual usage.",
        source="Commitments",
        evidence=[{"compute_total": compute, "coverage_pct": covered}],
        because="Commitment coverage is usually low for a good reason early on — nobody wants to"
                " lock in three years of a platform they are still shaping — and then stays low"
                " out of habit long after the shape has settled.",
        risk="This is the one item on the list where acting can cost you money. A reservation is"
             " a commitment: if the workload moves region, changes family, or is decommissioned,"
             " you keep paying. Do not buy against a number on a dashboard — buy against a"
             " workload you can name and a plan you believe.",
        effort="Low to buy, and effectively irreversible, which is the wrong way round. Treat"
               " it as a financial decision rather than a technical one.",
        steps=[
            "Identify which compute has been running unchanged for ninety days or more.",
            "Check the Rate optimization tab, where Azure prices a commitment against your"
            " actual usage rather than against a percentage someone guessed.",
            "Start with a one-year term. The three-year discount is larger and the estate you"
            " are betting on is three years less predictable.",
        ],
        action="If this compute is steady rather than bursty, price a reservation against it.",
        tab="rates",
        caveat="Only worth acting on for workloads that will still be running in a year.",
    )]


# ----------------------------------------------------------------- the analysis

def _summary(ranked: list[Finding], addressable: float, cur: str,
             days: int, failed: dict[str, str]) -> dict[str, Any]:
    """The paragraph a consultant opens with.

    Assembled from the findings rather than written in advance, so it can only say things the
    data supports — but shaped the way a person would say them: the situation, then what is
    driving it, then what to do first and why that one.
    """
    if not ranked:
        return {
            "headline": "Nothing worth flagging",
            "situation": f"Every source was read across the last {days} days and none of them "
                         f"found something above the reporting threshold.",
            "priority": "",
            "sequence": [],
        }

    urgent = [f for f in ranked if f.severity == "critical"]
    savings = [f for f in ranked if f.impact > 0 and f.kind not in ("budget", "advisor")]
    quick = [f for f in savings if f.effort.lower().startswith("low")]
    first = urgent[0] if urgent else (quick[0] if quick else ranked[0])

    if urgent:
        headline = f"{len(urgent)} thing{'s' if len(urgent) != 1 else ''} needs attention now"
    elif addressable >= 100:
        headline = f"{cur} {addressable:,.0f} of addressable waste"
    else:
        headline = "A clean estate, with room to tidy"

    bits = []
    if urgent:
        bits.append(f"{len(urgent)} finding{'s' if len(urgent) != 1 else ''} "
                    f"{'are' if len(urgent) != 1 else 'is'} still running and unresolved")
    if savings:
        bits.append(f"{len(savings)} recoverable {cur} {addressable:,.0f} over {days} days")
    if failed:
        bits.append(f"{len(failed)} source{'s' if len(failed) != 1 else ''} could not be read")

    situation = (
        f"Across {len(ranked)} findings: " + "; ".join(bits) + "."
        if bits else f"{len(ranked)} findings, none of them urgent."
    )

    # Why that one first. Sequencing is the part people actually want from a senior opinion —
    # a ranked list is a spreadsheet, an order of work is advice.
    if urgent:
        priority = (f"Start with “{first.title}”. It is not a saving — it is a commitment "
                    f"already broken, and everything below it is easier to justify once the "
                    f"number it breached is either fixed or corrected.")
    elif quick:
        priority = (f"Start with “{first.title}”. {first.effort.split('—')[0].strip()} effort "
                    f"for {cur} {first.impact:,.0f}, and it carries the least risk of anything "
                    f"on this list — the ratio will not be this good further down.")
    else:
        priority = (f"Start with “{first.title}” — the largest single item, and the one whose "
                    f"evidence is strongest.")

    # A short order of work. Three is deliberate: a longer list stops being a sequence and
    # becomes a backlog, which is what the ranked findings already are.
    #
    # Deduplicated by kind, because the ranking legitimately puts three anomalies at the top and
    # "investigate a spike, investigate another spike, investigate a third spike" is not a plan.
    # One of each kind gives a first move, a different second move, and a third.
    order: list[Finding] = []
    seen_kinds: set[str] = set()
    for f in [*urgent, *quick, *ranked]:
        if f.kind in seen_kinds:
            continue
        seen_kinds.add(f.kind)
        order.append(f)
        if len(order) == 3:
            break

    return {
        "headline": headline,
        "situation": situation,
        "priority": priority,
        "sequence": [
            # Just the effort verdict, not the explanation — the full sentence belongs on the
            # finding, and repeated three times in a list it becomes noise rather than guidance.
            {"title": f.title, "why": (f.effort.split("—")[0].strip() or f.severity) + " effort",
             "impact": f.impact, "kind": f.kind}
            for f in order
        ],
    }


def _rank(findings: list[Finding]) -> list[Finding]:
    """Impact first, then severity, then confidence.

    Money leads because this is a cost tool and the reader's time is finite. But severity breaks
    ties above it in one case that matters — a breached budget and an unexplained spike are both
    *running*, and something running outranks something merely sitting there costing the same
    amount it cost last month.
    """
    urgent = {"budget", "anomaly"}
    conf = {"high": 0, "medium": 1, "low": 2}
    return sorted(
        findings,
        key=lambda f: (
            0 if f.kind in urgent else 1,
            -f.rank_impact,
            SEVERITY_RANK.get(f.severity, 9),
            conf.get(f.confidence, 9),
        ),
    )


async def analyse(subscriptions: list[str] | None = None, days: int = 30,
                  currency: str | None = None) -> dict[str, Any]:
    """Read every source, cross-reference, and rank what is worth doing.

    Returns findings, a headline, and — for every claim — where the number came from.
    """
    subs = subscriptions or None
    data, failed, timing = await _gather(subs, days, currency)

    cur = (currency or "").upper() or None
    if not cur:
        for key in ("tags", "commitments", "waste"):
            got = data.get(key) or {}
            if isinstance(got, dict) and got.get("currency"):
                cur = got["currency"]
                break
    cur = cur or "USD"

    findings: list[Finding] = []
    builders = (
        ("waste", _from_waste), ("rightsizing", _from_rightsizing),
        ("shutdown", _from_shutdown), ("anomalies", _from_anomalies),
        ("budgets", _from_budgets),
        ("tags", _from_tags), ("commitments", _from_commitments),
    )
    for key, build in builders:
        got = data.get(key)
        if not isinstance(got, dict):
            continue
        try:
            findings.extend(build(got, cur))
        except Exception as exc:  # noqa: BLE001 - one bad source must not lose the rest
            log.warning("insights: %s builder failed: %s", key, str(exc)[:200])
            failed[key] = f"could not be interpreted: {str(exc)[:120]}"

    # Advisor needs the window to scale its annual figures onto the same axis as everything else.
    advisor = data.get("advisor")
    if isinstance(advisor, dict):
        try:
            findings.extend(_from_advisor(advisor, cur, days))
        except Exception as exc:  # noqa: BLE001
            log.warning("insights: advisor builder failed: %s", str(exc)[:200])
            failed["advisor"] = f"could not be interpreted: {str(exc)[:120]}"

    ranked = _rank(findings)[:TOP_N]

    # Only findings that actually claim a saving are added up. Governance and commitment findings
    # deliberately carry no impact, and rolling a zero into the headline would be honest but
    # rolling *Advisor's annualised* figure in would not — different period, different basis.
    addressable = sum(f.impact for f in ranked
                      if f.impact > 0 and f.kind not in ("budget", "advisor", "anomaly"))

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": days,
        "currency": cur,
        "subscriptions": len(subs) if subs else None,
        "summary": _summary(ranked, addressable, cur, days, failed),
        "findings": [f.as_dict() for f in ranked],
        "count": len(ranked),
        "addressable": round(addressable, 2),
        # Named so the panel can say "Advisor could not be read" rather than quietly showing
        # a shorter list and letting the reader assume there was nothing to find.
        "unavailable": failed,
        # How long each source took. Shown nowhere by default — it is here so "the analysis is
        # slow" can be answered with a number rather than a theory.
        "timing_ms": timing,
        "method": (
            "Every figure above is carried from the tab named beside it — this analysis reads "
            "the same sources you can open yourself and does not recompute, estimate or model "
            "any of them. Savings are stated as what stopping a resource would avoid at the "
            "rate it is currently billed, which is a ceiling rather than a forecast. Findings "
            "that cannot honestly claim a saving carry none."
        ),
    }

"""One savings list, with the duplicates collapsed.

Six tabs used to answer the same question — Orphaned resources, Rightsizing, Shutdown savings,
Rate optimization, Advisor and Commitments — and they overlapped in three separate ways:

  * Advisor repeats itself. Sixteen cost recommendations on this estate are five distinct
    problems; the rest are the same problem priced at a different SKU or term. Two groups of six
    identical rows. Shown as a list they read as sixteen things to do.

  * The scans overlap each other. `demovm1` is deallocated, so the orphan scan bills it for its
    disks; it also has no CPU, so the rightsizing scan lists it; Advisor flags it too. Three
    tabs, three rows, one virtual machine.

  * Rate optimization *is* Advisor. It reads the same Cost recommendations and splits them into
    commitments and usage, so every rate suggestion already appeared under Advisor.

The costly part is not the clutter, it is the arithmetic. Each of those tabs shows a total, and
the totals share resources — $15.82 of deallocated VM spend is inside both the orphan total and
the rightsizing total. Anyone who adds the tabs up gets a saving they cannot bank.

So the rule here is: **two claims about the same resource are one opportunity, and the money is
counted once.** Where two sources disagree about how much, we keep the larger and record both,
because a disagreement is worth seeing rather than averaging away.

What is deliberately *not* merged: the reason each scan gave. A VM that three independent checks
flag is a stronger candidate than one caught by a single heuristic, and that signal only exists
if we keep the provenance. It is the one useful thing the duplication was telling us.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("cost-agent.savings")

# How a category is presented: the chip label, and the order it sorts in when impact ties.
# Ordered by how directly you can act on it — deleting an orphan is a decision you can take this
# afternoon, buying a three-year reservation is not.
CATEGORIES: dict[str, dict[str, str]] = {
    "orphaned":   {"label": "Orphaned",    "hint": "Billed, attached to nothing"},
    "idle":       {"label": "Idle",        "hint": "Running, barely used"},
    "rightsize":  {"label": "Rightsize",   "hint": "Bigger than the load needs"},
    "schedule":   {"label": "Schedule",    "hint": "Idle on a predictable rhythm"},
    "rate":       {"label": "Rate",        "hint": "Same usage, cheaper price"},
    "commitment": {"label": "Commitment",  "hint": "Reserve capacity you already use"},
}
CATEGORY_ORDER = list(CATEGORIES)

# Which scan said so. `weight` is how much the source's own cost figure is trusted when two
# sources disagree: a warehouse figure is a bill we have, an Advisor estimate is a projection.
SOURCES: dict[str, dict[str, Any]] = {
    "waste":       {"label": "Orphan scan",  "basis": "billed", "weight": 3},
    "rightsizing": {"label": "Utilisation",  "basis": "billed", "weight": 3},
    "shutdown":    {"label": "Schedule scan", "basis": "billed", "weight": 2},
    "advisor":     {"label": "Azure Advisor", "basis": "estimate", "weight": 1},
    "rates":       {"label": "Rate analysis", "basis": "estimate", "weight": 1},
}


def _num(v: Any) -> float:
    """Money, or zero. Azure sends nulls, empty strings and occasionally strings with commas."""
    if v is None or v is True or v is False:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _norm_id(v: Any) -> str:
    """An ARM id reduced to something two sources can agree on.

    Azure is inconsistent about the case of resource-group segments — the same disk arrives as
    `.../resourceGroups/CostAnomaly/...` from one API and `.../resourcegroups/costanomaly/...`
    from another — so a case-sensitive match silently fails to dedupe. Trailing slashes likewise.
    """
    s = str(v or "").strip().rstrip("/").lower()
    return s


def _res_key(name: Any, group: Any, sub: Any = "") -> str:
    """The fallback key, for sources that report a name but no id.

    The rightsizing scan returns `name` and `resource_group` and no ARM id at all, so a VM it
    flags can only be matched to the orphan scan's entry by that pair. Scoped by subscription
    where we know it, because resource-group names repeat across subscriptions and matching
    `tub/TUB-VM` in one against `tub/TUB-VM` in another would merge two different machines.

    Returns empty when there is nothing to key on. Joining three blanks yields "||", which is
    perfectly truthy and would have every source with no resource fields matching every other.
    """
    parts = [str(x or "").strip().lower() for x in (sub, group, name)]
    return "|".join(parts) if any(parts) else ""


def _sub_of(rid: str) -> str:
    m = re.search(r"/subscriptions/([^/]+)", rid or "", re.I)
    return (m.group(1) if m else "").lower()


class Opportunity:
    """One thing worth doing, however many scans noticed it."""

    __slots__ = ("key", "category", "title", "why", "resource", "resource_id",
                 "resource_group", "subscription", "region", "kind", "detail",
                 "window", "annual", "currency", "sources", "options", "count",
                 "items", "disputed", "action", "folded")

    def __init__(self, key: str, category: str, title: str, *,
                 why: str = "", resource: str = "", resource_id: str = "",
                 resource_group: str = "", subscription: str = "", region: str = "",
                 kind: str = "", detail: str = "", window: float = 0.0,
                 annual: float = 0.0, currency: str = "USD", action: str = "",
                 count: int = 1, items: list[dict[str, Any]] | None = None) -> None:
        self.key = key
        self.category = category
        self.title = title
        self.why = why
        self.resource = resource
        self.resource_id = resource_id
        self.resource_group = resource_group
        self.subscription = subscription
        self.region = region
        self.kind = kind
        self.detail = detail
        self.window = round(_num(window), 2)
        self.annual = round(_num(annual), 2)
        self.currency = currency or "USD"
        self.action = action
        self.count = count
        self.items = items or []
        self.sources: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.disputed: dict[str, Any] | None = None
        # How many rows the source collapsed into this one that were re-estimates rather than
        # choices. Reported so the count is auditable rather than something the reader has to
        # take on trust after the list got shorter.
        self.folded = 0

    # ------------------------------------------------------------------ merging

    def add_source(self, name: str, claim: str, window: float = 0.0,
                   annual: float = 0.0) -> None:
        """Record that a scan found this, and reconcile its money with what we already had.

        Across sources the money is *not* added. Two scans looking at the same deallocated VM are
        describing one bill, and summing them is how the old tab totals came to overlap. We keep
        the larger figure — the smaller usually reflects a narrower view of the same resource,
        such as the orphan scan pricing only the disks while utilisation prices the whole machine
        — and when they differ by enough to matter we say so rather than quietly picking one.

        Within one source it is added, because repeat calls there are different resources: the
        utilisation scan attaches once per VM to a row covering three of them. Recording only the
        first left a row totalling 20.99 quoting a source that appeared to have found 5.92, which
        reads as a contradiction rather than as one VM out of three.
        """
        meta = SOURCES.get(name, {"label": name, "basis": "estimate", "weight": 1})
        prior = next((s for s in self.sources if s["source"] == name), None)
        if prior is None:
            self.sources.append({
                "source": name, "label": meta["label"], "basis": meta["basis"],
                "claim": claim, "matches": 1,
                "window": round(_num(window), 2), "annual": round(_num(annual), 2),
            })
        else:
            prior["matches"] += 1
            prior["window"] = round(prior["window"] + _num(window), 2)
            prior["annual"] = round(prior["annual"] + _num(annual), 2)
            prior["claim"] = (f"{prior['matches']} of these, "
                              f"{prior['window']:,.2f} over the window")

        w = _num(window) if prior is None else prior["window"]
        a = _num(annual) if prior is None else prior["annual"]
        if w > self.window:
            before = self.window
            self.window = round(w, 2)
            # Worth surfacing only when the gap is both relatively and absolutely material;
            # a few cents between two roundings is not a disagreement anyone needs to see.
            if before > 0 and (w - before) / max(w, 0.01) > 0.25 and (w - before) >= 1:
                self.disputed = {"low": before, "high": round(w, 2)}
        if a > self.annual:
            self.annual = round(a, 2)

    def add_option(self, label: str, window: float, annual: float,
                   detail: str = "") -> None:
        """Another way of buying the same saving — a different SKU, term or quantity.

        Advisor emits one recommendation per purchasable configuration, which is why six rows can
        describe one decision. They are alternatives, not additions: you buy one. Recorded so the
        choice is still visible, but only the best one counts toward the total.
        """
        if any(o["label"] == label for o in self.options):
            return
        self.options.append({
            "label": label, "detail": detail,
            "window": round(_num(window), 2), "annual": round(_num(annual), 2),
        })

    @property
    def corroborated(self) -> bool:
        """Flagged by more than one independent scan."""
        return len(self.sources) > 1

    @property
    def confidence(self) -> str:
        """How much to trust the figure, from where it came rather than how it feels.

        A number taken from the bill is not the same kind of claim as a projection from Advisor,
        and two independent scans agreeing is stronger than either alone.
        """
        billed = any(s["basis"] == "billed" for s in self.sources)
        if billed and self.corroborated:
            return "high"
        if billed:
            return "medium"
        return "low" if len(self.sources) < 2 else "medium"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "category_label": CATEGORIES.get(self.category, {}).get("label", self.category),
            "title": self.title,
            "why": self.why,
            "resource": self.resource,
            "resource_id": self.resource_id,
            "resource_group": self.resource_group,
            "subscription": self.subscription,
            "region": self.region,
            "kind": self.kind,
            "detail": self.detail,
            "window": self.window,
            "annual": self.annual,
            "currency": self.currency,
            "action": self.action,
            "count": self.count,
            "items": self.items[:12],
            "more": max(0, len(self.items) - 12),
            "sources": self.sources,
            "options": sorted(self.options, key=lambda o: -o["annual"])[:6],
            "corroborated": self.corroborated,
            "confidence": self.confidence,
            "disputed": self.disputed,
            "folded": self.folded,
        }


# ------------------------------------------------------------------- collection

# Waste categories carry their own shape; this maps them onto the small set of categories the
# UI filters by, so a chip means the same thing whichever scan produced the row.
_WASTE_CATEGORY = {
    "unattached_disks": "orphaned",
    "unassociated_public_ips": "orphaned",
    "empty_app_service_plans": "orphaned",
    "deallocated_vms": "orphaned",
    "orphaned_nics": "orphaned",
    "orphaned_snapshots": "orphaned",
    "empty_load_balancers": "orphaned",
    "idle_gateways": "idle",
    "stale_restore_points": "orphaned",
}


class _Index:
    """Resource id (or name+group) to the opportunity that already covers it.

    The whole point of the merge: before a scan creates a row, it asks here whether some other
    scan already reported this resource. Every id an opportunity covers is registered, including
    each item inside a grouped one, so that a per-VM finding from the utilisation scan can attach
    itself to the orphan scan's "3 deallocated VMs" row instead of becoming a fourth row.

    Two key shapes are kept for every resource, because the scans do not agree on what they know.
    The orphan scan sends a full ARM id, from which a subscription can be read; the utilisation
    scan sends a bare name and resource group and no subscription at all. Registering only the
    subscription-qualified key meant the two never met, and the deallocated VMs were counted
    twice — which is the exact arithmetic this module exists to prevent.

    The unqualified key is riskier, since resource-group names repeat across subscriptions, so
    any unqualified key claimed by two different opportunities is marked ambiguous and stops
    matching altogether. A missed merge shows one resource twice; a wrong merge silently folds
    two real resources into one and loses money from the total. The first is recoverable by
    looking, the second is not.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Opportunity] = {}
        self._ambiguous: set[str] = set()

    def register(self, opp: Opportunity, *keys: str, weak: bool = False) -> None:
        for k in keys:
            if not k:
                continue
            prior = self._by_key.get(k)
            if prior is None:
                self._by_key[k] = opp
            elif prior is not opp and weak:
                self._ambiguous.add(k)

    def find(self, *keys: str) -> Opportunity | None:
        for k in keys:
            if k and k not in self._ambiguous and k in self._by_key:
                return self._by_key[k]
        return None


def _from_waste(data: dict[str, Any], index: _Index, cur: str) -> list[Opportunity]:
    """The orphan scan. Grouped by kind, because fifteen unassociated IPs are one decision.

    Each item's id is still registered individually, so another scan that names one of those
    resources joins this row rather than opening its own.
    """
    out: list[Opportunity] = []
    for f in data.get("findings") or []:
        items = f.get("items") or []
        cat = _WASTE_CATEGORY.get(f.get("category") or "", "orphaned")
        cost = _num(f.get("cost"))
        opp = Opportunity(
            key=f"waste:{f.get('category')}",
            category=cat,
            title=f.get("title") or "Idle resources",
            why=f.get("why") or "",
            window=cost,
            currency=f.get("currency") or cur,
            count=int(f.get("count") or len(items) or 1),
            items=[{
                "name": i.get("name"), "group": i.get("resource_group"),
                "region": i.get("location"), "detail": i.get("detail"),
                "cost": _num(i.get("cost")), "id": i.get("id"),
            } for i in items],
            action="Delete it, or attach it to something that uses it.",
        )
        opp.add_source("waste", f"{opp.count} found, billed {cost:,.2f} over the window",
                       window=cost)
        strong = [_norm_id(i.get("id")) for i in items]
        strong += [_res_key(i.get("name"), i.get("resource_group"),
                            _sub_of(str(i.get("id") or ""))) for i in items]
        index.register(opp, opp.key, *strong)
        # The same resources keyed without their subscription, for scans that do not send one.
        index.register(opp, *[_res_key(i.get("name"), i.get("resource_group"), "")
                              for i in items], weak=True)
        out.append(opp)
    return out


def _from_rightsizing(data: dict[str, Any], index: _Index, cur: str) -> list[Opportunity]:
    """The utilisation scan. Per VM, and usually the second scan to reach a given machine.

    A deallocated VM is already in the orphan scan's row; all this adds is a second opinion on
    the same bill. A *running* VM with no load is new information and gets its own row.
    """
    out: list[Opportunity] = []
    for vm in data.get("vms") or []:
        name, group = vm.get("name"), vm.get("resource_group")
        cost = _num(vm.get("cost"))
        state = (vm.get("state") or "").lower()
        cpu = vm.get("cpu_avg")
        idle = cpu is not None and _num(cpu) < _num(data.get("cpu_threshold") or 5)
        stopped = "deallocat" in state

        claim = (f"{state or 'unknown'}, "
                 + (f"CPU {_num(cpu):.1f}% average" if cpu is not None else "no CPU data")
                 + f", {cost:,.2f} over the window")

        existing = index.find(_norm_id(vm.get("id")), _res_key(name, group, vm.get("subscriptionId")),
                              _res_key(name, group, ""))
        if existing is not None:
            existing.add_source("rightsizing", claim, window=cost)
            continue

        if not (idle or stopped):
            continue          # a busy VM is not an opportunity
        if cost <= 0:
            # Nothing is being spent on it, so there is nothing to recover. A row here would be
            # an instruction to go and save zero pounds, which is worse than silence.
            continue

        opp = Opportunity(
            key=f"vm:{_res_key(name, group, vm.get('subscriptionId'))}",
            category="idle" if not stopped else "orphaned",
            title=f"{name} is {'stopped but still billed' if stopped else 'running near idle'}",
            why=("Deallocated machines cost nothing to run, but their disks are still billed."
                 if stopped else
                 "The machine is on and charged in full while doing almost no work."),
            resource=name or "", resource_group=group or "",
            region=vm.get("location") or "", kind=vm.get("size") or "",
            detail=f"{vm.get('size') or ''} · {state}".strip(" ·"),
            window=cost, currency=vm.get("currency") or cur,
            action="Resize it to the load it actually carries, or shut it down.",
        )
        opp.add_source("rightsizing", claim, window=cost)
        index.register(opp, opp.key, _norm_id(vm.get("id")),
                       _res_key(name, group, vm.get("subscriptionId")))
        out.append(opp)
    return out


def _from_shutdown(data: dict[str, Any], index: _Index, cur: str) -> list[Opportunity]:
    """Machines idle on a rhythm. Almost always a VM some other scan has already named."""
    out: list[Opportunity] = []
    for c in data.get("candidates") or []:
        name, group = c.get("name"), c.get("resource_group")
        saving = _num(c.get("saving") or c.get("monthly_saving"))
        claim = c.get("pattern") or c.get("detail") or "idle on a predictable schedule"

        existing = index.find(_norm_id(c.get("id")), _res_key(name, group, c.get("subscriptionId")),
                              _res_key(name, group, ""))
        if existing is not None:
            existing.add_source("shutdown", f"{claim} — about {saving:,.2f} recoverable",
                                window=saving)
            continue

        opp = Opportunity(
            key=f"sched:{_res_key(name, group, c.get('subscriptionId'))}",
            category="schedule",
            title=f"{name} is idle on a predictable schedule",
            why="It is quiet at the same hours every day, so those hours need not be paid for.",
            resource=name or "", resource_group=group or "",
            region=c.get("location") or "", window=saving,
            currency=c.get("currency") or cur,
            action="Put it on a start/stop schedule covering the quiet hours.",
        )
        opp.add_source("shutdown", claim, window=saving)
        index.register(opp, opp.key, _norm_id(c.get("id")))
        out.append(opp)
    return out


def _advisor_category(problem: str) -> str:
    """Advisor states the problem in prose; this is the only signal for what kind it is."""
    p = (problem or "").lower()
    if "reserv" in p or "savings plan" in p or "reserved instance" in p:
        return "commitment"
    if "right-size" in p or "rightsize" in p or "underutilized" in p or "under-utilized" in p:
        return "rightsize"
    if "not attached" in p or "unattached" in p or "idle" in p or "unused" in p:
        return "orphaned"
    return "rate"


def _from_advisor(rows: list[dict[str, Any]], index: _Index, cur: str,
                  days: int, source: str) -> list[Opportunity]:
    """Azure Advisor, with its two different kinds of repetition told apart.

    Advisor repeats itself twice over, and the two repetitions mean opposite things:

      * **Terms are alternatives.** A one-year and a three-year reservation on the same plan are
        two ways to buy the same saving. You take one, so only the best counts — summing them
        would treble the estate's headline.

      * **Lookback windows are re-estimates.** The same term priced over 7, 30 and 60 days of
        history is one recommendation Advisor has costed three ways. Those are not choices at
        all, and showing them as rows is what made sixteen recommendations out of five.

    So rows collapse to one per *decision* — problem, resource, SKU, term, quantity — and the
    lookback spread becomes the confidence range on that decision's figure. The longest lookback
    supplies the headline number, deliberately not the largest: on this estate the seven-day
    window is the most optimistic of the three, so taking the maximum would have quietly
    published the least-evidenced estimate every time.
    """
    def decision_key(r: dict[str, Any]) -> str:
        problem = (r.get("problem") or r.get("solution") or "Cost recommendation").strip()
        target = (r.get("resource") or r.get("name") or r.get("subscriptionId") or "").strip()
        return "||".join(str(x or "").lower() for x in (
            problem, target, r.get("sku"), r.get("term"), r.get("quantity")))

    # First fold the re-estimates, keeping the spread.
    decisions: dict[str, dict[str, Any]] = {}
    for r in rows:
        k = decision_key(r)
        annual = _num(r.get("annual_savings")) or _num(r.get("savings")) * 12
        d = decisions.get(k)
        if d is None:
            decisions[k] = {"row": r, "low": annual, "high": annual, "estimates": 1,
                            "lookback": _num(r.get("lookback_days"))}
            continue
        d["estimates"] += 1
        d["low"] = min(d["low"], annual)
        d["high"] = max(d["high"], annual)
        if _num(r.get("lookback_days")) > d["lookback"]:
            d["row"], d["lookback"] = r, _num(r.get("lookback_days"))

    # Then group the remaining alternatives by the problem they solve.
    groups: dict[str, list[dict[str, Any]]] = {}
    for d in decisions.values():
        r = d["row"]
        problem = (r.get("problem") or r.get("solution") or "Cost recommendation").strip()
        target = (r.get("resource") or r.get("name") or r.get("subscriptionId") or "").strip()
        groups.setdefault(f"{problem.lower()}||{target.lower()}", []).append(d)

    out: list[Opportunity] = []
    for gkey, ds in groups.items():
        best_d = max(ds, key=lambda d: _num(d["row"].get("annual_savings")))
        first = best_d["row"]
        problem = (first.get("problem") or first.get("solution") or "Cost recommendation").strip()
        target = (first.get("resource") or first.get("name")
                  or first.get("subscriptionId") or "").strip()
        cat = _advisor_category(problem)

        annual = _num(first.get("annual_savings")) or _num(first.get("savings")) * 12
        window = annual * (max(days, 1) / 365.0)
        folded = sum(d["estimates"] - 1 for d in ds)

        claim = f"{problem} — about {annual:,.0f} a year"
        if len(ds) > 1:
            claim += f", best of {len(ds)} purchasable options"
        if best_d["lookback"]:
            claim += f", from {int(best_d['lookback'])} days of usage"

        # Advisor names the *subscription* as the resource for anything bought at subscription
        # scope, so a reservation and a savings plan in the same subscription arrive with an
        # identical resource field. Matching on it merged two unrelated recommendations into one
        # row and added their money together — the opposite of what this module is for. A
        # subscription is a place, not a thing to act on, so it is not an identity here.
        looks_like_sub = bool(re.fullmatch(r"[0-9a-f-]{36}", target, re.I))
        match_key = "" if looks_like_sub else _norm_id(target)

        existing = index.find(match_key, _norm_id(first.get("id")),
                              _res_key(first.get("name"), first.get("resourceGroup"),
                                       first.get("subscriptionId")))
        target_opp = existing
        if target_opp is None:
            target_opp = Opportunity(
                key=f"advisor:{gkey}",
                category=cat,
                title=problem,
                why=("Azure Advisor compares your recent usage against the price of committing "
                     "to it in advance." if cat == "commitment" else
                     "Azure Advisor flagged this from your recent usage."),
                resource="" if looks_like_sub else target,
                resource_id="" if looks_like_sub else target,
                resource_group=first.get("resourceGroup") or "",
                subscription=first.get("subscriptionId") or (target if looks_like_sub else ""),
                region=first.get("region") or "",
                kind=first.get("sku") or "",
                detail=" · ".join(x for x in (first.get("sku"), first.get("region"),
                                              first.get("scope")) if x),
                window=window, annual=annual,
                currency=first.get("currency") or cur,
                action=("Choose the term that matches how long you expect to keep running this."
                        if cat == "commitment" else "Apply the change Advisor describes."),
            )
            index.register(target_opp, target_opp.key, match_key)
            out.append(target_opp)

        target_opp.add_source(source, claim, window=window, annual=annual)
        target_opp.folded += folded
        if len(ds) > 1:
            for d in ds:
                a = _num(d["row"].get("annual_savings"))
                target_opp.add_option(
                    _option_label(d["row"]), a * (max(days, 1) / 365.0), a,
                    detail=_spread_note(d))
    return out


def _spread_note(d: dict[str, Any]) -> str:
    """How firm one option's figure is, said in terms of what produced it."""
    if d["estimates"] < 2:
        return f"from {int(d['lookback'])} days of usage" if d["lookback"] else ""
    if abs(d["high"] - d["low"]) < 0.5:
        return f"same figure across {d['estimates']} lookback windows"
    return (f"{d['low']:,.0f}–{d['high']:,.0f} depending on the lookback window; "
            f"shown from the longest")


def _option_label(r: dict[str, Any]) -> str:
    """Name a purchasable variant by what distinguishes it from its siblings.

    Term first, because that is the decision: a three-year commitment saves more and binds you
    for longer, and everything else about the two rows is usually identical.
    """
    term = str(r.get("term") or "").strip()
    pretty = {"P1Y": "1 year", "P3Y": "3 years", "P5Y": "5 years"}.get(term.upper(), term)
    bits = [pretty, str(r.get("sku") or "").strip()]
    q = str(r.get("quantity") or "").strip()
    if q and q not in {"0", "None", "1"}:
        bits.append(f"×{q}")
    label = " · ".join(b for b in bits if b)
    return label or "as recommended"


# -------------------------------------------------------------------- assembly

def _rank(opps: list[Opportunity]) -> list[Opportunity]:
    """Biggest recoverable money first, with corroboration breaking ties.

    Ranking on the window figure rather than the annual one, because the annual figure is only
    available for Advisor rows and sorting a mixed list by it puts every projection above every
    resource that is being billed today.
    """
    return sorted(opps, key=lambda o: (
        -o.window,
        not o.corroborated,
        CATEGORY_ORDER.index(o.category) if o.category in CATEGORY_ORDER else 99,
        o.title.lower(),
    ))


async def build(subs: list[str] | None, days: int,
                currency: str | None = None) -> dict[str, Any]:
    """Every savings source, merged into one list.

    Sources run concurrently and a failure is named rather than hidden — an estate whose Advisor
    is throttled should still see its orphaned disks, and "we could not read Advisor" is itself
    worth showing next to a total that is therefore incomplete.
    """
    import asyncio
    import functools
    import time

    from . import shutdown as shutdown_mod
    from . import waste as waste_mod

    async def offload(fn: Any, *a: Any, **kw: Any) -> Any:
        return await asyncio.to_thread(functools.partial(fn, *a, **kw))

    jobs = {
        "waste": waste_mod.find_waste(subscription_ids=subs, days=days, top=60),
        "rightsizing": waste_mod.vm_utilisation(subscription_ids=subs, days=min(days, 90)),
        "advisor": waste_mod.advisor_recommendations(subscription_ids=subs, category="Cost"),
        "shutdown": shutdown_mod.find_schedulable(subs, days=min(days, 14)),
    }
    names = list(jobs)
    started = time.monotonic()
    results = await asyncio.gather(*jobs.values(), return_exceptions=True)

    data: dict[str, Any] = {}
    failed: dict[str, str] = {}
    for name, res in zip(names, results):
        if isinstance(res, BaseException):
            failed[name] = str(res)[:200]
            log.info("savings: %s unavailable: %s", name, str(res)[:200])
        else:
            data[name] = res

    cur = (currency or "").upper() or _currency_of(data) or "USD"
    index = _Index()
    opps: list[Opportunity] = []

    # Order matters. The orphan scan runs first because its figures come from the bill and its
    # groupings are the ones a reader recognises; later scans then attach to those rows rather
    # than the other way round, which keeps "3 deallocated VMs" as one line instead of three.
    opps += _from_waste(data.get("waste") or {}, index, cur)
    opps += _from_rightsizing(data.get("rightsizing") or {}, index, cur)
    opps += _from_shutdown(data.get("shutdown") or {}, index, cur)
    opps += _from_advisor(list((data.get("advisor") or {}).get("recommendations") or []),
                          index, cur, days, "advisor")

    ranked = _rank(opps)

    # The number that matters, and the one the six separate tabs could not state: money counted
    # once. Split by basis because a bill and a projection should not be added into one figure
    # and presented as if they were the same kind of certainty.
    billed = sum(o.window for o in ranked
                 if any(s["basis"] == "billed" for s in o.sources))
    projected = sum(o.window for o in ranked
                    if not any(s["basis"] == "billed" for s in o.sources))

    merged = sum(1 for o in ranked if o.corroborated)
    collapsed = sum(o.folded for o in ranked)

    took = int((time.monotonic() - started) * 1000)
    log.info("savings: %d opportunities from %d sources in %dms (%d corroborated, "
             "%d duplicate variants folded)", len(ranked), len(data), took, merged, collapsed)

    return {
        "opportunities": [o.as_dict() for o in ranked],
        "count": len(ranked),
        "billed_total": round(billed, 2),
        "projected_total": round(projected, 2),
        "total": round(billed + projected, 2),
        "currency": cur,
        "days": days,
        "categories": [
            {"id": cid, **meta,
             "count": sum(1 for o in ranked if o.category == cid),
             "total": round(sum(o.window for o in ranked if o.category == cid), 2)}
            for cid, meta in CATEGORIES.items()
        ],
        "merged": merged,
        "variants_folded": collapsed,
        "sources_read": sorted(data),
        "unavailable": failed,
        "took_ms": took,
        "note": ("Each resource is counted once, however many scans found it. Where two scans "
                 "disagreed on the amount, the larger is shown and the range is noted."),
    }


def _currency_of(data: dict[str, Any]) -> str:
    for key in ("waste", "advisor", "rightsizing"):
        c = (data.get(key) or {}).get("currency")
        if c:
            return str(c).upper()
    return ""



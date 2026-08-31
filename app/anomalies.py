"""Finding the days that did not look like the others.

Every other tab answers a question someone thought to ask. This one has to speak first: a cost
spike is only expensive because nobody noticed it for three weeks, and nobody notices by reading
a chart every morning. The dashboard shows *what* spend is; this shows *what changed*.

The method is deliberately boring. A rolling median and a median absolute deviation over the
preceding fortnight, and a day is anomalous when it sits far enough outside that band. Not a
mean and a standard deviation, because both are dragged around by the very outliers being
looked for -- one $900 spike inflates the mean and the deviation together, and the next spike
of the same size then reads as normal. The median does not move, so the second spike is still
a spike.

Three deliberate refusals:

  * **No anomaly without a baseline.** Fewer than `MIN_HISTORY` days of prior data and the day
    is skipped rather than guessed at. A service that appeared on Tuesday is not a Wednesday
    anomaly.

  * **Absolute floor as well as relative.** A meter going from $0.02 to $0.20 is a 900% rise and
    means nothing. Everything must also clear `MIN_DELTA`, or the page fills with noise and
    stops being read -- which is the only real failure mode for an alerting surface.

  * **The most recent days are excluded by default.** Azure restates cost data for several days
    after the fact, so the newest figures are still moving. Reporting them produces "anomalies"
    that quietly resolve themselves, and an alert that cries wolf twice gets ignored forever.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

log = logging.getLogger("cloudlens.anomalies")

# Days of history a dimension needs before any of its days can be judged.
MIN_HISTORY = 7

# How many days of the *same kind* (weekday vs weekend) are needed before the baseline is drawn
# from them alone. Four, because two weeks of history only contains four weekend days — demand
# seven and the weekend comparison never engages, which is the bug this number exists to fix.
MIN_KIND = 4

# The trailing window the baseline is computed over. Two weeks covers a weekly cycle -- weekend
# dips are normal and must not read as anomalies -- without reaching so far back that a genuine
# step change stays "anomalous" for a month.
WINDOW = 14

# How many MADs from the median a day must sit to count. 3.5 is the conventional threshold for
# the modified z-score; lower floods the page, higher misses real events.
THRESHOLD = 3.5

# Money floors. Relative change alone is meaningless at the bottom of the scale.
#
# $15 rather than something smaller because of what the first real run looked like: at $5, three
# quarters of the findings were sub-$15 rounding wobble on steady services, and a page where the
# real $97 spike sits ninth is a page nobody reads twice. An alerting surface has exactly one
# fatal failure mode, and it is being ignored.
MIN_DELTA = 15.0         # a day must move at least this much, in billing currency
MIN_BASELINE = 1.0       # and the thing must have been costing at least this much

# Cost data is restated for days after the fact, so the tail is unstable.
SETTLE_DAYS = 2

# How close two findings' deltas must be to count as the same underlying event seen through
# different dimensions. 0.7 accepts the $97 / $96 / $82 spread that one spike produced when
# viewed as a resource group, a subscription and a service.
SAME_EVENT = 0.7

# Beyond this the score stops meaning anything — it is just "far outside anything seen before".
SCORE_CAP = 50.0

# What a spike can be attributed to. Ordered by how actionable the answer is: a service name
# tells you what broke, a subscription only tells you where to start looking.
DIMENSIONS = {
    "ServiceName": "service",
    "ResourceGroup": "resource group",
    "SubAccountName": "subscription",
    "ResourceName": "resource",
    "RegionName": "region",
    "MeterName": "meter",
}


class AnomalyError(RuntimeError):
    """Something the caller can act on."""


def _is_weekend(day: str) -> bool:
    """Saturday or Sunday. Used to keep weekday and weekend baselines apart."""
    try:
        return date.fromisoformat(day).weekday() >= 5
    except ValueError:
        return False


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _mad(values: list[float], centre: float) -> float:
    """Median absolute deviation — a spread that outliers cannot inflate."""
    if not values:
        return 0.0
    return _median([abs(v - centre) for v in values])


def score(value: float, history: list[float],
          min_samples: int = MIN_HISTORY) -> tuple[float, float, float]:
    """How unusual `value` is against `history`. Returns (score, baseline, spread).

    The modified z-score: 0.6745 is the constant that puts MAD on the same scale as a standard
    deviation for normally distributed data, so the familiar "3 sigma" intuition still applies.

    A MAD of zero is the case worth handling carefully — it means every day in the window was
    identical, which is extremely common for a fixed monthly charge. Any change at all is then
    infinitely surprising by the formula, so the fallback compares against the median directly
    and lets the money floors decide whether it matters.

    `min_samples` is a parameter rather than a constant because the weekend baseline is drawn
    from four days where the weekday one has fourteen. Hard-coding the larger figure made the
    weekend comparison silently never engage.
    """
    if len(history) < min_samples:
        return 0.0, 0.0, 0.0
    centre = _median(history)
    spread = _mad(history, centre)
    if spread == 0:
        if centre == 0:
            return (0.0 if value == 0 else 10.0), centre, 0.0
        change = abs(value - centre) / centre
        return (10.0 if change > 0.5 else 0.0), centre, 0.0
    # Capped. A near-zero MAD — a service that billed the same fraction of a cent every day —
    # divides into a real change and produces "1,308,815× the usual variation", which is
    # arithmetically true and reads as a bug. Past about 50 the number has stopped carrying
    # information anyway: it is simply "far outside anything seen before".
    return max(-SCORE_CAP, min(SCORE_CAP, 0.6745 * (value - centre) / spread)), centre, spread


def _series(rows: list[tuple], dimension: str) -> dict[str, dict[str, float]]:
    """`{key: {day: cost}}` from flat rows, with missing days filled as zero.

    Filling matters, and it has to span the *whole* period rather than only the days that key
    appeared on. A resource that billed on Monday and Wednesday but not Tuesday really did cost
    nothing on Tuesday, and leaving the gap out makes the median look like the cost never
    stopped -- exactly backwards for spotting something that switched off.
    """
    by_key: dict[str, dict[str, float]] = {}
    days: set[str] = set()
    for key, day, cost in rows:
        k = key or "(none)"
        d = str(day)
        by_key.setdefault(k, {})[d] = float(cost or 0.0)
        days.add(d)

    # Every day between the first and the last, not just the ones that carried a row — a gap in
    # the source data is a real zero, and a key that vanished for a week must show that week.
    ordered = _spanning_days(days)
    for series in by_key.values():
        for d in ordered:
            series.setdefault(d, 0.0)
    return by_key


def _spanning_days(days: set[str]) -> list[str]:
    """Every calendar day from the first seen to the last, inclusive."""
    if not days:
        return []
    known = sorted(days)
    first, last = date.fromisoformat(known[0]), date.fromisoformat(known[-1])
    out, cursor = [], first
    while cursor <= last:
        out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def detect(rows: list[tuple], dimension: str, *,
           settle_days: int = SETTLE_DAYS,
           min_delta: float = MIN_DELTA,
           min_baseline: float = MIN_BASELINE) -> list[dict[str, Any]]:
    """Every anomalous day in the series, worst first.

    Pure arithmetic over rows already fetched -- no database, no clock -- so the whole detector
    is testable against a handmade series, which is the only way to be confident about a
    threshold.
    """
    series = _series(rows, dimension)
    if not series:
        return []

    all_days = sorted({d for s in series.values() for d in s})
    if len(all_days) <= MIN_HISTORY:
        return []

    # Drop the unsettled tail. Reporting a spike that Azure is still revising produces alerts
    # that resolve themselves, and those are worse than no alerts.
    judged = all_days[:-settle_days] if settle_days else all_days

    found: list[dict[str, Any]] = []
    for key, by_day in series.items():
        for i, day in enumerate(judged):
            if i < MIN_HISTORY:
                continue
            value = by_day.get(day, 0.0)

            # Compare like with like. Weekday spend and weekend spend are different populations
            # for most estates — dev boxes idle, pipelines stop — so judging Saturday against a
            # window containing five weekdays reports every single weekend as an anomaly. The
            # first run of this did exactly that: six findings from one five-week series whose
            # only feature was a weekend.
            #
            # The same-kind window reaches back further than WINDOW days, because a fortnight
            # holds only four weekend days. `min_samples` is lowered to match: demanding the
            # usual seven would mean the weekend baseline never has enough to engage, which is
            # precisely the bug this exists to fix.
            kind = _is_weekend(day)
            same_kind = [d for d in judged[:i] if _is_weekend(d) == kind][-WINDOW:]
            if len(same_kind) >= MIN_KIND:
                history, floor = [by_day[d] for d in same_kind], MIN_KIND
            else:
                history, floor = [by_day[d] for d in judged[max(0, i - WINDOW):i]], MIN_HISTORY

            z, baseline, spread = score(value, history, min_samples=floor)
            if abs(z) < THRESHOLD:
                continue

            delta = value - baseline
            # Both floors, not either: a big relative move on small money is noise, and so is a
            # big absolute move on something that was never predictable.
            if abs(delta) < min_delta or max(baseline, value) < min_baseline:
                continue

            found.append({
                "day": day,
                "dimension": DIMENSIONS.get(dimension, dimension),
                "key": key,
                "cost": round(value, 2),
                "baseline": round(baseline, 2),
                "delta": round(delta, 2),
                "percent": round((delta / baseline) * 100, 1) if baseline else None,
                "score": round(abs(z), 1),
                "direction": "up" if delta > 0 else "down",
                "severity": _severity(abs(delta), abs(z)),
            })

    found.sort(key=lambda a: (a["day"], abs(a["delta"])), reverse=True)
    return found


def _severity(delta: float, z: float) -> str:
    """How loudly to say it.

    Driven mostly by money rather than by the statistic. A 20-sigma deviation on $6 is still
    $6, and someone woken up for it stops trusting the page.
    """
    if delta >= 500 or (delta >= 100 and z >= 10):
        return "high"
    if delta >= 50:
        return "medium"
    return "low"


def _rows(con: Any, dimension: str, days: int) -> list[tuple]:
    """Daily cost per key for one dimension. The dimension name is from a fixed map, never input."""
    if dimension not in DIMENSIONS:
        raise AnomalyError(f"Cannot analyse by {dimension!r}.")
    since = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    return con.execute(
        f'SELECT coalesce(nullif("{dimension}", \'\'), \'(none)\') AS k, '
        '"ChargePeriodStart" AS d, sum("BilledCost") AS c '
        f"FROM costs WHERE \"ChargePeriodStart\" >= DATE '{since}' "
        'GROUP BY 1, 2 ORDER BY 2'
    ).fetchall()


def analyse(scope: list[str] | None = None, days: int = 60,
            dimensions: tuple[str, ...] = ("ServiceName", "ResourceGroup", "SubAccountName"),
            top: int = 40, display_currency: str | None = None) -> dict[str, Any]:
    """Everything unusual in the loaded data, across several dimensions at once.

    Several dimensions because the useful answer depends on the question. "Storage jumped" is
    the headline; "it was rg-backups" is where to look; "it was subscription X" is who to tell.
    Running all three costs one extra warehouse pass each and turns a number into an
    explanation.

    `display_currency` converts inside the warehouse connection, so the baseline, the deviation
    and the threshold are all derived from the same converted figures. The money floors are
    scaled by the same rate — otherwise a ₹15 floor would replace a $15 one and the tab would
    report a different set of anomalies depending on which currency you happened to be viewing,
    which is indefensible: the events did not change, only the units.
    """
    from . import currency as fx
    from .warehouse import warehouse

    # 1.0 whenever the figures are unconverted, so the floors keep their documented meaning.
    scale = 1.0
    if display_currency and display_currency.upper() not in ("", "BILLED", "USD"):
        scale = fx.rate_to(display_currency) or 1.0

    con = warehouse.connect(read_only=True, scope=scope, currency=display_currency)
    try:
        currency = con.execute(
            'SELECT coalesce(max(nullif("BillingCurrency", \'\')), \'USD\') FROM costs'
        ).fetchone()[0]
        span = con.execute(
            'SELECT min("ChargePeriodStart"), max("ChargePeriodStart"), count(*) FROM costs'
        ).fetchone()

        if not span or not span[2]:
            return {"anomalies": [], "count": 0, "currency": currency,
                    "note": "No cost data is loaded yet, so there is nothing to compare against."}

        results: list[dict[str, Any]] = []
        for dim in dimensions:
            results.extend(detect(_rows(con, dim, days), dim,
                                  min_delta=MIN_DELTA * scale,
                                  min_baseline=MIN_BASELINE * scale))

        # The window that was actually judged, which is not the same as the data on hand.
        # `span` is everything the warehouse holds; this is what `days` narrowed it to, and it
        # is what the reader is looking at. Reporting only the former put an unchanging date
        # range next to a finding count that changed with the period selector — two readings of
        # "period" on one screen that disagreed, and the wrong one was the more prominent.
        window = con.execute(
            'SELECT min("ChargePeriodStart"), max("ChargePeriodStart") FROM costs '
            f"WHERE \"ChargePeriodStart\" >= DATE '"
            f"{(datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()}'"
        ).fetchone()

        # Daily totals, so the tab can draw the estate's own line with the flagged days on it.
        totals = con.execute(
            'SELECT "ChargePeriodStart", sum("BilledCost") FROM costs '
            'GROUP BY 1 ORDER BY 1'
        ).fetchall()
    finally:
        con.close()

    # The same event shows up under several dimensions -- a service spike is also a resource
    # group spike and a subscription spike, with nearly the same delta. Reporting all three says
    # one thing three times and pushes the next real event off the page.
    #
    # Grouped by day and by *proportional* similarity rather than a rounded delta: the three
    # views of one event rarely agree to the cent ($97.18 / $96.38 / $82.10 above), so an exact
    # match never collapsed them. The most specific dimension wins, and the others are kept as
    # corroboration so the detail is still there when someone opens the row.
    rank = {name: i for i, name in enumerate(DIMENSIONS.values())}
    merged: list[dict[str, Any]] = []
    for a in sorted(results, key=lambda x: (x["day"], -abs(x["delta"]))):
        twin = next(
            (m for m in merged
             if m["day"] == a["day"]
             and m["direction"] == a["direction"]
             and abs(a["delta"]) >= abs(m["delta"]) * SAME_EVENT
             and abs(a["delta"]) <= abs(m["delta"]) / SAME_EVENT),
            None,
        )
        if twin is None:
            merged.append({**a, "also": []})
            continue
        twin.setdefault("also", []).append(
            {"dimension": a["dimension"], "key": a["key"], "delta": a["delta"]})
        # Keep whichever view names the thing most precisely.
        if rank.get(a["dimension"], 9) < rank.get(twin["dimension"], 9):
            twin["dimension"], twin["key"] = a["dimension"], a["key"]

    merged.sort(key=lambda a: (a["day"], abs(a["delta"])), reverse=True)

    high = [a for a in merged if a["severity"] == "high"]
    up = [a for a in merged if a["direction"] == "up"]
    return {
        "anomalies": merged[:top],
        "count": len(merged),
        "high": len(high),
        "increases": len(up),
        "decreases": len(merged) - len(up),
        "impact": round(sum(a["delta"] for a in up), 2),
        "currency": currency,
        "from": str(span[0]) if span[0] else None,
        "to": str(span[1]) if span[1] else None,
        # The judged window. Named separately from `from`/`to` so nothing that already reads
        # those changes meaning underneath it.
        "window_from": str(window[0]) if window and window[0] else None,
        "window_to": str(window[1]) if window and window[1] else None,
        "days": days,
        "settled_to": _settled_to(totals),
        "daily": [{"day": str(d), "cost": round(c or 0, 2)} for d, c in totals],
        "note": None if merged else (
            "Nothing unusual in the loaded period. Days are compared against a rolling "
            f"{WINDOW}-day median, and must move at least {MIN_DELTA * scale:,.0f} "
            f"{currency} to count."
        ),
    }


def _settled_to(totals: list[tuple]) -> str | None:
    """The last day whose figure is worth judging, given restatement."""
    if not totals:
        return None
    days = [str(d) for d, _ in totals]
    return days[-(SETTLE_DAYS + 1)] if len(days) > SETTLE_DAYS else days[0]

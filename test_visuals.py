"""
Checks on the two Overview visuals: the region map and the spend treemap.

These exist because both defects this pair shipped with were invisible to every other test in
the suite -- the app returned 200, the JSON was right, no console error appeared, and the
picture was still wrong.

  * The world path drew two hard horizontal lines straight across the map. Russia's Chukotka
    and Kiribati straddle +/-180 longitude, so consecutive ring points step from +179 to -179
    and the segment between them spans the entire width of the projection.

  * The stylesheet referenced --fs-xs, --fs-lg, --r1 and --w-semibold, none of which exist.
    CSS fails silently on an undefined custom property: the rule is dropped and the element
    renders at whatever it inherits, so it looks merely "a bit off" rather than broken.

Run: python test_visuals.py
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "web", "assets")

checks = 0
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if ok:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}{(' - ' + detail) if detail else ''}")
        failures.append(label)


def read(name: str) -> str:
    with open(os.path.join(ASSETS, name), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------- world geometry
print("\nworldmap.js — generated geometry")

world_src = read("worldmap.js")

for const in ("WORLD", "MAP_W", "MAP_H", "LAT_TOP", "LAT_BOTTOM"):
    check(f"exports {const}", re.search(rf"export const {const}\s*=", world_src) is not None)

m = re.search(r'export const WORLD = "([^"]*)"', world_src)
check("WORLD path is present and non-empty", bool(m and len(m.group(1)) > 1000))

path = m.group(1) if m else ""
map_w = float(re.search(r"export const MAP_W = ([\d.]+)", world_src).group(1))
map_h = float(re.search(r"export const MAP_H = ([\d.]+)", world_src).group(1))

# Walk every subpath, accumulating the relative linetos into absolute positions.
subpaths = [s for s in path.split("M")[1:]]
check("path contains many subpaths", len(subpaths) > 100, f"got {len(subpaths)}")

worst_dx = 0.0
worst_sub = -1
min_x = min_y = float("inf")
max_x = max_y = float("-inf")
points_total = 0

for i, sub in enumerate(subpaths):
    head, _, body = sub.partition("l")
    start = head.strip().split()
    x, y = float(start[0]), float(start[1])
    min_x, max_x = min(min_x, x), max(max_x, x)
    min_y, max_y = min(min_y, y), max(max_y, y)
    for dx, dy in re.findall(r"(-?\d+)\s*(-?\d+)", body.rstrip("Z")):
        dxf = float(dx)
        if abs(dxf) > worst_dx:
            worst_dx, worst_sub = abs(dxf), i
        x += dxf
        y += float(dy)
        min_x, max_x = min(min_x, x), max(max_x, x)
        min_y, max_y = min(min_y, y), max(max_y, y)
        points_total += 1

# The regression: a segment crossing the antimeridian spans nearly the whole width. Real
# coastline steps at 110m resolution are a small fraction of that. A quarter of the map is a
# generous ceiling that no genuine simplified segment comes close to.
check(
    "no segment spans the map horizontally (antimeridian)",
    worst_dx < map_w / 4,
    f"largest dx {worst_dx:.0f} of {map_w:.0f} in subpath {worst_sub}",
)

check("geometry stays within the viewBox horizontally", -1 <= min_x and max_x <= map_w + 1,
      f"x range {min_x:.0f}..{max_x:.0f}")
check("geometry stays within the viewBox vertically", -1 <= min_y and max_y <= map_h + 1,
      f"y range {min_y:.0f}..{max_y:.0f}")
check("geometry spans most of the width", (max_x - min_x) > map_w * 0.9)
check("Antarctica is excluded", max_y < map_h + 1)
check("point count is reasonable for 110m", 2000 < points_total < 20000, f"{points_total} points")

# Size is a product constraint, not a detail: the whole app is about 50 KB over the wire.
raw = len(world_src.encode())
check("world geometry stays under 32 KB raw", raw < 32_000, f"{raw:,} bytes")

check("carries a provenance note", "Natural Earth" in world_src)
check("is marked generated", "Do not hand-edit" in world_src or "regenerate" in world_src.lower())


# ------------------------------------------------------------------- region map
print("\nregionmap.js — plotting")

rm = read("regionmap.js")
check("imports the generated geometry", "from \"./worldmap.js\"" in rm)
check("does not ship a hand-traced coastline", "MERIDIANS" not in rm and "PARALLELS" not in rm)
check("separates overlapping bubbles", "function separate" in rm)
check("scales circles by area, not radius", "Math.sqrt" in rm)
check("counts rows that are not places", "NON_PLACES" in rm)
check("reports unmapped regions rather than dropping them silently",
      "unrecognised" in rm or "unmapped" in rm)
# A region with no coordinates must never be plotted at (0,0), which is in the Atlantic.
check("filters to regions with known coordinates", ".filter((r) => r.at)" in rm)


# ---------------------------------------------------------------------- treemap
print("\ntreemap.js — layout")

tm = read("treemap.js")
check("uses squarified layout", "function squarify" in tm)
# The bug: laying out against a notional rectangle then stretching with percentages discards
# the aspect ratios squarify exists to control.
check("measures the real container", "clientWidth" in tm and "clientHeight" in tm)
check("does not lay out against a fixed rectangle", "const W = 1000" not in tm)
check("re-lays out on resize", "ResizeObserver" in tm)
check("disconnects the observer when detached", "isConnected" in tm)
check("rolls the tail into one tile", "rolled" in tm)
check("drops labels that would not fit", "tight" in tm)


# ------------------------------------------------------------------ stylesheet
print("\napp.css — the new blocks")

css = read("app.css")

defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
# Properties set from markup rather than the stylesheet. `--i` is the finding's index, written
# as an inline style so each analysis card can stagger its own entrance — it is defined at every
# use site, just not in a file this check can see.
from_markup = {"--i"}
missing = sorted(used - defined - from_markup)
check(
    "every custom property used is defined",
    not missing,
    "undefined: " + ", ".join(missing) if missing else "",
)

for cls in ("grid-viz", "rg-land", "rg-dot", "rg-halo", "rg-legend", "rg-leg-bar",
            "tm-plot", "tm-tile", "tm-inner", "tm-val"):
    check(f"styles .{cls}", f".{cls}" in css)

# Every swatch the JS can emit must exist, in both themes, or a tile renders transparent.
swatch_count = len(re.findall(r"\.tm-c(\d+)\s*\{", css))
light_swatches = len(re.findall(r'\[data-theme="light"\]\s*\.tm-c\d+', css))
js_swatches = int(re.search(r"const SWATCHES = (\d+)", tm).group(1))
check("dark swatches cover every index the JS emits", swatch_count >= js_swatches,
      f"{swatch_count} css vs {js_swatches} js")
check("light swatches cover every index the JS emits", light_swatches >= js_swatches,
      f"{light_swatches} css vs {js_swatches} js")

# The treemap gap is drawn as a border so tile areas stay exactly proportional; a margin would
# shrink small tiles proportionally more and misstate the shares. Comments are stripped first:
# the explanatory comment in that block contains the words "not a margin:", which an earlier
# version of this check happily matched.
css_bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
tile_block = re.search(r"\.tm-tile\s*\{([^}]*)\}", css_bare)
check("tile spacing uses border, not margin",
      bool(tile_block) and "border:" in tile_block.group(1) and "margin:" not in tile_block.group(1))

check("panels stretch to equal height", "align-items: stretch" in css)
check("captions pin to the bottom of the panel", ".grid-viz .chart-note" in css
      and "margin-top: auto" in css)

# The merged savings list. Six rail entries became one, so the styles that carry the merge's
# reasoning — where a figure came from, and whether more than one scan agreed — have to exist,
# or the list looks like an ordinary table of numbers with no way to check them.
for cls in ("sv-list", "sv-card", "sv-head", "sv-body", "sv-chip", "sv-sources",
            "sv-badge", "sv-options", "sv-disputed", "sv-folded"):
    check(f"styles .{cls}", f".{cls}" in css)
check("a billed figure looks different from a projected one",
      ".sv-basis-billed" in css)
check("the corroboration badge is the one coloured thing on a collapsed row",
      ".sv-badge.good" in css and "var(--ok)" in css)

# The launcher is fixed to the bottom-right; so is the analysis panel's ask bar. At 1280 wide the
# 56px circle covered 46% of the Ask button, so a click meant to send a question shut the panel.
# Nothing about that is visible until the two happen to be on screen together.
check("the launcher gets out of the way once the analysis is open",
      bool(re.search(r"body\.analysing\s+\.bot\s*\{[^}]*opacity:\s*0", css_bare)))
check("and stops taking clicks while it is invisible",
      bool(re.search(r"body\.analysing\s+\.bot\s*\{[^}]*pointer-events:\s*none", css_bare)))


# ------------------------------------------------------------------- dashboard
print("\ndashboard.js — wiring")

dash = read("dashboard.js")
check("imports the treemap", 'from "./treemap.js"' in dash)
check("renders the treemap", "drawTreemap(" in dash)
check("renders the map", "drawRegionMap(" in dash)
check("pairs them in one row", 'class="grid-viz"' in dash)
check("treemap is fed subscriptions", re.search(r"drawTreemap\(\s*\$\(\"spendTreemap\"\),\s*d\.subscriptions", dash) is not None)
check("only one region map container", dash.count('id="regionMap"') == 1)
check("only one treemap container", dash.count('id="spendTreemap"') == 1)


# ----------------------------------------------------------------- the rail
print("\ndashboard.js — navigation hierarchy")

# The rail was three groups split by where the data came from — Cost, Analysis, Output — which
# put Budgets beside Rightsizing because both were live, and left thirteen unrelated entries
# under one heading. These five are the questions someone actually arrives with.
groups = re.findall(r'\{\s*id:\s*"(\w+)",\s*label:\s*"([^"]+)"', 
                    dash[dash.index("const RAIL_GROUPS"):dash.index("const GROUP_OF")])
check("the rail has five groups", len(groups) == 5, str([g[1] for g in groups]))
check("they are named for what they answer",
      [g[1] for g in groups] == ["Spend", "Monitor", "Optimize", "Govern", "Reports"])
# Monitor before Optimize, and Budgets first within it: "am I within budget" is the question
# that follows "what did I spend", well before "where could I cut". It also puts Budgets
# directly under Cost by Application / Tags, which is where tag budgets are created from.
_groups_src = dash[dash.index("const RAIL_GROUPS"):dash.index("const GROUP_OF")]
check("Monitor sits directly under Spend",
      _groups_src.index('id: "watch"') < _groups_src.index('id: "save"'))
check("and Budgets leads it, next to the tab budgets are made from",
      bool(re.search(r'id: "watch".*?members: \["budgets"', _groups_src, re.S)))
# The rail used to render whatever order `sections` happened to be built in, so a group could
# declare an order it did not get.
check("the rail honours the order a group declares",
      "const rank = new Map(g.members.map(" in dash)

members = re.findall(r'members:\s*\[([^\]]*)\]', dash)
listed = {m.strip().strip('"') for group in members for m in group.split(",") if m.strip()}
# Every fixed tab must be reachable. Either it is filed under a group, or it is a retired id
# that redirects to one — anything in neither would silently fall through to Spend and sit among
# the service families, which is where it would never be found.
retired = set(re.findall(r"^\s*(\w+):\s*\"savings\",", dash, re.M))
fixed = {"overview", "uptime", "tags", "savings", "waste", "rightsizing", "shutdown", "rates",
         "commitments", "advisor", "anomalies", "budgets", "history", "governance", "esu",
         "health", "report", "settings"}
check("every fixed tab is either filed under a group or redirected",
      fixed <= (listed | retired), f"missing: {sorted(fixed - listed - retired)}")
check("and none is both", not (listed & retired), f"both: {sorted(listed & retired)}")
check("no tab is filed twice",
      sum(len([m for m in g.split(",") if m.strip()]) for g in members) == len(listed))
# The rail was cut from 16 entries to 11, and this exists to stop it creeping back one
# well-argued tab at a time. Cost by hour took it to 12: it is the only view that reads the
# hours behind the money, so it could not be folded into another tab the way the six Optimize
# entries were. Raised deliberately, and still five short of where it started.
check("the rail is materially shorter than it was",
      len(listed) <= 12, f"{len(listed)} fixed entries (was 16)")

check("service families fall through to Spend", 'GROUP_OF.get(id) || "spend"' in dash)
check("only Spend is open on a first visit", 'readStore("railOpen", ["spend"])' in dash)
# The pre-paint script sets rail-mini on <html> because <body> does not exist yet. Leaving it
# there gave the stylesheet two sources of truth — and since its rules match either element, a
# stale html.rail-mini pinned the rail at 48px however the toggle was set.
check("the pre-paint class is handed over to body, not left on html",
      'document.documentElement.classList.remove("rail-mini")' in dash)
check("and only one element carries it afterwards",
      dash.count('document.body.classList.toggle("rail-mini"') == 1)
# Every drawChart call must pass an element. One passed a string and threw at runtime — on a tab
# most people open occasionally, and invisible to every static check until now.
check("drawChart is never handed a bare id",
      not re.search(r'drawChart\(\s*"', dash))

# Six tabs were folded into one. Anything still naming the old ids — a bookmark, a deep link
# from a finding, the AI analysis, or simply the tab someone was last on — has to land on the
# list that replaced them rather than falling back to Overview.
check("retired tabs are redirected rather than dropped", "MERGED_TABS" in dash)
for old in ("waste", "rightsizing", "shutdown", "rates", "advisor", "commitments"):
    check(f"{old} redirects to the merged list",
          bool(re.search(rf"{old}:\s*\"savings\"", dash)))
check("the redirect happens on selection", 'MERGED_TABS[id] || id' in dash)
check("and on restoring the remembered tab", "MERGED_TABS[current]" in dash)
check("the merged list has a renderer", "renderSavings" in dash)
check("and the rail offers exactly one Optimize entry",
      bool(re.search(r'id:\s*"save".*?members:\s*\[\s*"savings"\s*\]', dash, re.S)))

# The seven service families were seven rail entries reaching one renderer with a different id.
# As segments above the numbers they are what they always were: one view with a filter.
check("service families are marked so the rail leaves them out", "area: true" in dash)
check("and the rail actually filters on it", "!s.area" in dash)
check("there is a picker to reach them by", "function areaPicker" in dash)
check("Overview stays selected while you are inside one",
      's.id === "overview" && isArea(current)' in dash)
# renderRail and selectTab's fast path both decide what is marked, and they have to agree.
# When only the first knew the rule, the rail showed nothing selected on Compute & Web.
check("and the fast path agrees with the rail about it",
      'btn.dataset.tab === "overview" && isArea(id)' in dash)
check("the picker is wired from both views that carry it",
      dash.count("wireAreaPicker()") >= 3)
check("All spend is the first segment, not a separate destination",
      bool(re.search(r'seg\("overview",\s*"All spend"', dash)))
for cls in ("ar-picker", "ar-seg", "ar-name", "ar-cost"):
    check(f"styles .{cls}", f".{cls}" in css)

# "Custom export the cost data" wrote to blob storage and reported a name nobody could reach
# from a browser — correct data, entirely out of reach. It downloads now, through a real link
# for the same reason the report builder uses one: that is what makes the browser save a file
# natively rather than the page having to synthesise a click.
check("the selection can be downloaded", 'id="xDownload"' in dash)
check("through a real link, not a scripted click",
      bool(re.search(r'<a class="btn primary" id="xDownload"', dash)))
check("wired with the shared download helper",
      'wireDownloadLink(dl,' in dash and "/api/archive/export/download" in dash)
check("saving to storage is still offered", 'id="xExport"' in dash)
check("and both build their URL from one place", "function selectionQuery" in dash
      and dash.count("selectionQuery()") >= 2)
# A link has no :disabled, so an unusable selection has to lose its destination instead.
check("an unusable selection loses the link's destination",
      'dl.removeAttribute("href")' in dash)
check("and is marked unavailable to assistive tech",
      'dl.setAttribute("aria-disabled"' in dash)
# The label is typed, so it needs `input` — on `change` alone the URL kept whatever the label
# was when the tab rendered.
check("typing a label updates the download URL", "lbl.oninput = paintExportNote" in dash)

# The raw-row export sat on the schedules tab, which is where exports are *created in Azure* —
# not where someone wanting the rows would look. It belongs under the report builder, and the
# tab it left is now named for what it actually holds.
check("the schedules tab is named for its contents",
      '{ id: "settings", label: "Cost exports"' in dash)
check("and no longer calls itself a settings page",
      'label: "Export settings"' not in dash)
check("the selection export is built by the Export data tab",
      dash.index('id="xDownload"') > dash.index("async function renderReport")
      and dash.index('id="xDownload"') < dash.index("async function renderSettings"))
check("and is wired from there",
      "wireSelectionExport();" in dash
      and dash.index("wireSelectionExport();") < dash.index("async function renderSettings"))
check("its wiring is its own function, not the schedules one",
      "function wireSelectionExport()" in dash)
check("the schedules tab no longer draws it",
      dash[dash.index("async function renderSettings"):dash.index("function wireSelectionExport")]
      .count('id="xSubs"') == 0)
# It is named for what it does — a custom cut of the cost data — rather than for the widget
# above it, which is what "the current selection" described.
check("the section says what it produces",
      "<h3>Custom export the cost data</h3>" in dash)
check("and drops the name that described a control instead",
      "Export the current selection" not in dash)
# Two jobs on one tab. Without a break the export card read as a third panel of the builder,
# with the Download report button stranded between them looking like it applied to both.
check("a rule separates the two halves of the tab", '<hr class="tab-split">' in dash)
check("the rule is placed after the builder's download bar, not before it",
      dash.index('<hr class="tab-split">') > dash.index('id="buildNote"'))
check("and the split is styled", ".tab-split {" in css)
# A separator the same weight as the hairlines inside the panels reads as another panel edge,
# not as a break between two halves of the page.
check("the rule is stronger than the borders it sits between",
      bool(re.search(r"\.tab-split \{[^}]*background: var\(--border-strong\)", css, re.S)))
check("and its gap is wider than the board's own spacing",
      bool(re.search(r"\.tab-split \{[^}]*margin: var\(--s6\) 0 var\(--s5\)", css, re.S)))
check("the panel that opens the section is marked as doing so",
      '<section class="card section-lead">' in dash and ".card.section-lead" in css)

# The create form is why someone opens Cost exports; the list is the record of having used it.
# With fourteen schedules the form sat below a screenful of cards.
_settings = dash[dash.index("async function renderSettings"):dash.index("function snapshotOptions")]
check("the create form comes before the list of schedules",
      _settings.index('id="sCreate"') < _settings.index('class="schedules"'))
check("and the list is labelled, now that it is no longer the first thing",
      '<h4 class="sub-head">Existing schedules</h4>' in dash and ".sub-head {" in css)
check("the label is a step down from the panel title, not another one",
      bool(re.search(r"\.sub-head \{[^}]*font-size: var\(--t-micro\)", css, re.S)))

# Azure reports a budget as one number with no history, so the meter can say 93% without
# answering what follows: a steady climb landing just inside, or a breach three weeks ago?
check("each budget carries a chart of its own", "function drawBudgetChart" in dash)
check("drawn per budget rather than one combined axis",
      'querySelectorAll(".budget-chart")' in dash)
check("cumulative, because a budget is a running total by definition",
      "data: t.cumulative" in dash)
check("against a flat line at the limit, so the crossing point is the day it went over",
      "t.labels.map(() => amount)" in dash and "dash: true" in dash)
# The reference line is the backdrop, not a second measurement — same reasoning as the
# Overview's previous-period line.
check("the limit line is told apart by style rather than hue",
      "categorical: false" in dash.split("function drawBudgetChart")[1])
check("and the chart is styled as card content, not a panel inside a panel",
      ".budget-chart figure.chart" in css and "border: 0" in css)
# A tag-scoped budget drawn over whole-subscription spend would contradict its own headline:
# one budget here tracks $14 while its subscription spends $466.
check("a filter the local data cannot reproduce is admitted, not drawn over",
      "t.filtered_exactly" in dash and "will read high" in dash)
check("and a materially different total is named rather than smoothed away",
      "gap > 0.15" in dash)

# Ten budgets is more than a screen and a large estate has dozens, so the tab opens on a
# question — which ones need me, or the one called X — rather than on a scroll.
check("budgets can be filtered by state", "data-bstatus" in dash)
check("and found by name, subscription or what they track",
      "function paintBudgetList" in dash
      and "[b.name, b.subscription, b.filter]" in dash)
check("filtering redraws rather than refetching the live Azure call",
      dash.index("function paintBudgetList") > dash.index("async function renderBudgets")
      and "paintBudgetList(rows)" in dash)
check("the filter survives leaving the tab and coming back",
      "let budgetFilter = {" in dash)
check("an empty result says so instead of showing a blank list",
      "No budget matches" in dash)
# Cards are indexed into the unfiltered list, or a filtered view would draw the wrong budget's
# chart under the right budget's name.
check("a filtered card still finds its own chart",
      "rows.indexOf(b)" in dash)
check("the toolbar is styled and wraps rather than overflowing",
      ".budget-tools {" in css and "flex-wrap: wrap" in css)

# A tag budget is created from the Tags tab; this is the return leg of that journey.
check("a tag budget links back to the selection behind it",
      "data-tagsfor" in dash and "function showTagsFor" in dash)
check("and the Tags tab applies it case-insensitively, as Azure filters",
      "pendingTagSelection" in dash and "lower.get(String(rawKey).toLowerCase())" in dash)
check("arriving from a budget is explained rather than silently pre-selected",
      "tagState?.fromBudget" in dash and "Showing the tags" in dash)
# The two tabs count differently and both are right: Tags attributes a resource's whole cost
# to its tags, a budget counts only the charges carrying them. A $14 budget landing on a $44
# page with no explanation is the kind of thing that derails a demo.
check("and the difference in what each counts is stated",
      "attributes a resource's whole cost" in dash)
# The Refresh menu used to promise "Seconds, not minutes" for the quick path. It is not: the
# Query API is rate-limited hard enough that a busy day still takes minutes, and a control that
# lies about its own cost is worse than a slow one.
check("the quick option no longer promises seconds",
      "Seconds, not minutes" not in dash)
check("and says what actually makes it slow",
      "Azure rate-limits it" in dash)
check("the export path leads, being the only one with no report to build and no quota",
      dash.index('data-act="focus"') < dash.index('data-act="quick"'))
check("and the full-detail options are honest about ten minutes",
      "ten minutes or more" in dash)

# The button doubles as the freshness indicator, and after a refresh it kept yesterday's date
# until the next full page load — so a refresh that had plainly worked looked like it had not.
check("a finished refresh updates the date on the button",
      "if (w.to) lastAsOf = w.to;" in dash)

# Every route to fresh data is slow in its own way, and none of it needs watching — the data
# changes once a day, overnight. So it reloads itself.
_main = open(os.path.join(HERE, "app", "main.py"), encoding="utf-8").read()
check("the server hands over the parsed filter for that link",
      'b["tag_filter"] = [[k, v] for k, v in tags]' in _main)
check("the warehouse reloads itself on a schedule",
      "async def _daily_refresh" in _main)
check("started with the app and stopped with it",
      "_daily_refresh())" in _main and "daily.cancel()" in _main)
check("at an hour after Azure writes its exports",
      'AUTO_REFRESH_HOUR' in _main)
check("and it can be turned off",
      'os.getenv("AUTO_REFRESH", "true")' in _main)
check("it prefers the export, which touches no rate limit",
      "_load_export_pair(ok, max_files=60)" in _main)
# This check used to assert `ingest_export(max_files=60)` -- the exact call that cannot work.
# With no account, container or SAS the reader raises before reading anything, so every night
# would have failed into the log. The job had never fired when the test was written, so the
# assertion pinned the bug rather than the behaviour.
check("and it has somewhere to read from",
      "found = await discover_exports()" in _main
      and "ingest_export(max_files=60)" not in _main)
check("it never fights a refresh someone is watching",
      "an ingest is already running" in _main)
check("nor repeats work the warehouse already holds",
      "warehouse already holds" in _main)
check("and a bad night does not stop the next one",
      "must not stop tomorrow" in _main)

# renderReport used to claim every checkbox on the tab. The moved section brings its own, which
# carry no data-group -- so an unscoped selector would both steal their handler and index the
# report selection with `undefined`.
check("the report builder only claims its own checkboxes",
      'querySelectorAll("input[data-group]")' in dash
      and 'querySelectorAll("input[type=checkbox]")' not in dash)
# The section needs what the warehouse holds and which snapshots exist; fetching those after the
# builder's options would add a round trip to a tab that already waits on one.
check("the data it needs is fetched alongside the builder's options",
      bool(re.search(r"const \[opts, arch, wh\] = await Promise\.all", dash)))
check("and the schedules tab stopped fetching what it no longer draws",
      bool(re.search(r"const \[sched, arch, me\] = await Promise\.all", dash)))
# An anchor styled as a button has to stay identical to one, so it joins the same rules rather
# than copying them.
check("a.btn shares the button rules rather than duplicating them",
      "button, a.btn {" in css)
check("including the primary treatment", "button.primary, a.btn.primary {" in css)
check("and has a disabled state, which :disabled cannot give a link",
      "a.btn.disabled" in css)
# Every group collapses, including the one holding the open tab. Refusing that meant Spend — the
# group with nine entries and the default tab — could never be closed, which broke exactly the
# case the rule was written to protect.
check("any group can be collapsed, including the current one",
      "if (openGroups.has(id)) openGroups.delete(id);" in dash)
check("a collapsed group says the open tab is inside it", "holds-current" in dash
      and ".rail-group.holds-current" in css)
check("jumping across groups opens the destination", "openGroups.add(groupOf(id))" in dash)
check("the choice survives a reload", 'writeStore("railOpen"' in dash)
# Overview is the sum of the families listed beneath it, so adding the group up reports twice
# the estate's spend — which it did, until it was measured against the API.
check("the group summary does not double-count",
      'items.find((s) => s.id === "overview")' in dash)
# Keyboard nav reads the DOM because the narrow layout shows every tab regardless of the
# accordion; deriving it from state alone would skip entries that are plainly on screen.
check("arrow keys only walk what is on screen", "b.offsetParent !== null" in dash)
check("the header reports its state to assistive tech", 'aria-expanded="${open}"' in dash)
check("and says what it controls", 'aria-controls="railgrp-' in dash)

check("the collapsed rail lies down into a strip on narrow screens",
      ".rail-head { display: none; }" in css)
check("and shows every tab there", ".rail-items, .rail-items[hidden] { display: flex; }" in css)
check("the chevron turns when a group opens", ".rail-group.open .rail-chev" in css)


# ---------------------------------------------------------------- chart colour
print("\napp.js — telling series apart")

app = read("app.js")

# The ordered blue ramp is right for slices ranked by cost and wrong for named categories:
# "On demand", "Committed" and "Spot" came out as three barely distinguishable blues, which is
# a legend you decode rather than read.
check("there is a categorical palette as well as an ordered ramp",
      "CATEGORICAL_DARK" in app and "CATEGORICAL_LIGHT" in app)
check("it is used when a chart has several series", "spec.categorical ?? manySeries" in app)
check("blue and amber lead it — the pair that survives colour vision deficiency",
      '"#479EF5", "#FFB900"' in app)
# Colour alone is never the answer: shapes carry the same information for anyone who cannot
# separate the hues, and for anything printed in grey.
check("series also carry a marker shape", "POINT_STYLES" in app)
check("the legend shows each series' own shape", "isPie ? { pointStyle" in app)
# A category must be the same colour in every chart it appears in, or two views of the same
# three things disagree.
check("shared categories have a fixed colour slot", "CATEGORY_SLOT" in app)
check("Spot is the same colour wherever it appears", '["spot", 2]' in app)
# The ordered ramp's neighbours are nearly identical — correct for ranked slices, useless for
# two separate lines.
check("multi-series ordered charts spread across the ramp", "const BAND = 5" in app)
check("and stay inside the legible half of it",
      "Math.round((index * BAND)" in app)
# Drawing and re-tinting used to choose colours by different rules, so a theme switch turned a
# legible chart into two adjacent shades of one blue.
check("one function decides every series colour", "function seriesColour" in app)
check("drawChart tolerates being handed an id", 'typeof container === "string"' in app)
check("the re-tint uses it too", "categorical: isCat, isPie: isPieChart" in app)
check("and remembers what the chart was", "chart.$clCategorical = useCategorical" in app)

print("\ndashboard.js — comparison charts name both dates")

# One date above two numbers reads as though both were measured on it. They were not: the
# second is the same day of the previous window.
check("the trend chart passes per-point dates", "pointLabels: labels" in dash)
check("including the previous window's own dates", "previous_labels" in dash)
check("the tooltip prints them", "c.dataset.pointLabels?.[c.dataIndex]" in app)
check("the comparison keeps the ordered ramp rather than a rival hue",
      "categorical: false" in dash)
check("pricing models are marked categorical", "categorical: true" in dash)

py = open(os.path.join(HERE, "app", "dashboard.py"), encoding="utf-8").read()
check("the server sends the previous window's dates", '"previous_labels"' in py)


print(f"\n{checks - len(failures)}/{checks} checks passed")
if failures:
    print("failed: " + ", ".join(failures))
    sys.exit(1)

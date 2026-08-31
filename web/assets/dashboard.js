/**
 * The tabbed dashboard: the answers people want before they think of a question.
 *
 * Importing app.js also boots the chat — the two share the subscription picker, the formatting
 * and the chart renderer, so there is one implementation of each rather than two that drift.
 *
 * Every tab is one request against the local warehouse, so switching tabs is instant and the
 * model is never involved. The stale-resources tab is the exception: it needs live inventory
 * from Azure, so it says so and shows its own progress.
 */
import { allSubscriptions, askAgent, drawChart, esc, get, money, moneyHtml, onScopeChange, refreshHeader, scopeIds, whoAmI } from "./app.js";
import { close as closeAnalysis, invalidate as invalidateAnalysis, isOpen as isAnalysisOpen, openAnalysis, setAnalysisContext, wireAnalysis } from "./analysis.js";
import { mountBot, setBotState } from "./bot.js";
import { railIcon } from "./icons.js";
import { drawRegionMap } from "./regionmap.js";
import { drawTreemap } from "./treemap.js";

const $ = (id) => document.getElementById(id);

let current = readStore("costTab", "overview");
let days = Number(readStore("costDays", 30)) || 30;
let sections = [];
let loadToken = 0; // guards against a slow tab landing after a faster one

// Switching back to a tab you were just on should be instant. Keyed by tab, period and scope,
// so changing any of those is a miss rather than a stale hit; short-lived because the point of
// this app is that the numbers move.
const CACHE_MS = 90_000;
const cache = new Map();

async function cached(key, fetcher) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.value;
  // The *promise* is cached, not the value it resolves to. Caching the value leaves a window
  // between the first call and its response in which every other caller also misses and fires
  // its own request — which is why the same endpoint was being fetched twice on every load.
  const pending = (async () => {
    try {
      return await fetcher();
    } catch (err) {
      cache.delete(key); // a failure must not be remembered for 90 seconds
      throw err;
    }
  })();
  cache.set(key, { at: Date.now(), value: pending });
  return pending;
}

/** Anything that loads newer data invalidates everything — the figures all move together. */
function clearCache() {
  cache.clear();
}

function readStore(key, fallback) {
  try {
    const v = JSON.parse(localStorage.getItem(key));
    return v ?? fallback;
  } catch {
    return fallback;
  }
}
function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private browsing — the choice just won't survive a reload */
  }
}

const qs = () => {
  const scope = scopeIds();
  const cur = readStore("costCurrency", "");
  return `?days=${days}` +
    (scope.length ? `&scope=${encodeURIComponent(scope.join(","))}` : "") +
    (cur ? `&currency=${encodeURIComponent(cur)}` : "");
};

/** Cache key for the current view: tab, period, scope and display currency together. */
const key = (name) => `${name}|${days}|${scopeIds().join(",")}|${readStore("costCurrency", "")}`;

const sectionsData = () => cached(key("sections"), () => get(`/api/dashboard/sections${qs()}`, { timeout: 30000 }));

const pct = (n) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(1)}%`);

/** Rising cost is bad, falling is good — the opposite of most dashboards, so be explicit. */
const trendClass = (n) => (n == null ? "" : n > 1 ? "up" : n < -1 ? "down" : "flat");

/** The tools name findings in snake_case; a heading should read as English. */
const title = (s) =>
  String(s || "Finding")
    .replace(/_/g, " ")
    // Uppercase the acronym but not its plural: "IPs", not "IPS".
    .replace(/\b(vm|ip|nic|nsg|sql)(s?)\b/gi, (_, word, plural) => word.toUpperCase() + plural)
    .replace(/^./, (c) => c.toUpperCase());

// ------------------------------------------------------------------- tab bar
async function loadTabs() {
  // The header's download links carry the period and scope, so they have to be re-pointed
  // whenever either changes — before anyone can click them.
  refreshExportLinks();
  let data;
  try {
    data = await sectionsData();
  } catch (err) {
    $("tabs").innerHTML = "";
    fail(err.forbidden ? err.message : "Could not load the cost breakdown.");
    return;
  }

  sections = [
    { id: "overview", label: "Overview", cost: data.total, currency: data.currency },
    // The service families. Marked so the rail leaves them out: each is `renderSection` with a
    // different id, so seven rail entries were seven ways to reach one view. They stay in
    // `sections` because the picker inside Overview is built from them, and because a deep link
    // to `compute` should still resolve.
    ...(data.sections || []).filter((s) => s.cost > 0).map((s) => ({ ...s, area: true })),
    { id: "uptime", label: "Cost by hour", analysis: true },
    { id: "tags", label: "Cost by Application / Tags", analysis: true },
    { id: "anomalies", label: "Anomalies", analysis: true },
    { id: "history", label: "History", analysis: true },
    { id: "budgets", label: "Budgets", live: true },
    // One entry where there were six. Orphaned resources, Rightsizing, Shutdown savings, Rate
    // optimization, Advisor and Commitments all answered "where can we spend less", and their
    // totals shared resources — so the rail offered six doors into one room and no way to tell
    // how much of what was behind them had been counted twice.
    { id: "savings", label: "Savings", live: true },
    { id: "governance", label: "Governance", live: true },
    { id: "esu", label: "End of support", live: true },
    { id: "health", label: "Retirements", live: true },
    { id: "report", label: "Export data", build: true },
    { id: "settings", label: "Cost exports", build: true },
  ];

  // A tab that has gone. Someone whose last visit left them on Rightsizing should land on the
  // list that now contains it, not be silently dropped back to Overview.
  if (MERGED_TABS[current]) current = MERGED_TABS[current];
  if (!sections.some((s) => s.id === current)) current = "overview";

  // The button doubles as the freshness indicator: how current the data is, is exactly what
  // tells you whether you need to press it. Remembered rather than applied immediately —
  // the control appears only once the admin check returns, which can land after this.
  lastAsOf = data.as_of || lastAsOf;
  applyFreshness();

  renderRail();
  wireRail();
  $("tabs").onkeydown = (e) => {
    // Only what is on screen. Stepping into a collapsed group would move the selection somewhere
    // the eye cannot follow, which is worse than the key doing nothing.
    const order = visibleTabs();
    const i = order.indexOf(current);
    // A vertical list, so up/down are the natural keys. Left/right stay bound too: the rail
    // lies down into a horizontal strip on narrow screens.
    if (e.key === "ArrowDown" || e.key === "ArrowRight") selectTab(order[(i + 1) % order.length]);
    else if (e.key === "ArrowUp" || e.key === "ArrowLeft") {
      selectTab(order[(i - 1 + order.length) % order.length]);
    } else if (e.key === "Home") selectTab(order[0]);
    else if (e.key === "End") selectTab(order[order.length - 1]);
    else return;
    e.preventDefault();
    $("tabs").querySelector(".rail-item.on")?.focus();
  };

  await renderTab();
}

// --------------------------------------------------------------- the rail
/**
 * The rail is grouped by what someone is trying to do, not by where the data comes from.
 *
 * It used to be three groups — Cost, Analysis, Output — split by whether a tab hit the warehouse
 * or Azure. That is an implementation detail: it put Budgets next to Rightsizing because both are
 * live, and left "Analysis" as thirteen unrelated entries, which is a list to read rather than a
 * menu to use. These five are the questions instead: what did I spend, where can I spend less,
 * is something wrong, am I compliant, and give me the data.
 *
 * Membership is explicit rather than derived, because the right group for a tab is a judgement
 * about what it answers. Anything not named here is a service family from the cost data itself,
 * and belongs under Spend with the rest of the money.
 */
const RAIL_GROUPS = [
  { id: "spend", label: "Spend", hint: "Where the money went",
    members: ["overview", "uptime", "tags"] },
  // Monitor sits directly under Spend, and Budgets leads it. Two reasons, and the second is
  // the one that matters: "am I within budget" is the question that follows "what did I
  // spend", well before "where could I cut" — and a budget here is usually a *tag* budget,
  // created from the selection on the tab immediately above it. Ordering the rail this way
  // puts the two ends of that workflow next to each other without pretending a tag breakdown
  // is a risk signal, which is what moving it into this group would have claimed.
  { id: "watch", label: "Monitor", hint: "What changed, and what is at risk",
    members: ["budgets", "anomalies", "history"] },
  { id: "save", label: "Optimize", hint: "Where to spend less",
    members: ["savings"] },
  { id: "govern", label: "Govern", hint: "Lifecycle and compliance",
    members: ["governance", "esu", "health"] },
  { id: "data", label: "Reports", hint: "Export the data and schedule the exports",
    members: ["report", "settings"] },
];

/**
 * Tabs that were folded into another one, and where they went.
 *
 * Six separate entries under Optimize became one. They are kept here rather than deleted so a
 * bookmark, a deep link from a finding, or a hash someone pasted into a ticket still lands
 * somewhere useful instead of falling back to Overview — and so the AI analysis, which names a
 * tab for each finding it reports, does not have to know the rail was rearranged.
 */
const MERGED_TABS = {
  waste: "savings",
  rightsizing: "savings",
  shutdown: "savings",
  rates: "savings",
  advisor: "savings",
  commitments: "savings",
};

const GROUP_OF = new Map(
  RAIL_GROUPS.flatMap((g) => g.members.map((id) => [id, g.id])));

/** Service families come from the estate, so anything unnamed is spend. */
const groupOf = (id) => GROUP_OF.get(id) || "spend";

/** A service family rather than a fixed tab — Compute & Web, Networking, and the rest. */
const isArea = (id) => sections.some((s) => s.id === id && s.area);

/**
 * The area picker that replaced seven rail entries.
 *
 * Compute & Web, Networking, Storage, AI & Data, Integration & IoT, Security & Ops and Other
 * services were seven buttons that all reached `renderSection` with a different id — the same
 * view of a different slice. On the rail they read as seven places to go; as a row of segments
 * above the numbers they read as what they are, which is one view with a filter on it.
 *
 * Overview is the first segment rather than a separate destination, because Overview *is* the
 * unfiltered case: the sum of the families listed beside it.
 */
function areaPicker(currentId) {
  const areas = sections.filter((s) => s.area);
  if (!areas.length) return "";
  const seg = (id, label, cost, cur) => `<button class="ar-seg" data-area="${esc(id)}"
      aria-pressed="${currentId === id}" type="button">
      <span class="ar-name">${esc(label)}</span>
      ${cost == null ? "" : `<span class="ar-cost">${esc(money(cost, cur))}</span>`}
    </button>`;
  const all = sections.find((s) => s.id === "overview");
  return `<div class="ar-picker" role="group" aria-label="Cost area">
      ${seg("overview", "All spend", all?.cost, all?.currency)}
      ${areas.map((a) => seg(a.id, a.label, a.cost, a.currency)).join("")}
    </div>`;
}

/** Wired after every render of a view that carries the picker. */
function wireAreaPicker() {
  for (const b of $("tabBody").querySelectorAll(".ar-seg")) {
    b.onclick = () => selectTab(b.dataset.area);
  }
}

/**
 * Which groups are expanded. Collapsed by default apart from Spend: the whole point of the
 * hierarchy is that you see the handful of entries you came for, not all twenty-four.
 */
let openGroups = new Set(readStore("railOpen", ["spend"]));

// The pre-paint script sets this on <html> so the rail never renders wide and snaps narrow.
// Hand it over to <body> and clear it: <html> carries the preference for exactly one moment —
// the paint before this module runs — and leaving it there gave the stylesheet two sources of
// truth. Since the rules match either element, a stale `html.rail-mini` pinned the rail at 48px
// however the toggle was set, and expanding it silently did nothing.
if (document.documentElement.classList.contains("rail-mini")) {
  document.documentElement.classList.remove("rail-mini");
  document.body.classList.add("rail-mini");
}

/** Tab ids in rendered order, skipping anything the reader cannot currently see.
 *
 *  Read from the DOM rather than from `openGroups`, because the narrow layout shows every tab
 *  regardless of the accordion — deriving this from state alone would make the arrow keys skip
 *  entries that are plainly on screen.
 */
function visibleTabs() {
  return [...$("tabs").querySelectorAll("[data-tab]")]
    .filter((b) => b.offsetParent !== null)
    .map((b) => b.dataset.tab);
}

function toggleGroup(id) {
  // Every group collapses, including the one you are standing in. This used to refuse — on the
  // reasoning that you must be able to see where you are — which meant Spend could never be
  // closed, because the default tab lives in it. That is the group with nine entries and the
  // most worth collapsing, so the rule broke exactly the case it was written for.
  //
  // Nothing is lost by closing it: the board still shows the tab, and the group header carries
  // a marker saying the current tab is inside. You can always see where you are; you just are
  // not forced to look at eight siblings to do it.
  if (openGroups.has(id)) openGroups.delete(id);
  else openGroups.add(id);
  writeStore("railOpen", [...openGroups]);
  renderRail();
  wireRail();
  $("tabs").querySelector(`[data-group="${id}"]`)?.focus();
}

/** The rail is re-rendered whenever a group opens or closes, so its handlers are re-attached
 *  from one place rather than at each call site. */
function wireRail() {
  for (const btn of $("tabs").querySelectorAll("[data-tab]")) {
    btn.onclick = () => selectTab(btn.dataset.tab);
  }
  for (const head of $("tabs").querySelectorAll("[data-group]")) {
    head.onclick = () => toggleGroup(head.dataset.group);
  }
  const mini = $("railMini");
  if (mini) {
    mini.onclick = () => setRailMini(!document.body.classList.contains("rail-mini"));
  }
}

function renderRail() {
  // Icon-only mode forces every group open. Collapsed groups hide their items behind a header,
  // and once the headers are gone too there would be no way to reach them — the rail would be
  // five dividers and nothing else.
  const iconOnly = document.body.classList.contains("rail-mini");

  // The collapse control sits above the navigation, where Azure's does. At the foot it was
  // below the fold on a short window with every group open — a control you have to scroll to
  // find is a control nobody finds. Here it is the first thing in the rail and always visible.
  $("tabs").innerHTML = `<div class="rail-top">
        <button class="rail-mini-btn" id="railMini" type="button"
                aria-pressed="${iconOnly}"
                title="${iconOnly ? "Show labels" : "Collapse to icons"}">
          <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
            <path d="${iconOnly ? "M8 6l4 4-4 4" : "M12 6l-4 4 4 4"}" fill="none"
                  stroke="currentColor" stroke-width="1.6"
                  stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="rail-name">${iconOnly ? "Expand" : "Collapse"}</span>
        </button>
      </div>`
    + RAIL_GROUPS.map((g) => {
    // Ordered by `members`, not by the order `sections` happens to be built in. The group
    // already declares which tabs it holds; having it declare the order too keeps the rail's
    // shape in one readable place instead of split across two lists that have to agree.
    // Service families are excluded — they are reached from the picker inside Overview.
    const rank = new Map(g.members.map((id, i) => [id, i]));
    const items = sections
      .filter((s) => groupOf(s.id) === g.id && !s.area)
      .sort((a, b) => (rank.get(a.id) ?? 99) - (rank.get(b.id) ?? 99));
    if (!items.length) return "";
    const open = iconOnly || openGroups.has(g.id);

    // A collapsed group still has to say what is inside it, or closing one loses information.
    //
    // Spend can total its own money — but not by adding its entries up: Overview *is* the sum of
    // the families listed under it, so adding them together reports twice the estate's spend.
    // Where a roll-up entry exists, it is the answer; otherwise the honest summary is how many
    // entries there are, which is enough to tell you whether it is worth opening.
    const roll = items.find((s) => s.id === "overview");
    const summary = roll
      ? money(roll.cost, roll.currency)
      : `${items.length}`;

    const body = items
      .map((s) => {
        // Overview owns the service families now, so it stays marked while you are inside one.
        // Without this the rail shows nothing selected on Compute & Web, and the reader has no
        // way to tell where they are.
        const on = s.id === current || (s.id === "overview" && isArea(current));
        const amount = s.live
          ? '<span class="rail-sub live">live</span>'
          : s.build
            ? ""
            : s.cost != null
              ? `<span class="rail-sub">${esc(money(s.cost, s.currency))}</span>`
              : "";
        // The title is what carries the name once the labels are hidden, so it is always
        // present rather than added when the rail collapses — a tooltip that only exists in
        // one mode is a tooltip that gets forgotten in the other.
        const tip = s.cost != null
          ? `${s.label} · ${money(s.cost, s.currency)}`
          : s.label;
        return `<button role="tab" data-tab="${esc(s.id)}" aria-selected="${on}"
                class="rail-item${on ? " on" : ""}" tabindex="${on ? 0 : -1}"
                title="${esc(tip)}"
                >${railIcon(s.id)}<span class="rail-name">${esc(s.label)}</span>${amount}</button>`;
      })
      .join("");

    // Where the reader is. Once a group can be collapsed while you are standing in it, the
    // header has to say so — otherwise closing Spend while on Overview leaves nothing on the
    // rail marked, and the selection appears to have been lost.
    const holdsCurrent = items.some((s) => s.id === current)
      || (g.id === "spend" && isArea(current));
    const currentLabel = holdsCurrent
      ? (items.find((s) => s.id === current)?.label || "")
      : "";

    return `<div class="rail-group${open ? " open" : ""}${
        holdsCurrent && !open ? " holds-current" : ""}" role="presentation">
        <button class="rail-head" data-group="${esc(g.id)}" aria-expanded="${open}"
                aria-controls="railgrp-${esc(g.id)}"
                title="${esc(holdsCurrent && !open
                  ? `${g.label} — ${currentLabel} is open`
                  : g.hint)}"
                ${iconOnly ? "tabindex=\"-1\" aria-hidden=\"true\"" : ""}>
          <svg class="rail-chev" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" stroke-width="1.6"
                  stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <span class="rail-label">${esc(g.label)}</span>
          <span class="rail-count">${esc(summary)}</span>
        </button>
        <div class="rail-items" id="railgrp-${esc(g.id)}" role="presentation" ${
          open ? "" : "hidden"}>${body}</div>
      </div>`;
  }).join("");
}

/**
 * Icon-only navigation, as Azure's portal does it.
 *
 * The rail is the widest fixed thing on the page, and on a laptop next to the agent panel it is
 * competing with the numbers for room. Collapsing it returns ~150px to the board while keeping
 * every destination one click away — which is the trade only worth making because the icons
 * carry meaning on their own.
 *
 * The choice is remembered, and the names survive as tooltips and as the accessible name of each
 * button, so nothing is lost to anyone reading with a screen reader.
 */
function setRailMini(on) {
  document.body.classList.toggle("rail-mini", on);
  writeStore("railMini", on);
  renderRail();
  wireRail();
  // Charts measure their container, and the board just changed width by 150px. Without this the
  // canvas keeps its old size until something else forces a resize.
  window.dispatchEvent(new Event("resize"));
}

function selectTab(id) {
  if (!id) return;
  // A tab that has been folded into another sends you to its replacement rather than nowhere.
  // Deep links from findings, the AI analysis and anything anyone bookmarked all still name the
  // six ids that used to sit under Optimize.
  id = MERGED_TABS[id] || id;
  // Choosing a tab is a request to look at that tab. The analysis covers the board, so leaving
  // it open would change the page underneath and show the reader nothing — the click would
  // appear to have done nothing at all. Closing happens even when the tab is unchanged, because
  // clicking the tab you are already on is exactly how someone asks to get back to it.
  closeAnalysis();
  if (id === current) return;
  const moved = groupOf(id) !== groupOf(current);
  current = id;
  writeStore("costTab", id);
  // Name the tab on the panel itself, so a screen reader announces what was opened rather than
  // an unlabelled region — and so the skip link lands somewhere that identifies itself.
  const label = (sections.find((s) => s.id === id) || {}).label || id;
  const panel = $("tabBody");
  if (panel) panel.setAttribute("aria-label", label);

  // Jumping across groups — from a deep link on another tab, say — has to open the destination,
  // or the selection lands somewhere invisible.
  if (moved) {
    openGroups.add(groupOf(id));
    writeStore("railOpen", [...openGroups]);
    renderRail();
    wireRail();
  } else {
    for (const btn of $("tabs").querySelectorAll("[data-tab]")) {
      // The same rule renderRail uses: Overview owns the service families, so it stays marked
      // while you are inside one. Matching on the id alone left the rail with nothing selected
      // on Compute & Web, because that entry is no longer on the rail to be selected.
      const on = btn.dataset.tab === id
        || (btn.dataset.tab === "overview" && isArea(id));
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", String(on));
      btn.tabIndex = on ? 0 : -1;
    }
  }
  renderTab();
}

function fail(message) {
  $("tabBody").innerHTML = `<div class="banner err">${esc(message)}</div>`;
}

/**
 * A floating notice, for feedback about an action rather than about the tab. Exports are the
 * reason this exists: a banner inside the tab body can sit above the fold or be wiped by the
 * next render, so a failed download looked exactly like nothing happening at all.
 */
function toast(message, kind = "ok", action) {
  let host = $("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.setAttribute("role", kind === "err" ? "alert" : "status");

  const text = document.createElement("span");
  text.textContent = message;
  el.appendChild(text);

  if (action) {
    const a = document.createElement("a");
    a.className = "toast-action";
    a.textContent = action.label;
    a.href = action.href;
    el.appendChild(a);
  }
  const close = document.createElement("button");
  close.className = "toast-x";
  close.type = "button";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "×";
  close.onclick = () => el.remove();
  el.appendChild(close);

  host.appendChild(el);
  setTimeout(() => el.remove(), action ? 20000 : 6000);
  return el;
}

// -------------------------------------------------------------------- render
const LIVE_WAIT = {
  savings: "Reading four Azure scans and merging what they each found…",
  waste: "Scanning Azure for idle and orphaned resources…",
  budgets: "Reading budgets and current spend from Azure…",
  history: "Reading the archived snapshots…",
  anomalies: "Comparing every day against its own baseline…",
  shutdown: "Reading hourly CPU to find machines only used in the daytime…",
  rightsizing: "Reading CPU metrics from Azure Monitor…",
  esu: "Checking operating system and SQL versions against their support dates…",
  governance: "Checking tag coverage and network configuration across the estate…",
  rates: "Reading Advisor's reservation and savings plan recommendations…",
  health: "Reading Azure Service Health for retirements and required migrations…",
  advisor: "Fetching Advisor recommendations…",
  report: "Loading what you can include…",
};

async function renderTab() {
  const mine = ++loadToken;
  const body = $("tabBody");
  TABLES.clear();          // the tables from the previous tab are gone with its markup
  // The picker is emitted by the views it filters, so replacing the body with a skeleton took
  // it off screen for the length of the fetch — half a second in which the strip vanished from
  // under the pointer and a second click landed on nothing at all. Drawing it above the
  // skeleton keeps it in place, already showing the destination as pressed: a filter should
  // stay put while the thing it filters reloads.
  const keepsPicker = current === "overview" || isArea(current);
  body.innerHTML = (keepsPicker ? areaPicker(current) : "") + skeleton(current);
  if (keepsPicker) wireAreaPicker();
  startSkeletonClock();

  try {
    if (current === "report") return await renderReport(mine);
    if (current === "settings") return await renderSettings(mine);
    if (current === "savings") return await renderSavings(mine);
    if (current === "waste") return await renderWaste(mine);
    if (current === "rightsizing") return await renderRightsizing(mine);
    if (current === "shutdown") return await renderShutdown(mine);
    if (current === "esu") return await renderEsu(mine);
    if (current === "governance") return await renderGovernance(mine);
    if (current === "commitments") return await renderCommitments(mine);
    if (current === "budgets") return await renderBudgets(mine);
    if (current === "history") return await renderHistory(mine);
    if (current === "anomalies") return await renderAnomalies(mine);
    if (current === "tags") return await renderTags(mine);
    if (current === "uptime") return await renderUptime(mine);
    if (current === "rates") return await renderRates(mine);
    if (current === "health") return await renderHealth(mine);
    if (current === "advisor") return await renderAdvisor(mine);
    if (current === "overview") return await renderOverview(mine);
    return await renderSection(mine, current);
  } catch (err) {
    if (mine !== loadToken) return;
    fail(err?.message || "Something went wrong loading this tab.");
  } finally {
    if (mine === loadToken) {
      clearInterval(skTimer);
      wireTables(body);
    }
  }
}

/**
 * A placeholder shaped like the answer.
 *
 * A line of "Loading…" tells you nothing about what is coming and makes the page jump when it
 * lands. Blocks the size of the real panels hold the layout still and make the wait legible —
 * particularly on the live tabs, which take real seconds because they are querying Azure.
 */
function skeleton(tab) {
  const wait = LIVE_WAIT[tab];
  const bar = (h) => `<div class="sk" style="height:${h}px"></div>`;
  return `<div class="sk-wrap" role="status" aria-label="${esc(wait || "Loading")}">
      ${wait ? `<p class="sk-note">${esc(wait)}
        <span class="sk-clock" id="skClock" aria-hidden="true"></span></p>` : ""}
      ${bar(74)}
      ${bar(240)}
      <div class="sk-row">${bar(200)}${bar(200)}</div>
    </div>`;
}

/**
 * A clock on the slow tabs.
 *
 * Waste takes 7.6s and Governance 6.7s because both scan live Azure inventory. A static
 * skeleton for that long is indistinguishable from a hang — the honest fix is not to pretend it
 * is fast but to show that it is still working, and after ten seconds to say so in words.
 */
let skTimer = null;
function startSkeletonClock() {
  clearInterval(skTimer);
  const started = Date.now();
  skTimer = setInterval(() => {
    const el = $("skClock");
    if (!el) return clearInterval(skTimer);
    const s = Math.round((Date.now() - started) / 1000);
    if (s < 3) return;
    el.textContent = s < 10 ? ` ${s}s` : ` ${s}s · Azure is slow to answer, still waiting`;
  }, 1000);
}

function kpiRow(items) {
  return `<div class="kpis">${items
    .map(
      (k) => `<div class="kpi">
        <span class="k">${esc(k.label)}</span>
        <span class="v">${k.value}</span>
        ${k.sub ? `<span class="s ${k.cls || ""}">${k.sub}</span>` : ""}
      </div>`
    )
    .join("")}</div>`;
}

/**
 * A data table.
 *
 * Rows are rendered through a shared renderer so that sorting and paging are properties of
 * every table rather than something bolted onto the few that grew large. Both are done in the
 * page against data already in memory: no request, no round trip, no spinner.
 *
 * Paging is not decoration. "Top resources" can run to hundreds of rows, and a hundred DOM
 * nodes that nobody scrolls to still cost layout on every theme switch and every resize.
 *
 * Every table ends in a totals row. Panels beside each other share a row height, so a short
 * table left a band of blank surface below its last row — and blank surface inside a panel
 * reads as something failing to load. A total is the one thing that genuinely belongs at the
 * foot of a column of money, so the space ends up carrying information rather than being
 * padded out with decoration.
 */
let tableSeq = 0;
function table(title, rows, columns, note, opts = {}) {
  if (!rows.length) {
    return `<section class="card"><h3>${esc(title)}</h3>
      <p class="muted empty">Nothing in this period.</p></section>`;
  }
  const id = `t${++tableSeq}`;
  const pageSize = opts.pageSize ?? 25;
  TABLES.set(id, {
    rows, columns, pageSize, page: 0, sort: null, dir: 1, title,
    total: opts.total !== false,
  });
  return `<section class="card" data-table="${id}">
      <h3>${esc(title)}</h3>
      ${note ? `<p class="muted note">${esc(note)}</p>` : ""}
      <div class="table-wrap" tabindex="0" role="region" aria-label="${esc(title)}, scrollable">
        ${tableMarkup(id)}
      </div>
      ${pagerMarkup(id)}
      ${totalMarkup(id)}
    </section>`;
}

/**
 * The footer: what the column adds up to, across every row rather than the page on screen.
 *
 * Only columns that carry a `total` formatter get a figure — summing a percentage column
 * would produce 100% and summing a delta column would be meaningless without context.
 */
function totalMarkup(id) {
  const t = TABLES.get(id);
  if (!t.total) return "";
  const cells = t.columns.filter((c) => c.total);
  if (!cells.length) return "";
  return `<div class="tfoot">
      <span class="tfoot-k">${t.rows.length} ${t.rows.length === 1 ? "row" : "rows"}</span>
      ${cells
        .map(
          (c) =>
            `<span class="tfoot-v"><span class="tfoot-lbl">${esc(c.label)}</span>${c.total(
              t.rows
            )}</span>`
        )
        .join("")}
    </div>`;
}

/** Live table state, keyed by the id embedded in the markup. Cleared whenever a tab renders. */
const TABLES = new Map();

function tableMarkup(id) {
  const t = TABLES.get(id);
  const head = t.columns
    .map((c, i) => {
      const sortable = c.sort !== false;
      const cls = [c.num ? "num" : "", sortable ? "sortable" : "",
                   t.sort === i ? (t.dir === 1 ? "sort-asc" : "sort-desc") : ""]
        .filter(Boolean).join(" ");
      const aria = t.sort === i ? (t.dir === 1 ? "ascending" : "descending") : "none";
      return `<th${cls ? ` class="${cls}"` : ""}${sortable ? ` data-col="${i}" role="columnheader"
        aria-sort="${aria}" tabindex="0"` : ""}>${esc(c.label)}</th>`;
    })
    .join("");

  const start = t.page * t.pageSize;
  const body = view(t)
    .slice(start, start + t.pageSize)
    .map((r) => `<tr>${t.columns.map((c) => `<td${c.num ? ' class="num"' : ""}>${c.cell(r)}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/** Rows in their current order. The original array is never mutated — sorting is a view. */
function view(t) {
  if (t.sort == null) return t.rows;
  const c = t.columns[t.sort];
  const key = c.value || ((r) => c.cell(r));
  return [...t.rows].sort((a, b) => {
    const x = key(a);
    const y = key(b);
    // Numbers compare as numbers; anything else compares as text, case-insensitively.
    const both = typeof x === "number" && typeof y === "number";
    const cmp = both ? x - y : String(x).localeCompare(String(y), undefined, { numeric: true });
    return cmp * t.dir;
  });
}

function pagerMarkup(id) {
  const t = TABLES.get(id);
  if (t.rows.length <= t.pageSize) return "";
  const pages = Math.ceil(t.rows.length / t.pageSize);
  const from = t.page * t.pageSize + 1;
  const to = Math.min(t.rows.length, from + t.pageSize - 1);
  return `<div class="pager">
      <span class="pager-count">${from}–${to} of ${t.rows.length}</span>
      <span class="pager-btns">
        <button type="button" data-page="prev" ${t.page === 0 ? "disabled" : ""}>Previous</button>
        <button type="button" data-page="next" ${t.page >= pages - 1 ? "disabled" : ""}>Next</button>
      </span>
    </div>`;
}

/**
 * Wire every table in the panel just rendered.
 *
 * Delegated from the panel rather than bound per header, so re-rendering the rows after a
 * sort does not need the handlers reattached.
 */
function wireTables(root) {
  for (const section of root.querySelectorAll("[data-table]")) {
    const id = section.dataset.table;
    if (!TABLES.has(id)) continue;

    const redraw = () => {
      section.querySelector(".table-wrap").innerHTML = tableMarkup(id);
      const pager = section.querySelector(".pager");
      const markup = pagerMarkup(id);
      if (pager) pager.outerHTML = markup;
      else if (markup) section.insertAdjacentHTML("beforeend", markup);
    };

    section.addEventListener("click", (e) => {
      const th = e.target.closest("th[data-col]");
      if (th) return sortBy(id, Number(th.dataset.col), redraw);
      const btn = e.target.closest("[data-page]");
      if (!btn) return;
      const t = TABLES.get(id);
      t.page += btn.dataset.page === "next" ? 1 : -1;
      redraw();
    });
    section.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const th = e.target.closest("th[data-col]");
      if (!th) return;
      e.preventDefault();
      sortBy(id, Number(th.dataset.col), redraw);
    });
  }
}

function sortBy(id, col, redraw) {
  const t = TABLES.get(id);
  // Same column toggles direction; a new column starts descending for numbers (the largest
  // cost is what someone is looking for) and ascending for text.
  if (t.sort === col) t.dir = -t.dir;
  else {
    t.sort = col;
    t.dir = t.columns[col].num ? -1 : 1;
  }
  t.page = 0;
  redraw();
}

/** A share-of-total bar, so a table of numbers reads as proportions at a glance. */
function bar(value, max) {
  const w = max > 0 ? Math.max(2, Math.round((value / max) * 100)) : 0;
  return `<span class="meter"><span style="width:${w}%"></span></span>`;
}

async function renderSection(token, id) {
  const d = await cached(key(id), () => get(`/api/dashboard/${id}${qs()}`, { timeout: 30000 }));
  if (token !== loadToken) return;
  if (d.empty) {
    $("tabBody").innerHTML = `<p class="muted empty">No data loaded for this period yet.</p>`;
    return;
  }

  const cur = d.currency;
  const k = d.kpis;
  const topService = d.services[0];
  const maxService = topService ? topService.cost : 0;
  const maxResource = d.resources_top[0]?.cost || 0;

  $("tabBody").innerHTML = `
    ${areaPicker(id)}
    ${kpiRow([
      { label: `Spend, last ${d.days} days`, value: moneyHtml(k.total, cur, 2) },
      {
        label: "vs previous period",
        value: pct(k.change_pct),
        cls: trendClass(k.change_pct),
        sub: k.previous ? `was ${esc(money(k.previous, cur))}` : "no prior period",
      },
      { label: "Share of all spend", value: k.share_pct == null ? "—" : `${k.share_pct}%` },
      { label: "Billed resources", value: String(k.resources) },
      { label: "Services", value: String(k.services) },
    ])}
    <div id="secChart" class="card chart-card"></div>
    <div class="grid-2">
      ${table(
        "By service",
        d.services,
        [
          { label: "Service", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost, maxService)}` },
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null
      )}
      ${table("By region", d.regions, [
        { label: "Region", cell: (r) => esc(r.name) },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ])}
    </div>
    ${table(
      "Top resources",
      d.resources_top,
      [
        { label: "Resource", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxResource)}` },
        { label: "Resource group", cell: (r) => esc(r.grp) },
        { label: "Service", cell: (r) => esc(r.service) },
        { label: "Region", cell: (r) => esc(r.region) },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      "Ranked by spend in this period, not by size."
    )}
    ${d.mixed_currency ? '<p class="muted note">This estate bills in more than one currency; figures show the largest.</p>' : ""}
  `;

  if (d.trend.labels.length > 1) {
    drawChart($("secChart"), {
      type: "area",
      title: `${d.label} — daily spend`,
      labels: d.trend.labels,
      datasets: [{ label: d.label, data: d.trend.values }],
      currency: cur,
    });
  } else {
    $("secChart").remove();
  }
  wireAreaPicker();
}

/**
 * The opening view.
 *
 * This is the first thing anyone sees, so it answers the four questions people actually arrive
 * with — what am I spending, what is it going on, who is spending it, and what changed — rather
 * than showing one chart and a lot of white space.
 *
 * It is one request. Everything below comes from a single warehouse pass, so a page this dense
 * still lands in milliseconds and touches Azure not at all.
 */
async function renderOverview(token) {
  const d = await cached(key("executive"), () =>
    get(`/api/dashboard/executive${qs()}`, { timeout: 30000 }));
  if (token !== loadToken) return;
  if (d.empty) {
    $("tabBody").innerHTML = `<p class="muted empty">No data loaded for this period yet.</p>`;
    return;
  }

  const cur = d.currency;
  const k = d.kpis;
  const maxArea = d.areas.reduce((m, a) => Math.max(m, a.cost), 0);
  const maxService = d.services.reduce((m, s) => Math.max(m, s.cost), 0);
  const maxSub = d.subscriptions.reduce((m, s) => Math.max(m, s.cost), 0);
  const maxRegion = (d.regions || []).reduce((m, s) => Math.max(m, s.cost), 0);

  /** A change, written the way someone reads it: direction, amount, and what it means. */
  const move = (r) => {
    if (r.previous === 0) return `<span class="delta new">new</span>`;
    if (Math.abs(r.delta) < 0.01) return `<span class="delta flat">—</span>`;
    const up = r.delta > 0;
    return `<span class="delta ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${esc(
      money(Math.abs(r.delta), cur)
    )}</span>`;
  };

  const changeCol = {
    label: `vs prev ${d.days}d`,
    num: true,
    value: (r) => r.delta,
    cell: move,
    // Summing deltas is the one total that means something here: what the rows *net* to.
    // Summing their costs would just restate the cost column.
    total: (rows) => {
      const net = rows.reduce((n, r) => n + r.delta, 0);
      if (Math.abs(net) < 0.01) return `<span class="delta flat">—</span>`;
      const up = net > 0;
      return `<span class="delta ${up ? "up" : "down"}">${up ? "▲" : "▼"} ${esc(
        money(Math.abs(net), cur)
      )}</span>`;
    },
  };

  $("tabBody").innerHTML = `
    ${areaPicker("overview")}
    ${kpiRow([
      {
        label: `Spend, last ${d.days} days`,
        value: moneyHtml(k.total, cur, 2),
        sub: k.previous ? `was ${esc(money(k.previous, cur))}` : "no prior period",
      },
      {
        label: "vs previous period",
        value: pct(k.change_pct),
        cls: trendClass(k.change_pct),
        sub:
          k.previous != null
            ? `${k.total >= k.previous ? "+" : "−"}${esc(
                money(Math.abs(k.total - k.previous), cur)
              )}`
            : "",
      },
      { label: "Average per day", value: moneyHtml(k.daily_avg, cur, 2) },
      {
        label: "Billed resources",
        value: String(k.resources),
        sub: `${k.services} services · ${k.regions} regions`,
      },
      {
        label: "Subscriptions",
        value: String(k.subscriptions),
        sub: "in scope",
      },
      {
        label: "Top 5 services",
        value: k.top5_share_pct == null ? "—" : `${k.top5_share_pct}%`,
        sub: "of all spend",
      },
    ])}

    <div id="trendChart" class="card chart-card"></div>

    <!-- Shape of the estate, then its geography. The treemap answers "how concentrated is this"
         and the map answers "where does it run" — both read at a glance, and both are followed
         further down by the same numbers as sortable tables for anyone who needs the exact
         figure rather than the impression. -->
    <div class="grid-viz">
      <div id="spendTreemap"></div>
      <div id="regionMap"></div>
    </div>

    <!-- Subscriptions first: it is how the estate is *organised* and usually how it is billed
         and owned, so it is the cut most people want before any technical breakdown. Full
         width because there are only ever a few of them and the names are long enough that
         half a row would truncate them. -->
    ${table(
      "By subscription",
      d.subscriptions,
      [
        { label: "Subscription", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxSub)}` },
        changeCol,
        {
          label: "Share",
          num: true,
          value: (r) => r.cost,
          cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
        },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      null
    )}

    <div class="grid-2">
      ${table(
        "Where the money goes",
        d.areas,
        [
          { label: "Area", cell: (r) => `${esc(r.label)} ${bar(r.cost, maxArea)}` },
          {
            label: "Share",
            num: true,
            value: (r) => r.cost,
            cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
          },
          changeCol,
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null
      )}
      ${table(
        "Biggest movers",
        d.movers,
        [
          { label: "Service", cell: (r) => esc(r.name || "—") },
          changeCol,
          { label: "Now", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2) },
        ],
        `What changed most against the previous ${d.days} days, by amount.`
      )}
    </div>

    <!-- Paired by length rather than by topic: both of these run to eight rows, so the row
         has no slack in it. -->
    <div class="grid-2">
      ${table(
        "Top services",
        d.services,
        [
          { label: "Service", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost, maxService)}` },
          {
            label: "Share",
            num: true,
            value: (r) => r.cost,
            cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
          },
          { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        ],
        null
      )}
      ${
        d.resource_groups.length
          ? table(
              "Top resource groups",
              d.resource_groups,
              [
                { label: "Resource group", cell: (r) => esc(r.name) },
                changeCol,
                {
                  label: "Share",
                  num: true,
                  value: (r) => r.cost,
                  cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
                },
                { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
              ],
              null
            )
          : ""
      }
    </div>

    <!-- The map above answers "where is it concentrated" at a glance; this answers "exactly how
         much, and is it moving". -->

    ${table(
      "By region",
      d.regions || [],
      [
        { label: "Region", cell: (r) => `${esc(r.name)} ${bar(r.cost, maxRegion)}` },
        {
          label: "Resources",
          num: true,
          value: (r) => r.resources || 0,
          cell: (r) => String(r.resources || 0),
          total: (rows) => String(rows.reduce((n, r) => n + (r.resources || 0), 0)),
        },
        changeCol,
        {
          label: "Share",
          num: true,
          value: (r) => r.cost,
          cell: (r) => (r.share_pct == null ? "—" : `${r.share_pct}%`),
        },
        { label: "Cost", num: true, value: (r) => r.cost, cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      null
    )}
    ${d.mixed_currency ? '<p class="muted note">This estate bills in more than one currency; figures show the largest.</p>' : ""}
  `;

  drawRegionMap($("regionMap"), d.regions, cur);
  drawTreemap($("spendTreemap"), d.subscriptions, cur, {
    title: "Spend hierarchy",
    kind: "Subscription",
  });

  if (d.trend.labels.length > 1) {
    // The final day of an export is usually still being written. Left in, the line dives to
    // near zero and the opening screen reads as "spend collapsed" — so it comes out of the
    // chart and the caption says so. It stays in every total: it is real money, just not a
    // whole day of it.
    const cut = d.trend.partial_last ? -1 : undefined;
    const labels = cut ? d.trend.labels.slice(0, cut) : d.trend.labels;
    const values = cut ? d.trend.values.slice(0, cut) : d.trend.values;
    const last = d.trend.labels[d.trend.labels.length - 1];

    drawChart($("trendChart"), {
      type: "area",
      title: `Daily spend — last ${d.days} days, against the ${d.days} before`,
      labels,
      datasets: [
        { label: "This period", data: values, pointLabels: labels },
        // Aligned by position rather than by date, so day 1 sits above day 1 — which is why
        // each point carries its own date for the tooltip. Without it the hover shows one date
        // above both numbers and silently misattributes the comparison.
        {
          label: "Previous period",
          data: d.trend.previous.slice(0, values.length),
          pointLabels: (d.trend.previous_labels || []).slice(0, values.length),
          dash: true,
        },
      ],
      currency: cur,
      // Two views of one measure, not two measures: the dashed reference line is told apart by
      // its line style, and giving it a rival hue would make last month look like a second
      // subject rather than the backdrop this month is being read against.
      categorical: false,
      note: d.trend.partial_last
        ? `${last} is still being written by the cost export, so it is left off the line. It is counted in every total above.`
        : "",
    });
  } else {
    $("trendChart").remove();
  }
  wireAreaPicker();
}

/**
 * One savings list, with the duplicates collapsed.
 *
 * Replaces six tabs — Orphaned resources, Rightsizing, Shutdown savings, Rate optimization,
 * Advisor and Commitments — that answered the same question separately and overlapped while
 * doing it. Each had its own total, and those totals shared resources, so the only way to know
 * what the estate could actually save was to work out by hand which rows were the same VM.
 *
 * The server does that reconciliation now. What is left for this to do is show the result in a
 * way that keeps the reader's trust: say how much is billed today versus estimated, mark the
 * rows that more than one scan agrees on, and never present a number without saying where it
 * came from.
 */
let savingsFilter = "all";

async function renderSavings(token) {
  let d;
  try {
    // Four live Azure scans behind this, the slowest being the orphan inventory.
    d = await get(`/api/dashboard/savings${qs()}`, { timeout: 180000 });
  } catch (err) {
    if (token !== loadToken) return;
    fail(err?.message || "Could not gather savings opportunities.");
    return;
  }
  if (token !== loadToken) return;

  const opps = d.opportunities || [];
  const cur = d.currency;

  if (!opps.length) {
    $("tabBody").innerHTML = `${kpiRow([{ label: "Opportunities", value: "0" }])}
      <p class="muted empty">Nothing worth changing found in this scope.
      ${d.error ? esc(d.error) : ""}</p>`;
    return;
  }

  const cats = (d.categories || []).filter((c) => c.count > 0);
  const shown = () => (savingsFilter === "all"
    ? opps : opps.filter((o) => o.category === savingsFilter));

  const kpis = [
    {
      label: `Recoverable, ${d.days}d`, value: money(d.billed_total, cur, 2),
      sub: "billed today", cls: "good",
    },
    {
      label: "Estimated further", value: money(d.projected_total, cur, 2),
      sub: "projected by Advisor",
    },
    { label: "Opportunities", value: String(d.count) },
  ];
  // Only worth a tile when there is something to report; a permanent "0 corroborated" reads as
  // a fault rather than as an estate whose scans happen not to overlap.
  if (d.merged) {
    kpis.push({
      label: "Confirmed twice", value: String(d.merged),
      sub: "found by two scans", cls: "good",
    });
  }
  if (d.variants_folded) {
    kpis.push({
      label: "Duplicates folded", value: String(d.variants_folded),
      sub: "re-estimates of the same choice",
    });
  }

  const chip = (id, label, n, total) => `<button class="sv-chip" data-cat="${esc(id)}"
      aria-pressed="${savingsFilter === id}">
      ${esc(label)} <span class="sv-chip-n">${n}</span>
      ${total ? `<span class="sv-chip-m">${esc(money(total, cur, 0))}</span>` : ""}
    </button>`;

  const filters = `<div class="sv-filters" role="group" aria-label="Filter by kind">
      ${chip("all", "Everything", opps.length, d.total)}
      ${cats.map((c) => chip(c.id, c.label, c.count, c.total)).join("")}
    </div>`;

  const unavailable = Object.keys(d.unavailable || {});
  const warn = unavailable.length
    ? `<p class="muted warn">Could not read ${esc(unavailable.join(", "))}, so the total
       below is incomplete.</p>`
    : "";

  $("tabBody").innerHTML = `${kpiRow(kpis)}
    ${warn}
    <p class="muted">${esc(d.note || "")}</p>
    ${filters}
    <div class="sv-list" id="svList">${shown().map((o) => savingsCard(o, d)).join("")}</div>`;

  const redraw = () => {
    $("svList").innerHTML = shown().map((o) => savingsCard(o, d)).join("");
    wireSavingsCards();
  };
  for (const b of $("tabBody").querySelectorAll(".sv-chip")) {
    b.onclick = () => {
      savingsFilter = b.dataset.cat;
      for (const other of $("tabBody").querySelectorAll(".sv-chip")) {
        other.setAttribute("aria-pressed", String(other.dataset.cat === savingsFilter));
      }
      redraw();
    };
  }
  wireSavingsCards();
}

/** Expanding a card is the only interaction, so it is wired from one place after every redraw. */
function wireSavingsCards() {
  for (const head of $("tabBody").querySelectorAll(".sv-head[aria-expanded]")) {
    head.onclick = () => {
      const open = head.getAttribute("aria-expanded") === "true";
      head.setAttribute("aria-expanded", String(!open));
      const body = head.parentElement.querySelector(".sv-body");
      if (body) body.hidden = open;
    };
  }
}

function savingsCard(o, d) {
  const cur = o.currency || d.currency;
  const money2 = (v) => money(v, cur, 2);

  // Corroboration is the one thing the old six tabs could not show, because it only exists once
  // the rows are put side by side. It is also the most useful signal on the card: a resource
  // three independent checks agree on is a safer place to start than a larger number one
  // heuristic guessed at.
  const badge = o.corroborated
    ? `<span class="sv-badge good" title="${esc(o.sources.map((s) => s.label).join(" and "))}
        both found this">${o.sources.length} scans agree</span>`
    : "";
  const conf = `<span class="sv-conf sv-conf-${esc(o.confidence)}">${esc(o.confidence)}
     confidence</span>`;

  const count = o.count > 1 ? `<span class="sv-count">${o.count} items</span>` : "";

  // Where the figure came from, always. A saving without a provenance is a number to argue with.
  const sources = `<ul class="sv-sources">${o.sources.map((s) => `<li>
      <span class="sv-src-label">${esc(s.label)}</span>
      <span class="sv-src-basis sv-basis-${esc(s.basis)}">${
        s.basis === "billed" ? "from the bill" : "estimate"}</span>
      <span class="sv-src-claim">${esc(s.claim)}</span></li>`).join("")}</ul>`;

  // Two scans that priced the same resource differently. Shown rather than averaged: the gap is
  // usually one scan seeing only part of the resource, and that is worth knowing before acting.
  const disputed = o.disputed
    ? `<p class="sv-disputed">The scans priced this differently —
       ${esc(money2(o.disputed.low))} against ${esc(money2(o.disputed.high))}.
       The larger is shown.</p>`
    : "";

  const options = o.options.length > 1
    ? `<div class="sv-options"><h4>Ways to buy this</h4>
       <p class="muted">You take one of these, not all of them.</p>
       <ul>${o.options.map((x, i) => `<li class="${i === 0 ? "best" : ""}">
          <span class="sv-opt-label">${esc(x.label)}</span>
          <span class="sv-opt-money">${esc(money(x.annual, cur, 0))}<span class="sv-opt-per">
            a year</span></span>
          ${x.detail ? `<span class="sv-opt-note">${esc(x.detail)}</span>` : ""}
        </li>`).join("")}</ul></div>`
    : "";

  const folded = o.folded
    ? `<p class="sv-folded">Azure priced this ${o.folded + o.options.length} times over
       different lookback windows; those are re-estimates of the same choice, not separate
       opportunities.</p>`
    : "";

  const items = (o.items || []).length
    ? `<table class="sv-items"><thead><tr><th>Resource</th><th>Group</th><th>Region</th>
        <th class="num">Cost</th></tr></thead><tbody>
        ${o.items.map((i) => `<tr><td>${esc(i.name || "—")}</td>
          <td>${esc(i.group || "—")}</td><td>${esc(i.region || "—")}</td>
          <td class="num">${i.cost ? esc(money2(i.cost)) : "—"}</td></tr>`).join("")}
      </tbody></table>
      ${o.more ? `<p class="muted">and ${o.more} more</p>` : ""}`
    : "";

  const where = [o.resource, o.resource_group, o.region].filter(Boolean).join(" · ");
  const annual = o.annual
    ? `<span class="sv-annual">${esc(money(o.annual, cur, 0))} a year</span>` : "";

  return `<article class="sv-card sv-${esc(o.category)}">
    <button class="sv-head" aria-expanded="false">
      <span class="sv-main">
        <span class="sv-title">${esc(o.title)}</span>
        <span class="sv-meta">
          <span class="sv-cat">${esc(o.category_label)}</span>
          ${count}${badge}${conf}
        </span>
        ${where ? `<span class="sv-where">${esc(where)}</span>` : ""}
      </span>
      <span class="sv-money">
        <span class="sv-window">${esc(money(o.window, cur, 2))}</span>
        <span class="sv-window-note">over ${d.days} days</span>
        ${annual}
      </span>
      <span class="sv-caret" aria-hidden="true">▾</span>
    </button>
    <div class="sv-body" hidden>
      <p class="sv-why">${esc(o.why || "")}</p>
      ${disputed}
      <h4>How this was found</h4>
      ${sources}
      ${folded}
      ${options}
      ${o.action ? `<p class="sv-action"><strong>What to do:</strong> ${esc(o.action)}</p>` : ""}
      ${items}
    </div>
  </article>`;
}

async function renderWaste(token) {
  let d;
  try {
    // Live Azure inventory across every subscription in scope — minutes on a large estate.
    d = await get(`/api/dashboard/waste${qs()}`, { timeout: 180000 });
  } catch (err) {
    if (token !== loadToken) return;
    fail(err?.message || "Could not scan for orphaned resources.");
    return;
  }
  if (token !== loadToken) return;

  const findings = (d.findings || []).filter((f) => (f.count || 0) > 0);
  const cur = d.currency;
  if (!findings.length) {
    $("tabBody").innerHTML = `${kpiRow([
      { label: "Idle or orphaned items", value: "0" },
    ])}<p class="muted empty">Nothing idle or orphaned found in this scope. ${
      d.error ? esc(d.error) : ""
    }</p>`;
    return;
  }

  const cards = findings
    .map((f) => {
      const rows = (f.items || []).slice(0, 12);
      const cols = [
        { label: "Resource", cell: (r) => esc(r.name || r.id || "—") },
        { label: "Resource group", cell: (r) => esc(r.resource_group || "—") },
        { label: "Region", cell: (r) => esc(r.location || r.region || "—") },
        {
          label: `Cost, ${d.period_days}d`,
          num: true,
          cell: (r) => (r.cost == null ? "—" : esc(money(r.cost, r.currency || cur, 2))),
        },
      ];
      const heading = `${title(f.category)} — ${f.count} item${f.count === 1 ? "" : "s"}`;
      const note = f.cost
        ? `${money(f.cost, cur, 2)} over the last ${d.period_days} days. ${f.why || ""}`
        : f.why || "No direct cost — clutter rather than spend.";
      return table(heading, rows, cols, note);
    })
    .join("");

  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: "Idle or orphaned items", value: String(d.total_items ?? 0) },
      {
        label: `Cost, last ${d.period_days} days`,
        value: d.total_cost == null ? "mixed currency" : esc(money(d.total_cost, cur, 2)),
      },
      { label: "Subscriptions scanned", value: String(d.subscriptions ?? 0) },
    ])}
    <p class="muted note">Live inventory joined to what each item actually cost. Nothing here is
      deleted for you, and some idle resources exist deliberately — treat it as a list to review.</p>
    ${cards}`;
}

// ------------------------------------------------------------- refresh data
// Pulling newer cost data is the one write this app can make, so it is admin-only, explicit,
// and reports what it is doing. Two routes: the Cost Details API (works anywhere, slow) and a
// Cost Management export already being written to storage (the scalable path on a big tenant).
let lastAsOf = null;

/** Show how current the data is on the button, whenever both facts are known. */
function applyFreshness() {
  if (!lastAsOf || $("refresh").hidden || $("refreshBtn").classList.contains("busy")) return;
  const when = new Date(`${lastAsOf}T00:00:00`);
  setRefreshLabel(
    `Refresh · to ${when.toLocaleDateString(undefined, { day: "numeric", month: "short" })}`
  );
}

function setRefreshLabel(text, busy = false) {
  $("refreshLabel").textContent = text;
  $("refreshBtn").classList.toggle("busy", busy);
  $("refreshBtn").disabled = busy;
  if (!busy && text === "Refresh data") applyFreshness();
}

function openRefresh(open) {
  $("refreshMenu").hidden = !open;
  $("refreshBtn").setAttribute("aria-expanded", String(open));
  if (open) buildRefreshMenu();
}

/**
 * Four choices, not thirteen.
 *
 * This menu used to offer three lookback periods and then every Cost Management export it could
 * see — nine of them on this tenant, listed by resource name and storage account. That is a
 * chooser for someone who already knows which blob container holds their data, and most of those
 * exports sit behind a private endpoint and fail the moment you pick one.
 *
 * What actually differs between the useful options is *where the numbers come from*, so that is
 * what the menu asks. The period is fixed at three months, which is what the dashboard's
 * comparisons need; there is no reason to make someone choose it every time.
 *
 * Quick leads because the other three are all slow for different reasons, and waiting ten
 * minutes to see today's number is not a thing anyone does twice. It reads the Query API, which
 * answers synchronously — measured at eight seconds for a month of a subscription against forty
 * seconds *per period* just to generate a detail report. What it gives up is columns, and the
 * order here is honest about that: quick first for the number, FOCUS for the whole picture.
 */
function buildRefreshMenu() {
  const menu = $("refreshMenu");
  if (menu.dataset.built === "1") return;

  menu.innerHTML = `
    <div class="head">Reload cost data</div>
    <button data-act="focus">FOCUS report
      <span class="sub">Reads an export Azure already wrote to storage — no report to build
        and no rate limit. The fastest route, and the one to schedule</span></button>
    <button data-act="quick">Quick refresh
      <span class="sub">Live query, capped at about a minute. Fewer columns — no quantities,
        benefits or tags — and it keeps whatever it got if Azure rate-limits it</span></button>
    <button data-act="api" data-metric="AmortizedCost">Amortized · full detail
      <span class="sub">Every column, including reservations and tags — but Azure builds the
        report on request, so this takes ten minutes or more</span></button>
    <button data-act="api" data-metric="ActualCost">Actual · full detail
      <span class="sub">The same, billed on the day rather than spread across a
        reservation's term</span></button>
    <p class="note">Loads the last 3 months and replaces those periods; nothing is duplicated.</p>
    <div class="archive-note" id="archiveNote"></div>`;

  for (const btn of menu.querySelectorAll("[data-act='api']")) {
    btn.onclick = () =>
      startIngest(`/api/ingest?months=3&metric=${btn.dataset.metric}`, "POST");
  }
  // The same endpoint, asked to use the synchronous Query API instead of a generated report.
  // Actual rather than amortized: a query cannot return benefit attribution either way, and
  // actual is the metric that needs no reservation spreading to be meaningful without it.
  menu.querySelector("[data-act='quick']").onclick = () =>
    startIngest("/api/ingest?months=3&metric=ActualCost&quick=true", "POST");
  // No export picker: the server ranks FOCUS ahead of amortized ahead of actual and takes the
  // best one that actually has a storage destination.
  menu.querySelector("[data-act='focus']").onclick = () =>
    startIngest("/api/exports/ingest", "POST", { max_files: 60 });

  menu.dataset.built = "1";
  paintArchiveNote();
}

/**
 * Where the archive goes, and whether it is working.
 *
 * Inside the refresh menu because that is the moment it matters: every option above this line
 * writes a dated copy, and a refresh that silently stopped archiving would otherwise be
 * invisible until someone went looking for a file that was never written.
 */
async function paintArchiveNote() {
  const host = $("archiveNote");
  if (!host) return;
  host.innerHTML = `<span class="muted">Checking the archive…</span>`;
  try {
    const a = await get("/api/archive?limit=6", { timeout: 30000 });
    if (!a.enabled) {
      host.innerHTML = `<span class="muted">No archive configured.</span>`;
      return;
    }
    if (!a.reachable) {
      host.innerHTML = `<span class="bad">Archive unreachable</span>
        <span class="muted">${esc(a.error || "")}</span>`;
      return;
    }
    const names = Object.keys(a.latest || {});
    const last = names.length
      ? names.map((k) => `${esc(k)} · ${esc((a.latest[k].name || "").split("/").pop())}`).join("<br>")
      : "nothing archived yet";
    host.innerHTML = `<span class="ok">Archiving to ${esc(a.account)}/${esc(a.container)}</span>
      <span class="muted">One file per dataset per day. Latest:<br>${last}</span>`;
  } catch {
    host.innerHTML = `<span class="muted">Could not read the archive status.</span>`;
  }
}

async function startIngest(url, method, body) {
  openRefresh(false);
  setRefreshLabel("Starting…", true);
  try {
    const res = await fetch(url, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      setRefreshLabel("Refresh data");
      showIngest(false);
      toast(
        typeof data.detail === "string"
          ? data.detail
          : `Could not start the refresh (${res.status}).`,
        "err"
      );
      return;
    }
    // Say which source is actually being used. "FOCUS report" resolves to a specific export
    // server-side, and knowing which one it picked is the difference between a number you can
    // trust and one you have to go and verify.
    if (data.export) toast(`Loading from ${data.export} (${data.type || "export"}).`);
    watchIngest();
  } catch (err) {
    setRefreshLabel("Refresh data");
    showIngest(false);
    toast("Could not reach the server to start a refresh.", "err");
  }
}

/** Poll until the ingest stops, then rebuild everything from the new data. */
async function watchIngest() {
  // Long enough for six months across a dozen subscriptions; the label keeps counting so a
  // slow run never looks stuck.
  const maxPolls = 800;
  showIngest(true);
  for (let i = 0; i < maxPolls; i++) {
    await new Promise((r) => setTimeout(r, 3000));
    let w;
    try {
      w = await get("/api/warehouse", { timeout: 20000 });
    } catch {
      continue; // a restart mid-ingest shouldn't abandon the watch
    }
    const state = w.ingest || {};
    if (state.status === "running") {
      paintIngest(state);
      setRefreshLabel(`Loading ${Math.round((state.progress || 0) * 100)}%…`, true);
      continue;
    }
    showIngest(false);
    // Say what was just loaded, not what was loaded before it. `setRefreshLabel` re-applies the
    // freshness date, and until now that date was only updated later by `loadTabs()` — so the
    // button kept yesterday's date after a refresh that had plainly worked, and only corrected
    // itself on the next full page load. The refresh knows the answer; it should not wait to be
    // told by a re-render.
    if (w.to) lastAsOf = w.to;
    setRefreshLabel("Refresh data");

    // Outcomes go to a toast, not a banner. The old path prepended into #tabBody, which is the
    // wrong place for feedback about a header button: it can sit below the fold, and on a
    // partial refresh the loadTabs() below wiped it before anyone read it. toast() floats and
    // survives the re-render — it exists because a failed export "looked exactly like nothing
    // happening at all".
    if (state.status === "failed") {
      toast(`Refresh failed. ${state.detail || state.error || "No reason was reported."}`, "err");
      return;
    }
    // A partial ingest loaded something, so the new data is worth showing — but saying nothing
    // would leave a hole in the numbers that nobody knows about.
    if (state.status === "partial") {
      toast(`Some data could not be loaded. ${state.detail || "See the server log."}`, "err");
    }
    // Nothing came back and nothing was replaced. Distinct from a failure — no request errored —
    // and distinct from success, because the numbers on screen are the ones that were already
    // there. Reporting it as "ready" is what used to make an empty refresh look like a working
    // one, right up until someone noticed the figures had not moved in a week.
    if (state.status === "empty") {
      toast(
        state.detail ||
          "The refresh returned no rows, so nothing changed. The existing data is unchanged.",
        "warn"
      );
    }
    // A quick load is a deliberate trade, so it says what it traded. Someone who opens Tags or
    // Commitments after one and finds them empty should already know why, rather than
    // discovering it and concluding the refresh half-failed.
    if (state.mode === "quick" && (state.status === "ready" || state.status === "partial")) {
      toast(
        `Loaded ${(state.rows || 0).toLocaleString()} rows. A quick refresh carries ` +
          "cost by day, resource, service and region — quantities, unit prices, reservation " +
          "benefits and tags need FOCUS or a full-detail refresh.",
        "warn"
      );
    }
    // The archive is a side effect of the refresh, so it reports separately. A silent failure
    // here is the one that matters: the dashboard looks correct, and the daily copy that was
    // supposed to preserve these numbers was never written.
    const arch = state.archive;
    if (arch?.error) {
      toast(`Data loaded, but the daily archive failed. ${arch.error}`, "err");
    } else if (arch?.name) {
      toast(`Archived ${(arch.rows || 0).toLocaleString()} rows to ${arch.name}.`);
    }
    clearCache();
    refreshHeader();
    await loadTabs();
    return;
  }
  setRefreshLabel("Refresh data");
  showIngest(false);
  // Giving up silently is how this looked like "the button does nothing": the spinner simply
  // stopped and the numbers were unchanged, with no way to tell whether it had worked.
  toast(
    "Still loading after 40 minutes, so this page stopped watching. The refresh is probably " +
      "still running on the server — reload in a few minutes to see the result.",
    "err"
  );
}

// ------------------------------------------------------------- the progress strip
// A refresh is minutes of waiting on Azure. A control that merely looks busy is what makes
// someone press it a second time, or reload the page mid-ingest and assume it failed — so the
// wait gets a bar that actually moves, a clock, and a sentence saying it is safe to walk away.
let ingestStarted = 0;
let ingestTick = null;

function showIngest(on) {
  const host = $("ingest");
  if (!host) return;
  host.hidden = !on;
  if (on) {
    ingestStarted = ingestStarted || Date.now();
    // The clock runs on its own timer rather than on the poll, so it counts smoothly between
    // the three-second polls instead of jumping.
    ingestTick = ingestTick || setInterval(paintElapsed, 1000);
  } else {
    clearInterval(ingestTick);
    ingestTick = null;
    ingestStarted = 0;
    setIngestFill(0);
  }
}

function paintElapsed() {
  const el = $("ingestElapsed");
  if (!el || !ingestStarted) return;
  const s = Math.floor((Date.now() - ingestStarted) / 1000);
  el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** The bar only ever moves forwards. A phase count that dips must not drag it backwards. */
let ingestSeen = 0;
function setIngestFill(fraction) {
  const fill = $("ingestFill");
  if (!fill) return;
  if (fraction === 0) ingestSeen = 0;
  ingestSeen = Math.max(ingestSeen, fraction);
  fill.style.width = `${Math.round(ingestSeen * 100)}%`;
  const pct = $("ingestPct");
  if (pct) pct.textContent = `${Math.round(ingestSeen * 100)}%`;
  const bar = fill.parentElement;
  if (bar) {
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-valuenow", String(Math.round(ingestSeen * 100)));
    bar.setAttribute("aria-valuemin", "0");
    bar.setAttribute("aria-valuemax", "100");
  }
}

function paintIngest(state) {
  showIngest(true);
  setIngestFill(state.progress || 0);

  const done = state.done ?? 0;
  const total = state.total ?? 0;
  const gen = state.generating || 0;
  const sub = state.submitting || 0;
  const load = state.loading || 0;

  // Name the wait rather than the step number. "2 of 3" says nothing about why nothing has
  // happened for four minutes; "Azure is building 2 reports" says exactly why.
  let what;
  if (state.fetching) {
    // An export is a couple of very large blobs, and the whole wait is one download. Saying
    // which file, and how big, is the difference between a slow refresh and an apparently
    // dead one — the bar cannot move within a single fetch, so the words have to.
    const mb = state.fetching_bytes ? ` (${(state.fetching_bytes / 1048576).toFixed(1)} MB)` : "";
    const name = state.fetching_name ? ` ${state.fetching_name}` : "";
    what = `Downloading file ${state.fetching} of ${state.total || "?"}${name}${mb}…`;
  } else if (state.writing) {
    // The longest single step, and the one that used to look like a hang: tens of thousands of
    // rows going into DuckDB. Named so the wait is legible rather than a bar stuck at 90%.
    what = `Writing ${state.writing.toLocaleString()} rows into the warehouse…`;
  } else if (load) what = `Loading ${load} report${load === 1 ? "" : "s"} into the warehouse…`;
  else if (gen) what = `Azure is building ${gen} report${gen === 1 ? "" : "s"}…`;
  else if (sub) what = `Queuing ${sub} request${sub === 1 ? "" : "s"} with Azure…`;
  else if (done && done === total) what = "Finishing up…";
  else what = "Starting…";

  // Named as the menu names it, and with the route it took. "API data" was this option's old
  // name, and leaving it here made the *slow* full-detail path read like the fast one someone
  // had meant to pick — a nine-period, six-minute run labelled as if it were the live query.
  // The route is what actually predicts the wait, so it is what the strip says.
  const metric = { FocusCost: "FOCUS", AmortizedCost: "Amortized", ActualCost: "Actual" }[
    state.metric
  ];
  const route = state.mode === "quick"
    ? "quick"
    : state.metric && state.metric !== "FocusCost" && state.unit === "period"
      ? "full detail"
      : "";
  const named = metric ? `${metric}${route ? ` · ${route}` : ""}: ` : "";
  // The two routes count different things. Reading an export counts blobs; the API route counts
  // the periods it asked Azure to build. Calling both "periods" put "0/2 periods loaded" over a
  // FOCUS run that has no periods in it.
  const unit = state.unit || "period";
  const scope = total && !state.fetching
    ? ` · ${done}/${total} ${unit}${total === 1 ? "" : "s"} loaded`
    : "";
  const el = $("ingestWhat");
  if (el) el.textContent = `${named}${what}${scope}`;

  // The note is the expectation being set, so it has to match the route. "A few minutes" over
  // a quick refresh is wrong in one direction and over a full-detail run — nine periods Azure
  // builds one at a time — it is wrong in the other. Both were the same sentence.
  const note = $("ingestNote");
  if (note) {
    note.textContent = state.mode === "quick"
      ? "A live query, capped at about a minute. Whatever comes back in that time is kept."
      : state.unit === "period"
        ? "Azure builds these reports on request, which usually takes ten minutes or more. "
          + "You can keep using the dashboard — the numbers update on their own when it finishes."
        : "Reading an export Azure has already written. You can keep using the dashboard — "
          + "the numbers update on their own when it finishes.";
  }
}

// -------------------------------------------------------------- report builder
// What the Export data tab has selected. Kept outside the render so switching away and back does
// not silently discard a selection somebody has just spent a minute making.
let reportChoice = readStore("costReport", null);

async function renderReport(token) {
  // The raw-data export below needs to know what the warehouse holds and which snapshots were
  // archived; both are fetched alongside the builder's options rather than after them, so the
  // tab still costs one round trip.
  const [opts, arch, wh] = await Promise.all([
    cached(key("reportOptions"), () => get(`/api/report/options${qs()}`, { timeout: 30000 })),
    get("/api/archive?limit=200", { timeout: 30000 }).catch(() => ({})),
    get("/api/warehouse", { timeout: 20000 }).catch(() => ({})),
  ]);
  if (token !== loadToken) return;

  const areaIds = opts.areas.map((a) => a.id);
  // Default to everything from the warehouse and nothing live: live datasets call Azure and
  // turn a one-second export into a slow one, so they should be a deliberate choice.
  const fresh = () => ({
    v: 2,
    summary: true,
    sections: areaIds,
    blocks: [...opts.blocks],
    live: [],
  });
  const c = reportChoice || fresh();
  // An area that has since dropped out of scope must not linger in the selection.
  c.sections = c.sections.filter((id) => areaIds.includes(id));
  // Before v2 a mislabelled "Select all" could clear the whole list on the first click and
  // persist it, so every later export came out near-empty. Repair that once; after the marker
  // is written a deliberately narrow selection is left alone.
  if (c.v !== 2) {
    if (areaIds.length && !c.sections.length && !c.live.length) {
      c.sections = areaIds;
      if (!c.blocks.length) c.blocks = [...opts.blocks];
    }
    c.v = 2;
  }

  const BLOCK_LABEL = {
    trend: "Daily trend",
    services: "By service",
    regions: "By region",
    resources: "Top resources",
  };

  const checkbox = (group, id, label, sub, checked) => `
    <label class="opt">
      <input type="checkbox" data-group="${group}" value="${esc(id)}" ${checked ? "checked" : ""} />
      <span class="opt-text"><span class="opt-label">${esc(label)}</span>
      ${sub ? `<span class="opt-sub">${esc(sub)}</span>` : ""}</span>
    </label>`;

  $("tabBody").innerHTML = `
    <p class="muted note report-lede">Choose what to include, then download it in any format.
      Everything respects the period and subscriptions selected at the top of the page.</p>

    <div class="grid-2">
      <section class="card">
        <h3>Cost areas <button class="link" data-all="sections">Select all</button></h3>
        <p class="muted note">From the local warehouse — fast.</p>
        <div class="opt-list">
          ${checkbox("summary", "summary", "Summary",
                     "Totals and the by-area breakdown", c.summary)}
          ${opts.areas
            .map((a) =>
              checkbox("sections", a.id, a.label, money(a.cost, opts.currency),
                       c.sections.includes(a.id))
            )
            .join("")}
        </div>
      </section>

      <section class="card">
        <h3>Detail per area</h3>
        <p class="muted note">Applies to every area you selected.</p>
        <div class="opt-list">
          ${opts.blocks
            .map((b) => checkbox("blocks", b, BLOCK_LABEL[b] || b, "", c.blocks.includes(b)))
            .join("")}
        </div>

        <h3 style="margin-top:18px">Live findings
          <button class="link" data-all="live">Select all</button></h3>
        <p class="muted note">These query Azure directly, so the export takes longer.</p>
        <div class="opt-list">
          ${opts.live
            .map((l) => checkbox("live", l.id, l.label, "", c.live.includes(l.id)))
            .join("")}
        </div>
      </section>
    </div>

    <div class="build-bar">
      <!-- The format lives on the download control rather than in a panel of its own. A whole
           section repeating the choices already in the header Export menu was duplication;
           attaching them here keeps the choice where the action is. The items are real links
           for the same reason the header's are: a scripted download is an *automatic* one, and
           managed browsers block those silently. -->
      <div class="menu-wrap" id="buildWrap">
        <button class="primary" id="buildBtn" aria-haspopup="menu" aria-expanded="false"
                aria-controls="buildMenu">Download report<span class="caret" aria-hidden="true">▾</span></button>
        <div class="drop" id="buildMenu" role="menu" hidden>
          <div class="head">Download this selection</div>
          ${opts.formats
            .map(
              (f) => `<a data-fmt="${esc(f.id)}" role="menuitem" href="#">${esc(f.label)}</a>`
            )
            .join("")}
        </div>
      </div>
      <span class="muted" id="buildNote"></span>
    </div>

    <!-- The second half of this tab, and it has to look like one. The builder above produces a
         *report* — chosen areas, chosen detail, formatted to be read. This produces the raw
         rows behind it: a different question, a different audience, and a different set of
         controls that must not read as more options for the Download report button sitting
         directly above them. -->
    <hr class="tab-split">

    <section class="card section-lead">
      <div class="tagpick-head">
        <h3>Custom export the cost data</h3>
        <span class="muted note">
          <button type="button" class="linkish" id="xAll">All</button> ·
          <button type="button" class="linkish" id="xNone">None</button>
        </span>
      </div>
      <p class="muted note">The daily archive covers the whole estate, because that is what makes
        two days comparable. This is the other question — just the subscriptions you tick here —
        written as its own labelled file so it cannot overwrite that archive. Chosen
        independently of the picker in the header, since what you want to export is rarely what
        you happen to be looking at.</p>

      <div class="subpick" id="xSubs">
        ${allSubscriptions().map((s) => `<label class="subpick-item">
          <input type="checkbox" value="${esc(s.id)}" ${scopeIds().length === 0
            || scopeIds().includes(s.id) ? "checked" : ""}>
          <span class="subpick-name">${esc(s.name || s.id)}</span>
        </label>`).join("")}
      </div>

      <div class="budget-grid">
        <label class="bfield"><span>Cost period from</span>
          <input id="xFrom" type="date" value="${esc(wh.from || "")}"
                 min="${esc(wh.from || "")}" max="${esc(wh.to || "")}"></label>
        <label class="bfield"><span>to</span>
          <input id="xTo" type="date" value="${esc(wh.to || "")}"
                 min="${esc(wh.from || "")}" max="${esc(wh.to || "")}"></label>
        <label class="bfield"><span>Data as read on</span>
          <select id="xSource">
            <option value="">Today — the live warehouse</option>
            ${snapshotOptions(arch)}
          </select>
        </label>
        <label class="bfield"><span>Format</span>
          <select id="xFmt">
            <option value="csv">CSV — same shape a FOCUS export writes</option>
            <option value="parquet">Parquet — smaller, for tooling</option>
          </select>
        </label>
        <label class="bfield"><span>Label</span>
          <input id="xLabel" type="text" maxlength="40" placeholder="e.g. finance-review"
                 autocomplete="off" spellcheck="false"></label>
      </div>

      <p class="muted note"><strong>Two different dates.</strong> The period above chooses which
        days of <em>spend</em> are in the file. “Data as read on” chooses which
        <em>snapshot</em> they come from — Azure restates cost data for days afterwards, so
        August as read on the 27th is not August as read on the 28th. Leave it on today unless
        you are reproducing a figure someone reported earlier.</p>

      <p class="muted note">The warehouse currently holds
        ${wh.from ? `${esc(wh.from)} → ${esc(wh.to)}` : "nothing"}. CSV carries the same columns
        in the same order as a FOCUS export, so the file is interchangeable with one Azure
        produced.</p>
      <div class="build-bar">
        <a class="btn primary" id="xDownload" role="button">Download</a>
        <button class="ghost" id="xExport">Save to storage</button>
        <span class="muted" id="xNote"></span>
      </div>
    </section>`;

  const save = () => {
    reportChoice = c;
    writeStore("costReport", c);
    updateNote();
    syncAll();
  };

  const allOf = (g) => (g === "sections" ? areaIds : opts.live.map((l) => l.id));

  // The label has to describe what the next click does, otherwise the first click on a
  // fully-selected list silently clears it.
  const syncAll = () => {
    for (const b of $("tabBody").querySelectorAll("[data-all]")) {
      const all = allOf(b.dataset.all);
      b.disabled = !all.length;
      b.textContent = all.length && c[b.dataset.all].length === all.length
        ? "Clear all" : "Select all";
    }
  };

  const updateNote = () => {
    const parts = [];
    if (c.summary) parts.push("summary");
    parts.push(`${c.sections.length} area${c.sections.length === 1 ? "" : "s"}`);
    if (c.live.length) parts.push(`${c.live.length} live`);
    const nothing = !c.summary && !c.sections.length && !c.live.length;
    const thin = c.summary && !c.sections.length && !c.live.length;
    $("buildNote").textContent = nothing
      ? "Nothing selected."
      : thin
        ? "Summary only — tick some cost areas for a fuller report."
        : parts.join(" · ") + (c.live.length ? " — live findings take up to a minute." : "");
    $("buildNote").classList.toggle("warn", nothing || thin);
    // Links have no disabled state, so an empty selection has to lose its destination
    // instead — otherwise it would download a file with nothing in it.
    const btn = $("buildBtn");
    btn.disabled = nothing;
    for (const a of $("buildMenu").querySelectorAll("a[data-fmt]")) {
      if (nothing) a.removeAttribute("href");
      else wireDownloadLink(a, reportUrl(c, a.dataset.fmt));
    }
  };

  // `input[data-group]`, not every checkbox on the tab. The raw-data export below owns a
  // subscription list of its own, and those boxes carry no group — claiming them here would
  // both overwrite their handler and index the selection object with `undefined`.
  for (const input of $("tabBody").querySelectorAll("input[data-group]")) {
    input.onchange = () => {
      const g = input.dataset.group;
      if (g === "summary") c.summary = input.checked;
      else if (input.checked) c[g] = [...new Set([...c[g], input.value])];
      else c[g] = c[g].filter((v) => v !== input.value);
      save();
    };
  }
  for (const btn of $("tabBody").querySelectorAll("[data-all]")) {
    btn.onclick = () => {
      const g = btn.dataset.all;
      const all = allOf(g);
      const full = c[g].length === all.length;
      c[g] = full ? [] : [...all];
      for (const i of $("tabBody").querySelectorAll(`input[data-group="${g}"]`)) {
        i.checked = !full;
      }
      save();
    };
  }
  // The download control is a menu of format links. Opening it is the only scripted part;
  // the download itself is the browser following whichever link was clicked.
  const menu = $("buildMenu");
  const trigger = $("buildBtn");
  const openMenu = (show) => {
    if (show) {
      // Open downward when there is room and upward when there is not. The bar is the last
      // thing on the tab, so a menu that always dropped down would be clipped by the scroll
      // container; one that always went up would cover the choices just made.
      const below = window.innerHeight - trigger.getBoundingClientRect().bottom;
      menu.classList.toggle("up", below < 240);
    }
    menu.hidden = !show;
    trigger.setAttribute("aria-expanded", String(show));
  };
  trigger.onclick = (e) => {
    e.stopPropagation();
    openMenu(menu.hidden);
  };
  menu.onclick = (e) => {
    if (e.target.closest("a[data-fmt]")) openMenu(false);
  };
  document.addEventListener("click", (e) => {
    if (!$("buildWrap")?.contains(e.target)) openMenu(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") openMenu(false);
  });

  updateNote();
  syncAll();
  save();
  wireSelectionExport();
}

/** The link that downloads a report: everything the builder needs, as query parameters. */
function reportUrl(choice, format) {
  const p = new URLSearchParams({ days: String(days) });
  const scope = scopeIds();
  if (scope.length) p.set("scope", scope.join(","));
  p.set("summary", choice.summary ? "1" : "0");
  // An absent `sections` means "every area" to the server, so an explicit empty value is
  // needed to mean "none" — hence always sending it.
  p.set("sections", (choice.sections || []).join(","));
  p.set("blocks", (choice.blocks || []).join(","));
  p.set("live", (choice.live || []).join(","));
  // The file has to match the screen it was exported from.
  const cur = readStore("costCurrency", "");
  if (cur) p.set("currency", cur);
  return `/api/report/${format}?${p}`;
}

/**
 * The download itself is the browser following a link the person clicked — there is no
 * scripted click anywhere in the path.
 *
 * That distinction matters more than it looks. A download started from JavaScript is an
 * *automatic* download as far as the browser is concerned, and automatic downloads are
 * exactly what download-restriction policy, SmartScreen and the per-site "automatic
 * downloads" permission are there to block. When they block one they do it quietly: no file,
 * no dialog, no console error. A managed browser can therefore refuse a download that works
 * perfectly everywhere else, which is indistinguishable from a broken button.
 *
 * Following a real link is a user gesture the browser trusts, so all of that goes away. The
 * job of the code here is only to keep the link's href current.
 */
function wireDownloadLink(a, url) {
  a.href = url;
  a.removeAttribute("target");
  // No `download` attribute: the filename comes from Content-Disposition, and leaving it off
  // means an expired session redirects to the sign-in page (which the browser can show)
  // rather than saving the redirect as a file.
  a.onclick = () => {
    toast("Your download is starting. Check your browser's downloads if you don't see it.");
    return true;
  };
}

// -------------------------------------------------------------------- export
/** Download the current view as a file. The server builds it from the same data and scope. */
function wireExport() {
  const wrap = $("exportWrap");
  const menu = $("exportMenu");
  const btn = $("exportBtn");
  if (!wrap) return;

  const open = (show) => {
    menu.hidden = !show;
    btn.setAttribute("aria-expanded", String(show));
  };
  btn.onclick = (e) => {
    e.stopPropagation();
    open(menu.hidden);
  };
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) open(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") open(false);
  });

  for (const item of menu.querySelectorAll("[data-fmt]")) {
    const fmt = item.dataset.fmt;
    if (fmt === "print") {
      item.onclick = () => { open(false); window.print(); };
      continue;
    }
    if (fmt === "custom") {
      item.onclick = () => { open(false); selectTab("report"); };
      continue;
    }
    // A real link: the browser downloads it because the person clicked it, not because
    // script asked it to. The menu closes on the way through.
    item.addEventListener("click", () => open(false));
  }
  for (const item of menu.querySelectorAll("[data-raw]")) {
    item.addEventListener("click", () => open(false));
  }
  refreshExportLinks();
}

/**
 * Point the header menu's links at the current period, scope and display currency.
 *
 * Called whenever any of them changes, because the href has to be right *before* the click —
 * there is no handler that could fix it up afterwards.
 */
function refreshExportLinks() {
  const menu = $("exportMenu");
  if (!menu) return;
  const p = new URLSearchParams({ days: String(days) });
  const scope = scopeIds();
  if (scope.length) p.set("scope", scope.join(","));
  const cur = readStore("costCurrency", "");
  if (cur) p.set("currency", cur);
  for (const item of menu.querySelectorAll("a[data-fmt]")) {
    wireDownloadLink(item, `/api/report/${item.dataset.fmt}?${p}`);
  }
  // The raw rows are not a report format, so they have their own endpoint — but they take the
  // same period and scope, which is the whole point of offering them here. They are deliberately
  // *not* converted: raw rows are the billed record, and rewriting them would make the export
  // disagree with the invoice it is meant to reconcile against.
  const raw = new URLSearchParams(p);
  raw.delete("currency");
  for (const item of menu.querySelectorAll("a[data-raw]")) {
    wireDownloadLink(item, `/api/costs/raw.csv?${raw}`);
  }
}

// --------------------------------------------------------------------- wiring
/** Shared shape for the live tabs: fetch, guard against a stale response, show the reason. */
async function live(token, url, timeout = 180000) {
  let d;
  try {
    d = await get(url, { timeout });
  } catch (err) {
    if (token !== loadToken) return null;
    fail(err?.message || "Azure did not answer in time.");
    return null;
  }
  return token === loadToken ? d : null;
}

async function renderRightsizing(token) {
  const d = await live(token, `/api/dashboard/rightsizing${qs()}`);
  if (!d) return;

  const vms = d.vms || [];
  const cur = d.currency;
  const idle = vms.filter((v) => v.cpu_avg != null && v.cpu_avg < (d.cpu_threshold ?? 5));
  const worst = Math.max(...vms.map((v) => v.cost || 0), 0);

  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: "Virtual machines", value: String(d.count ?? vms.length) },
      { label: "Running", value: String(d.running ?? "—") },
      {
        label: `Idle (under ${d.cpu_threshold ?? 5}% CPU)`,
        value: String(d.idle_count ?? idle.length),
        cls: (d.idle_count ?? idle.length) > 0 ? "up" : "",
      },
      {
        label: `Cost of idle, ${d.period_days}d`,
        value: d.idle_cost == null ? "—" : esc(money(d.idle_cost, cur, 2)),
      },
      { label: "Stopped", value: String(d.stopped_count ?? "—") },
    ])}
    <p class="muted note">CPU average and peak from Azure Monitor over the last
      ${d.period_days} days, joined to what each machine actually cost. A low average with a high
      peak is bursty, not idle — resize those, don't stop them.</p>
    ${table(
      "Virtual machines by cost",
      vms,
      [
        { label: "Name", cell: (r) => `${esc(r.name || "—")} ${bar(r.cost || 0, worst)}` },
        { label: "Size", cell: (r) => esc(r.size || r.vm_size || "—") },
        { label: "Power state", cell: (r) => esc(r.power_state || r.state || "—") },
        {
          label: "CPU avg",
          num: true,
          cell: (r) => (r.cpu_avg == null ? "—" : `${Number(r.cpu_avg).toFixed(1)}%`),
        },
        {
          label: "CPU peak",
          num: true,
          cell: (r) => (r.cpu_max == null ? "—" : `${Number(r.cpu_max).toFixed(1)}%`),
        },
        {
          label: `Cost, ${d.period_days}d`,
          num: true,
          cell: (r) => (r.cost == null ? "—" : esc(money(r.cost, cur, 2))),
        },
      ],
      d.note || null
    )}`;
}

async function renderAdvisor(token) {
  const d = await live(token, `/api/dashboard/advisor${qs()}`, 90000);
  if (!d) return;

  const recs = d.recommendations || [];
  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: `${esc(d.category)} recommendations`, value: String(d.count ?? recs.length) },
      {
        label: "Estimated annual saving",
        value:
          d.estimated_annual_savings == null
            ? "not estimated"
            : esc(money(d.estimated_annual_savings, d.currency, 2)),
        cls: d.estimated_annual_savings ? "down" : "",
      },
    ])}
    <p class="muted note">Microsoft's own recommendations and its own savings estimates — useful
      precisely because they aren't ours. ${esc(d.note || "")}</p>
    ${table("Recommendations", recs, [
      { label: "Impact", cell: (r) => esc(r.impact || "—") },
      { label: "Recommendation", cell: (r) => esc(r.problem || r.solution || "—") },
      { label: "Resource", cell: (r) => esc(r.resource || "—") },
      {
        label: "Annual saving",
        num: true,
        cell: (r) => (r.annual_savings == null ? "—" : esc(money(r.annual_savings, r.currency, 2))),
      },
    ])}`;
}

/**
 * Governance: configuration that is wrong, rather than spend that is wasted.
 *
 * Two independent checks share the tab because they answer the same kind of question. Either
 * can fail without losing the other, so each half renders its own error rather than the tab
 * showing nothing.
 */
async function renderGovernance(token) {
  const d = await live(token, `/api/dashboard/governance${qs()}`, 180000);
  if (!d) return;

  const t = d.tagging || {};
  const a = d.accelerated_networking || {};
  const cur = t.currency;
  const maxUntagged = (t.by_type || []).reduce((m, x) => Math.max(m, x.untagged), 0);

  const section = (title, body) => `<section class="card"><h3>${esc(title)}</h3>${body}</section>`;
  const problem = (msg) =>
    `<p class="muted empty">${esc(msg)}</p>`;

  const tagBody = t.error
    ? section("Tag coverage", problem(t.error))
    : `
    ${kpiRow([
      {
        label: "Tag coverage",
        value: t.coverage_pct == null ? "—" : `${t.coverage_pct}%`,
        cls: t.coverage_pct != null && t.coverage_pct < 60 ? "up" : "",
        sub: `${t.scanned?.tagged ?? 0} of ${t.scanned?.resources ?? 0} taggable`,
      },
      { label: "Untagged resources", value: String(t.untagged_total ?? 0) },
      {
        label: "Untagged spend",
        value: moneyHtml(t.untagged_cost, cur, 2),
        sub: `over the last ${t.days ?? 30} days`,
      },
      {
        label: "Without accelerated networking",
        value: String((a.eligible || []).length),
        sub: a.error ? "check failed" : `${a.enabled ?? 0} already on`,
      },
    ])}

    <div id="tagChart" class="card chart-card"></div>

    <div class="grid-2">
      ${table(
        "Untagged by resource type",
        t.by_type || [],
        [
          { label: "Type", cell: (r) => `${esc(r.type)} ${bar(r.untagged, maxUntagged)}` },
          {
            label: "Coverage",
            num: true,
            value: (r) => r.coverage_pct ?? 0,
            cell: (r) => (r.coverage_pct == null ? "—" : `${r.coverage_pct}%`),
          },
          {
            label: "Untagged",
            num: true,
            value: (r) => r.untagged,
            cell: (r) => String(r.untagged),
            total: (rows) => String(rows.reduce((n, r) => n + r.untagged, 0)),
          },
        ],
        null
      )}
      ${table(
        "Tag keys already in use",
        t.tag_keys || [],
        [
          { label: "Key", cell: (r) => esc(r.key) },
          {
            label: "Resources",
            num: true,
            value: (r) => r.count,
            cell: (r) => String(r.count),
          },
        ],
        "The convention your estate already has — worth following rather than replacing."
      )}
    </div>

    ${table(
      "Untagged resources that cost money",
      t.resources || [],
      [
        { label: "Resource", cell: (r) => esc(r.name) },
        { label: "Type", cell: (r) => esc(String(r.type).split("/").pop()) },
        { label: "Resource group", cell: (r) => esc(r.resourceGroup || "—") },
        { label: "Region", cell: (r) => esc(r.location || "—") },
        {
          label: `Cost, ${t.days ?? 30}d`,
          num: true,
          value: (r) => r.cost || 0,
          cell: (r) => moneyHtml(r.cost, r.currency || cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + (r.cost || 0), 0), cur, 2),
        },
      ],
      "Billed resources with no tags — these are the ones nobody can charge back."
    )}
    ${t.note ? `<p class="muted note">${esc(t.note)}</p>` : ""}`;

  const accelBody = a.error
    ? section("Accelerated networking", problem(a.error))
    : (a.eligible || []).length
      ? `${table(
          "VMs without accelerated networking",
          a.eligible,
          [
            { label: "VM", cell: (r) => esc(r.vmName || "—") },
            { label: "Size", cell: (r) => esc(r.vmSize || "—") },
            {
              label: "Power state",
              cell: (r) =>
                /deallocated|stopped/i.test(r.powerState || "")
                  ? `<span class="pill">${esc(r.powerState)}</span>`
                  : esc(r.powerState || "—"),
            },
            { label: "Resource group", cell: (r) => esc(r.resourceGroup || "—") },
            { label: "Region", cell: (r) => esc(r.location || "—") },
          ],
          a.note
        )}`
      : section(
          "Accelerated networking",
          problem(
            a.scanned?.nics
              ? `All ${a.scanned.nics} attached network interface(s) either have accelerated networking on, or sit on a size that cannot support it.`
              : "No virtual machines with attached network interfaces in scope."
          )
        );

  $("tabBody").innerHTML = tagBody + accelBody;

  // Coverage reads better as a shape than a column of percentages: the eye finds the worst
  // offender before it reads a single number.
  const worst = (t.by_type || []).slice(0, 10);
  if (worst.length > 1 && !t.error) {
    drawChart($("tagChart"), {
      type: "hbar",
      title: "Untagged resources by type",
      labels: worst.map((x) => x.type),
      datasets: [{ label: "Untagged", data: worst.map((x) => x.untagged) }],
    });
  } else {
    $("tagChart")?.remove();
  }
}

/**
 * Rate optimisation: paying less for the same resources.
 *
 * Commitments and usage recommendations are kept apart on purpose. Buying a reservation is a
 * budget decision with a multi-year tail; deleting an unattached disk is a right-click. A
 * single merged list makes both harder to act on.
 */
async function renderRates(token) {
  const d = await live(token, `/api/dashboard/rates${qs()}`, 120000);
  if (!d) return;

  const cur = d.currency;
  const commitments = d.commitments || [];
  const usage = d.usage || [];

  const impact = (r) =>
    r.impact === "High"
      ? '<span class="pill bad">high</span>'
      : r.impact === "Medium"
        ? '<span class="pill warn">medium</span>'
        : `<span class="pill">${esc(r.impact || "low")}</span>`;

  $("tabBody").innerHTML = `
    ${kpiRow([
      {
        label: "Best commitment saving",
        value: moneyHtml(d.best_commitment_saving, cur, 2),
        sub: d.best_commitment_term ? `over a ${esc(d.best_commitment_term)} term` : "none offered",
      },
      {
        label: "Usage savings available",
        value: moneyHtml(d.usage_saving, cur, 2),
        sub: `${usage.length} recommendation${usage.length === 1 ? "" : "s"}`,
      },
      {
        label: "Subscriptions scanned",
        value: String(d.scanned?.subscriptions ?? 0),
        sub: `${d.scanned?.recommendations ?? 0} Advisor findings`,
      },
    ])}

    ${
      commitments.length
        ? table(
            "Reservations and savings plans",
            commitments,
            [
              { label: "Recommendation", cell: (r) => esc(r.problem || "—") },
              { label: "Term", cell: (r) => esc(r.term || "—") },
              { label: "Based on", cell: (r) => esc(r.lookback || "—") },
              { label: "Impact", cell: impact, sort: false },
              {
                label: "Saving",
                num: true,
                value: (r) => r.savings || 0,
                cell: (r) => moneyHtml(r.savings, r.currency || cur, 2),
              },
            ],
            "Terms are alternatives to choose between, not savings to add together — the same workload appears once per term."
          )
        : `<section class="card"><h3>Reservations and savings plans</h3>
           <p class="muted empty">Azure is not currently recommending a commitment for this
           scope. That usually means the workload is too bursty or too new for a steady-state
           discount to pay off — Advisor looks at the last 30 days.</p></section>`
    }

    ${
      usage.length
        ? table(
            "Other cost recommendations",
            usage,
            [
              { label: "Recommendation", cell: (r) => esc(r.problem || "—") },
              { label: "Resource", cell: (r) => esc(r.name || "—") },
              { label: "Type", cell: (r) => esc(r.type || "—") },
              { label: "Impact", cell: impact, sort: false },
              {
                label: "Saving",
                num: true,
                value: (r) => r.savings || 0,
                cell: (r) =>
                  r.savings == null ? "—" : moneyHtml(r.savings, r.currency || cur, 2),
                total: (rows) =>
                  moneyHtml(rows.reduce((n, r) => n + (r.savings || 0), 0), cur, 2),
              },
            ],
            null
          )
        : ""
    }

    <section class="card">
      <h3>Existing commitments</h3>
      <p class="muted empty">${esc(d.utilisation_note || "")}</p>
    </section>

    <p class="muted note">${esc(d.note || "")}</p>`;
}

/**
 * Retirements and required migrations, from Azure Service Health.
 *
 * The sort is by deadline rather than by severity, because a date you can miss is more
 * actionable than a label. Overdue items lead — Azure leaves advisories active past their
 * deadline, and those are the ones already costing someone something.
 */
async function renderHealth(token) {
  const d = await live(token, `/api/dashboard/health${qs()}`, 120000);
  if (!d) return;

  const all = d.advisories || [];
  const c = d.counts || {};

  if (!all.length) {
    $("tabBody").innerHTML = `<section class="card"><h3>Retirements and advisories</h3>
      <p class="muted empty">Azure has no active advisories for the subscriptions in scope.</p>
      </section>`;
    return;
  }

  const when = (a) => {
    if (a.days_left == null) return '<span class="muted">no deadline</span>';
    if (a.overdue) return `<span class="delta up">${Math.abs(a.days_left)}d overdue</span>`;
    if (a.days_left <= 30) return `<span class="delta up">${a.days_left}d</span>`;
    if (a.days_left <= 90) return `<span class="delta">${a.days_left}d</span>`;
    return `<span class="muted">${a.days_left}d</span>`;
  };

  const kind = (a) =>
    a.retirement
      ? '<span class="pill bad">retirement</span>'
      : `<span class="pill${a.level === "Warning" ? " warn" : ""}">${esc(
          (a.level || "advisory").toLowerCase()
        )}</span>`;

  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: "Active advisories", value: String(c.active ?? 0) },
      {
        label: "Retirements",
        value: String(c.retirements ?? 0),
        cls: (c.retirements ?? 0) > 0 ? "up" : "",
        sub: "service or feature going away",
      },
      {
        label: "Due within 90 days",
        value: String(c.due_90_days ?? 0),
        cls: (c.due_90_days ?? 0) > 0 ? "up" : "",
        sub: d.next_deadline ? `next: ${esc(d.next_deadline)}` : "no dated deadline",
      },
      {
        label: "Past deadline",
        value: String(c.overdue ?? 0),
        cls: (c.overdue ?? 0) > 0 ? "up" : "",
        sub: "still listed as active",
      },
    ])}

    ${table(
      "What Azure is changing",
      all,
      [
        { label: "Advisory", cell: (a) => esc(a.headline || "—") },
        { label: "Kind", cell: kind, sort: false },
        { label: "Due", cell: (a) => esc(a.due || "—"), value: (a) => a.due || "" },
        {
          label: "Time left",
          num: true,
          // Sorts by urgency: overdue first, then soonest. Undated go last rather than
          // sorting as zero, which would put them above things that are genuinely due.
          value: (a) => (a.days_left == null ? 99999 : a.days_left),
          cell: when,
        },
        {
          label: "Your resources",
          num: true,
          value: (a) => a.resource_count,
          cell: (a) => (a.resource_count ? String(a.resource_count) : "—"),
        },
        { label: "Tracking", cell: (a) => esc(a.tracking || "—") },
      ],
      "Sorted by deadline. Azure names impacted resources for some advisory types and not others."
    )}

    ${
      // The detail nobody wants in a table cell, but everybody wants for the two that matter.
      all
        .filter((a) => a.retirement && a.days_left != null && a.days_left <= 120)
        .slice(0, 5)
        .map(
          (a) => `<section class="card">
            <h3>${esc(a.tracking)} — ${esc(a.due || "no deadline")}</h3>
            <p class="muted note"><strong>${esc(a.headline)}</strong></p>
            ${a.summary ? `<p class="muted note">${esc(a.summary)}</p>` : ""}
            ${a.actions ? `<p class="muted note"><strong>Recommended:</strong> ${esc(a.actions)}</p>` : ""}
            ${
              a.resources.length
                ? `<p class="muted note"><strong>Affects:</strong> ${a.resources
                    .slice(0, 12)
                    .map((r) => esc(r.name))
                    .join(", ")}${a.resource_count > 12 ? ` and ${a.resource_count - 12} more` : ""}</p>`
                : ""
            }
          </section>`
        )
        .join("")
    }

    <p class="muted note">${esc(d.note || "")}</p>`;
}

/**
 * Commitments: reservations, savings plans and Spot.
 *
 * A warehouse query, so it is instant — but what it can *say* depends on how the warehouse was
 * built. An actual-cost ingest bills a reservation as one lump on the day it was bought and
 * carries no benefit attribution, so "what did the commitment save" has no honest answer. The
 * tab explains that rather than drawing a chart of zeroes.
 */
// ------------------------------------------------------------------ cost by tag

/**
 * Cost grouped by the tags on the resources that incurred it.
 *
 * The whole tab runs on one request. The server sends every tagged resource with its cost and
 * its tag keys; selecting, deselecting and switching between all/any is then set arithmetic
 * over an array already in memory, so a click repaints in a frame rather than waiting on a
 * round trip. On a few thousand resources the filter is well under a millisecond.
 *
 * Two selection modes, because both are legitimate questions and they are not interchangeable:
 * "all" answers *what does this combination cost* (resources carrying every selected tag);
 * "any" answers *what is covered by these tags at all*. Reading one as the other silently
 * changes the number, so the mode is a visible control rather than an assumption.
 */
let tagState = null;

/**
 * A selection handed to the Tags tab by another tab, applied on its next render.
 *
 * Budgets created here are tag budgets, and the tab above them is where the tag lives — so a
 * budget saying `Tracks contact = support@…` should be able to show what is actually inside
 * that figure. Passed as state rather than a URL because the tab's selection has never been
 * addressable, and inventing a deep-link format for one button is more surface than it earns.
 */
let pendingTagSelection = null;

/** Open the Tags tab already narrowed to a budget's own filter. */
function showTagsFor(tags, name) {
  if (!tags || !tags.length) return;
  pendingTagSelection = tags;
  pendingTagName = name || null;
  selectTab("tags");
}
let pendingTagName = null;

// What is being asked of the Cost by hour tab: the schedule to model against, and which slice
// of the estate to ask about. Module-level so it survives a re-render but not a reload — these
// are a question being posed, not a setting, and the answer changes with period and scope.
let uptimeSchedule = "business";
let uptimeFilter = { service: "", group: "", region: "", state: "all", find: "" };

/** A week is 168 hours. Everything on this tab that talks about a schedule divides by it. */
const WEEK_HOURS = 168;

const UPTIME_STATES = [
  ["all", "All meters"],
  ["always", "Never switched off"],
  ["schedulable", "Can be scheduled"],
  ["idle", "Schedulable and never off"],
  ["part", "Already part-time"],
];

/** Whether a row survives the current filter. Every panel on the tab reads through this. */
function uptimeKeep(r, f) {
  if (f.service && r.service !== f.service) return false;
  if (f.group && r.resource_group !== f.group) return false;
  if (f.region && r.region !== f.region) return false;
  if (f.state === "always" && !r.always_on) return false;
  if (f.state === "schedulable" && !r.schedulable) return false;
  if (f.state === "idle" && !(r.schedulable && r.always_on)) return false;
  if (f.state === "part" && r.always_on) return false;
  if (f.find) {
    const hay = `${r.name} ${r.service} ${r.meter} ${r.resource_group} ${r.region}`.toLowerCase();
    if (!hay.includes(f.find.toLowerCase())) return false;
  }
  return true;
}

/**
 * The values a dimension actually takes, with what each is worth.
 *
 * Built from the rows rather than from a fixed list, so the dropdown can only ever offer a
 * choice that has something behind it — and each option carries its cost, because choosing
 * between eleven service names is a different task from choosing between eleven numbers.
 *
 * Each dimension is counted against the *other* filters but not against itself, which is what
 * lets someone see the alternatives to their current choice rather than only the choice they
 * already made. Counting a dimension against itself would leave every dropdown showing exactly
 * one option the moment it was used.
 */
function uptimeFacet(rows, f, field) {
  const others = { ...f, [field]: "" };
  const totals = new Map();
  for (const r of rows) {
    if (!uptimeKeep(r, others)) continue;
    const v = r[field] || "";
    if (!v) continue;
    totals.set(v, (totals.get(v) || 0) + r.cost);
  }
  return [...totals.entries()].sort((a, b) => b[1] - a[1]);
}

/**
 * A typical recent day, resistant to the partial one at the end.
 *
 * The mirror of `_recent_daily` on the server, and it exists for the same reason: cost exports
 * restate for a day or two, so the newest day is routinely a fraction of a real one and a mean
 * carries that straight into the headline. Duplicated deliberately — the server cannot compute
 * this for a filter it does not know about, and a round trip per dropdown change is the thing
 * this whole tab is built to avoid.
 */
function recentDaily(series) {
  const tail = series.slice(-7).map(Number).sort((a, b) => a - b);
  if (!tail.length) return 0;
  const mid = Math.floor(tail.length / 2);
  return tail.length % 2 ? tail[mid] : (tail[mid - 1] + tail[mid]) / 2;
}

/** Daily hours and cost for a set of rows, summed from their per-meter cells. */
function uptimeSeries(rows, daily, dates) {
  const hours = new Array(dates.length).fill(0);
  const cost = new Array(dates.length).fill(0);
  for (const r of rows) {
    for (const [i, h, c] of daily[r.key] || []) {
      hours[i] += h;
      cost[i] += c;
    }
  }
  return { hours, cost };
}

/**
 * The headline figures for whatever is currently selected.
 *
 * Computed here even when nothing is filtered, rather than using the server's totals for the
 * unfiltered case and these for the rest. Two implementations of one number is how they come
 * to disagree, and the disagreement would show up only under a filter — which is exactly where
 * nobody has a second source to check it against.
 */
function uptimeTotals(rows, series, totalSpend) {
  const sum = (f) => rows.reduce((n, r) => n + f(r), 0);
  const hours = sum((r) => r.hours);
  const cost = sum((r) => r.cost);
  const alwaysOn = rows.filter((r) => r.always_on);
  const week = series.hours.slice(-7);
  return {
    hours,
    cost,
    coverage_pct: totalSpend > 0 ? (cost / totalSpend) * 100 : 0,
    avg_rate: hours > 0 ? cost / hours : 0,
    run_rate: recentDaily(series.cost) / 24,
    run_hours: recentDaily(series.hours),
    // How many of the last seven days this selection ran at all. The median is the right
    // defence against a partial final day across a whole estate, where most things run daily
    // — but on an intermittent subset it collapses to zero, and "$0.00 an hour" next to "98
    // hours billed" reads as a broken figure rather than a true one. This is what lets the
    // KPI say which it is.
    active_days: week.filter((h) => h > 0).length,
    week_days: week.length,
    resources: new Set(rows.map((r) => r.id)).size,
    meters: rows.length,
    always_on: alwaysOn.length,
    always_on_cost: alwaysOn.reduce((n, r) => n + r.cost, 0),
  };
}

/**
 * What an hour of this estate costs, and how much of the time it was actually running.
 *
 * The tab exists because a daily cost column cannot answer either question. Azure bills by the
 * day, so there is no hour-of-day series to plot — but time-metered rows carry the *hours
 * consumed*, which is uptime measured rather than guessed, and cost divided by those hours is
 * the effective hourly rate. That rate is the only number a shutdown schedule can be argued
 * from: "this VM cost £40 last month" does not tell you what leaving it on is worth.
 *
 * Filtering and the savings model both run here rather than on the server. The whole response
 * is already in the page, so narrowing to a service or changing the schedule under
 * consideration is arithmetic over data in hand — and putting either behind a request would
 * mean a spinner on every dropdown. Crucially the filter drives the charts as well as the
 * table: a view that narrowed the rows but left the trend showing the whole estate would be
 * answering a question nobody asked, next to the one they did.
 */
async function renderUptime(token) {
  const d = await cached(key("uptime"), () =>
    get(`/api/dashboard/uptime${qs()}`, { timeout: 45000 }));
  if (token !== loadToken) return;

  const all = d.resources || [];
  if (!all.length) {
    $("tabBody").innerHTML = `<section class="card"><h3>Cost by hour</h3>
      <p class="muted empty">Nothing in this period is billed by the hour.</p>
      <p class="muted">This view is built from time-metered usage — meters priced per hour,
        where Azure states how many hours were consumed. Storage, egress and per-request
        services have no hours to report, so an estate made only of those appears here
        empty.</p></section>`;
    return;
  }

  const cur = d.currency;
  const dates = d.dates || [];
  const daily = d.daily || {};
  const schedules = d.schedules || [];
  const preset = schedules.find((s) => s.id === uptimeSchedule) || schedules[0];
  const weekly = preset ? preset.hours : WEEK_HOURS;

  const f = uptimeFilter;
  const rows = all.filter((r) => uptimeKeep(r, f));
  const narrowed = rows.length !== all.length;

  // With the per-meter detail dropped for size there is nothing to rebuild a filtered chart
  // from, so the charts stay estate-wide and say so rather than being quietly wrong.
  const canChart = !d.daily_dropped && dates.length > 1;
  const series = canChart
    ? uptimeSeries(rows, daily, dates)
    : { hours: d.trend?.hours || [], cost: d.trend?.cost || [] };

  const t = uptimeTotals(rows, series, d.total_spend ?? d.totals?.total_spend ?? 0);

  // The candidate set is computed here rather than taken from the server's always-on count,
  // because which resources a schedule would actually save on depends on the schedule: one
  // already running 50 hours a week saves nothing by moving to a 60-hour one. Deriving the
  // count, the current cost and the saving from the same predicate is what stops the sentence
  // below claiming a $58 saving on a $60 subset.
  const candidates = rows.filter((r) => scheduleSaving(r, weekly) > 0);
  const modelled = candidates.reduce((n, r) => n + scheduleSaving(r, weekly), 0);
  const candidateCost = candidates.reduce((n, r) => n + r.cost, 0);
  const maxCost = rows.reduce((m, r) => Math.max(m, r.cost), 0);

  // A rate is not a total: four decimals, because the interesting ones are fractions of a
  // penny and rounding them to two turns every small meter into "$0.00".
  const rate = (n) => (n ? esc(money(n, cur, 4)) : "—");
  const hours = (n) => Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });

  // A steady rate is only meaningful for something that runs steadily. Where the selection ran
  // on fewer than half of the last seven days the median is zero and truthful but useless as a
  // headline, so the KPI stops asserting a rate and reports the intermittency instead — that
  // is the real answer to "what does an hour cost" for a workload that is usually off.
  const intermittent = t.run_hours === 0 && t.hours > 0;
  const ranOn = `ran on ${t.active_days} of the last ${t.week_days} days`;

  const picker = (id, label, field, options) => `
    <label class="muted note">${esc(label)}
      <select id="${id}" data-field="${field}" aria-label="Filter by ${esc(label)}">
        <option value="">All ${esc(label.toLowerCase())} (${options.length})</option>
        ${options
          .map(
            ([v, c]) => `<option value="${esc(v)}"${v === f[field] ? " selected" : ""}>
              ${esc(v)} — ${esc(money(c, cur, 0))}</option>`
          )
          .join("")}
      </select>
    </label>`;

  $("tabBody").innerHTML = `
    <!-- Above the figures, because it changes them. A control that alters the numbers sitting
         above it reads as broken the first time someone uses it. -->
    <section class="card">
      <div class="tagpick-head">
        <h3>Cost by hour</h3>
        ${
          narrowed
            ? `<span class="muted note">${rows.length} of ${all.length} meters ·
                 <button type="button" id="uptimeClear" class="linkish">clear filters</button></span>`
            : `<span class="muted note">${all.length} hourly meters</span>`
        }
      </div>
      <div class="uptime-filters">
        ${picker("upService", "Service", "service", uptimeFacet(all, f, "service"))}
        ${picker("upGroup", "Resource group", "group", uptimeFacet(all, f, "resource_group"))}
        ${picker("upRegion", "Region", "region", uptimeFacet(all, f, "region"))}
        <label class="muted note">State
          <select id="upState" aria-label="Filter by running state">
            ${UPTIME_STATES.map(
              ([v, l]) =>
                `<option value="${v}"${v === f.state ? " selected" : ""}>${esc(l)}</option>`
            ).join("")}
          </select>
        </label>
        <label class="muted note">Find
          <input id="upFind" type="search" value="${esc(f.find)}" placeholder="name or meter"
            aria-label="Search resources by name or meter">
        </label>
      </div>
    </section>

    ${
      rows.length
        ? ""
        : `<section class="card"><p class="muted empty">No hourly meter matches these
             filters.</p></section>`
    }

    ${kpiRow([
      {
        label: "Costs per hour, now",
        value: intermittent ? "—" : moneyHtml(t.run_rate, cur, 2),
        sub: intermittent ? ranOn : `${hours(t.run_hours)} hours running per day`,
      },
      {
        label: "Average rate per hour",
        value: moneyHtml(t.avg_rate, cur, 4),
        sub: `blended across ${t.meters} hourly meter${t.meters === 1 ? "" : "s"}`,
      },
      {
        label: "Hours billed",
        value: hours(t.hours),
        sub: `${t.resources} resource${t.resources === 1 ? "" : "s"}, last ${d.days} days`,
      },
      {
        label: "Hour-billed spend",
        value: moneyHtml(t.cost, cur, 2),
        sub: `${t.coverage_pct.toFixed(1)}% of all spend in this period`,
      },
      {
        label: "Never switched off",
        value: String(t.always_on),
        sub: `${esc(money(t.always_on_cost, cur, 0))} running 24/7`,
      },
      {
        label: `Modelled saving`,
        value: moneyHtml(modelled, cur, 0),
        cls: modelled > 0 ? "down" : "flat",
        sub: `over ${d.days} days, ${candidates.length} resource${
          candidates.length === 1 ? "" : "s"
        }`,
      },
    ])}

    <section class="card">
      <div class="tagpick-head">
        <h3>If the schedulable resources were put on a schedule</h3>
        <label class="muted note">Schedule
          <select id="uptimeSchedule" aria-label="Shutdown schedule to model against">
            ${schedules
              .map(
                (s) => `<option value="${esc(s.id)}"${s.id === preset?.id ? " selected" : ""}>
                  ${esc(s.label)} — ${s.hours}h/week</option>`
              )
              .join("")}
          </select>
        </label>
      </div>
      <p class="muted">A week is ${WEEK_HOURS} hours. Running only ${weekly} of them costs
        ${Math.round((weekly / WEEK_HOURS) * 100)}% of running all of them.
        ${
          candidates.length
            ? `The ${candidates.length} resource${candidates.length === 1 ? "" : "s"}
               ${narrowed ? "in this selection" : "here"} that
               ${candidates.length === 1 ? "runs" : "run"} longer than that and
               <em>can</em> be stopped would have cost
               ${esc(money(candidateCost - modelled, cur, 0))} instead of
               ${esc(money(candidateCost, cur, 0))} over these ${d.days} days —
               ${esc(money(modelled, cur, 0))} less.`
            : `Nothing ${narrowed ? "in this selection" : "in this period"} is both stoppable
               and running longer than that, so this schedule would save nothing.`
        }</p>
      <p class="muted note">A ceiling, not a forecast. It assumes nothing was needed out of
        hours, and it counts only services Azure lets you stop: a managed search index or a
        Defender node bills by the hour and cannot be switched off, so its idle hours are not
        offered as a saving you could never bank.</p>
    </section>

    <div id="uptimeTrend" class="card chart-card"></div>
    <div id="uptimeWeek" class="card chart-card"></div>

    ${table(
      narrowed ? "The selected hourly meters" : "Every hourly meter, by what an hour of it costs",
      rows,
      [
        {
          label: "Resource",
          cell: (r) => `${esc(r.name)} ${bar(r.cost, maxCost)}`,
        },
        { label: "Service", cell: (r) => esc(r.service || "—") },
        { label: "Meter", cell: (r) => esc(r.meter || "—") },
        {
          label: "Hours",
          num: true,
          value: (r) => r.hours,
          cell: (r) => hours(r.hours),
          total: (rows) => hours(rows.reduce((n, r) => n + r.hours, 0)),
        },
        {
          // Measured across the resource's own lifetime, not the window: something created on
          // Tuesday was not switched off for the days before it existed. The instance count
          // rides along because it is what the percentage was measured against — 100% on a
          // two-node pool is 48 hours a day, and without it the hours column looks wrong.
          label: "Uptime",
          num: true,
          value: (r) => r.uptime_pct,
          cell: (r) =>
            `${r.uptime_pct}%${r.units > 1 ? ` <span class="muted">×${r.units}</span>` : ""}${
              r.always_on ? ' <span class="delta up">24/7</span>' : ""
            }`,
        },
        {
          label: "Per hour",
          num: true,
          value: (r) => r.rate,
          cell: (r) => rate(r.rate),
        },
        {
          label: `Cost, ${d.days}d`,
          num: true,
          value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2),
        },
        {
          label: "On a schedule",
          num: true,
          value: (r) => scheduleSaving(r, weekly),
          cell: (r) => {
            const s = scheduleSaving(r, weekly);
            if (!r.schedulable) return `<span class="muted">cannot be stopped</span>`;
            if (s <= 0) return `<span class="muted">already off enough</span>`;
            return `<span class="delta down">−${esc(money(s, cur, 2))}</span>`;
          },
          total: (rows) =>
            `<span class="delta down">−${esc(
              money(rows.reduce((n, r) => n + scheduleSaving(r, weekly), 0), cur, 0)
            )}</span>`,
        },
      ],
      `Hours come from the usage quantity on hour-priced meters, so uptime is measured rather `
        + `than inferred — a stopped resource emits none. ${
          d.truncated ? "Showing the largest meters only. " : ""
        }${d.mixed_currency ? "This estate bills in more than one currency; figures show the largest. " : ""}`,
      { pageSize: 20 }
    )}
  `;

  wireUptimeControls(token);

  if (canChart && rows.length) {
    // Cost per hour of wall-clock, not per hour consumed: the daily bill spread over the 24
    // hours it covers. It is the honest answer to "what does an hour cost" from data that is
    // only ever daily, and unlike the blended rate it moves when the estate grows.
    drawChart($("uptimeTrend"), {
      type: "area",
      title: narrowed
        ? "What an hour of this selection cost, day by day"
        : "What an hour of this estate cost, day by day",
      labels: dates,
      datasets: [{ label: "Per hour", data: series.cost.map((c) => +(c / 24).toFixed(4)) }],
      currency: cur,
      note: "Daily spend on hourly meters divided by 24. The last day of any cost export is "
        + "usually partial, so a dip at the right-hand edge is the data arriving, not a saving.",
    });

    // The closest thing to a clock that daily billing supports, and it is measured. A flat
    // profile is itself the finding: nothing in this selection is on a schedule.
    const week = weekdayProfile(dates, series.hours);
    if (week.some((w) => w.hours > 0)) {
      drawChart($("uptimeWeek"), {
        type: "bar",
        title: "Hours running, by day of the week",
        labels: week.map((w) => w.short),
        datasets: [{ label: "Average hours", data: week.map((w) => +w.hours.toFixed(1)) }],
        note: "Average across every such weekday in the period. A flat line means nothing is "
          + "switched off at weekends.",
      });
    } else {
      $("uptimeWeek").remove();
    }
  } else {
    $("uptimeTrend").remove();
    $("uptimeWeek").remove();
  }
}

const WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/**
 * Average hours per day of the week, from a daily series.
 *
 * Dates are plain YYYY-MM-DD, so they are read in UTC: parsing them as local time would shift
 * the whole series by a day for anyone west of Greenwich and silently rotate the chart.
 */
function weekdayProfile(dates, hours) {
  const bucket = WEEKDAY_NAMES.map(() => ({ hours: 0, n: 0 }));
  dates.forEach((day, i) => {
    const parsed = new Date(`${day}T00:00:00Z`);
    if (Number.isNaN(parsed.getTime())) return;
    // JS weeks start on Sunday; this chart starts on Monday, as the server's does.
    const slot = (parsed.getUTCDay() + 6) % 7;
    bucket[slot].hours += hours[i] || 0;
    bucket[slot].n += 1;
  });
  return WEEKDAY_NAMES.map((short, i) => ({
    short,
    hours: bucket[i].n ? bucket[i].hours / bucket[i].n : 0,
    days: bucket[i].n,
  }));
}

/** Re-render on any change: one path, so the panels cannot disagree about what is selected. */
function wireUptimeControls(token) {
  const again = async () => {
    await renderUptime(token);
    // renderTab wires the tables it renders; a re-render from here has to do it itself, or
    // sorting and paging quietly stop working after the first filter change.
    if (token === loadToken) wireTables($("tabBody"));
  };

  for (const id of ["upService", "upGroup", "upRegion"]) {
    const el = $(id);
    if (el) el.onchange = (e) => { uptimeFilter[e.target.dataset.field] = e.target.value; again(); };
  }
  if ($("upState")) $("upState").onchange = (e) => { uptimeFilter.state = e.target.value; again(); };
  if ($("uptimeSchedule")) {
    $("uptimeSchedule").onchange = (e) => { uptimeSchedule = e.target.value; again(); };
  }
  if ($("uptimeClear")) {
    $("uptimeClear").onclick = () => {
      uptimeFilter = { service: "", group: "", region: "", state: "all", find: "" };
      again();
    };
  }

  const find = $("upFind");
  if (find) {
    // Debounced, and the caret restored afterwards: re-rendering on every keystroke would
    // rebuild the input under the person typing into it.
    let timer = null;
    find.oninput = (e) => {
      const { value, selectionStart } = e.target;
      clearTimeout(timer);
      timer = setTimeout(async () => {
        uptimeFilter.find = value;
        await again();
        const back = $("upFind");
        if (back) {
          back.focus();
          back.setSelectionRange(selectionStart, selectionStart);
        }
      }, 250);
    };
  }
}

/**
 * What putting one resource on a schedule would have saved over the period.
 *
 * Measured against the hours it *actually* ran, not against a full week. A resource already up
 * only 84 hours a week does not save 64% by moving to a 60-hour schedule — it saves the
 * difference between 84 and 60, and one already inside the schedule saves nothing at all.
 * Modelling from 168 would have credited every part-time resource with a saving it had already
 * taken, and the tab would have added those up into a number the estate could not deliver.
 */
function scheduleSaving(r, weeklyHours) {
  if (!r.schedulable || !r.cost) return 0;
  const running = (r.uptime_pct / 100) * WEEK_HOURS;
  if (running <= weeklyHours) return 0;
  return r.cost * (1 - weeklyHours / running);
}

async function renderTags(token) {
  const d = await cached(key("tags"), () =>
    get(`/api/dashboard/tags${qs()}`, { timeout: 45000 }));
  if (token !== loadToken) return;

  const keys = d.keys || [];
  if (!keys.length) {
    // Three different situations, and only one of them is "you have no tags". A quick refresh
    // loads costs through the Query API, which cannot return tags at all — so the warehouse
    // legitimately holds spend with no tag column, and saying the estate is untagged would be
    // stating something false about it with complete confidence.
    let why;
    let fix = "";
    if (d.tags_not_loaded) {
      why = "The cost data currently loaded was fetched by a quick refresh, which cannot "
          + "return tags. Your resources may well be tagged — this view just has nothing to "
          + "read yet.";
      fix = `<p class="muted">Load tags with <strong>Refresh → FOCUS report</strong>, or a `
          + `full-detail refresh.</p>`;
    } else if ((d.untagged?.resources || 0) > 0) {
      why = `None of the ${d.untagged.resources.toLocaleString()} resources in this period carry a tag.`;
    } else {
      why = "No cost data is loaded for this period yet.";
    }
    $("tabBody").innerHTML = `<section class="card"><h3>Cost by Application / Tags</h3>
      <p class="muted empty">${esc(why)}</p>${fix}${uncostedMarkup(d)}</section>`;
    return;
  }

  // Selection survives a re-render of the same view but not a change of period or scope, where
  // the tags on offer may genuinely differ.
  const sig = key("tags");
  if (!tagState || tagState.sig !== sig) {
    // `values` maps a selected tag key to the set of its values the user has narrowed to.
    // Absent or empty means "every value of that key", which is what selecting a key alone
    // has always meant.
    tagState = { sig, selected: new Set(), values: new Map(), mode: "all", filter: "", day: null,
                 budget: { open: false } };
  }

  tagState.data = d;

  // A selection arriving from a budget. Applied after the state exists so it survives the
  // signature check above, and only for keys this period actually has — a budget filtered on a
  // tag with no spend in the window would otherwise select a chip that is not on screen and
  // read as a broken link rather than an empty answer.
  if (pendingTagSelection) {
    const have = new Set(d.keys.map((k) => k.key));
    // Case-insensitively, because Azure filters budgets that way even though it stores the
    // keys case-sensitively: this estate has `Contact` and `contact` as separate keys and a
    // budget on one is answered by both.
    const lower = new Map(d.keys.map((k) => [k.key.toLowerCase(), k.key]));
    let applied = 0;
    for (const [rawKey, values] of pendingTagSelection) {
      const k = have.has(rawKey) ? rawKey : lower.get(String(rawKey).toLowerCase());
      if (!k) continue;
      tagState.selected.add(k);
      if (values && values.length) {
        tagState.values.set(k, new Set(values));
      }
      applied += 1;
    }
    tagState.fromBudget = applied ? pendingTagSelection : null;
    tagState.fromBudgetName = applied ? pendingTagName : null;
    pendingTagSelection = null;
    pendingTagName = null;
  }

  $("tabBody").innerHTML = tagsMarkup(d);
  wireTags();
  paintTags();
}

function tagsMarkup(d) {
  const cur = d.currency;
  const tagged = d.tagged || { cost: 0, resources: 0 };
  const untagged = d.untagged || { cost: 0, resources: 0 };
  const coverage = d.total ? (tagged.cost / d.total) * 100 : 0;

  return `
    ${d.mixed ? `<div class="banner warn">This estate bills in more than one currency.
        Showing ${esc(cur || "")} only — the rest is excluded rather than converted.</div>` : ""}
    ${d.truncated ? `<div class="banner warn">Showing the first 8,000 resources.
        Narrow the subscription scope for a complete figure.</div>` : ""}
    ${tagState?.fromBudget ? `<div class="banner">Showing the tags
        <strong>${esc(tagState.fromBudgetName || "a budget")}</strong> tracks. Figures here can
        be higher than the budget's: this tab attributes a resource's whole cost to its tags,
        while a budget counts only the charges carrying them. Clear a chip to widen.</div>` : ""}

    ${kpiRow([
      { label: "Selected", value: `<span id="tagSum">${esc(money(0, cur))}</span>`,
        sub: `<span id="tagSumSub">nothing selected</span>` },
      { label: "Tagged spend", value: esc(money(tagged.cost, cur)),
        sub: `${tagged.resources.toLocaleString()} resource${tagged.resources === 1 ? "" : "s"}` },
      { label: "Untagged spend", value: esc(money(untagged.cost, cur)),
        cls: untagged.cost > 0 ? "up" : "",
        sub: `${untagged.resources.toLocaleString()} resource${untagged.resources === 1 ? "" : "s"}` },
      { label: "Tag coverage", value: `${coverage.toFixed(0)}%`,
        sub: "of spend carries a tag" },
    ])}

    <section class="card tagpick">
      <div class="tagpick-head">
        <h3>Tags</h3>
        <div class="tagpick-tools">
          <div class="seg" role="radiogroup" aria-label="How to combine selected tags">
            <button type="button" class="seg-btn on" data-mode="all" role="radio"
                    aria-checked="true">Has all</button>
            <button type="button" class="seg-btn" data-mode="any" role="radio"
                    aria-checked="false">Has any</button>
          </div>
          <input id="tagFilter" type="search" class="tagsearch" placeholder="Find a tag…"
                 aria-label="Filter the tag list" autocomplete="off">
        </div>
      </div>
      <p class="muted tagpick-hint" id="tagModeHint"></p>
      <div class="tagchips" id="tagChips" role="group" aria-label="Available tags"></div>
    </section>

    ${uncostedMarkup(d)}

    <section class="card tagsel">
      <div class="tagsel-head">
        <h3>Selected</h3>
        <div class="tagsel-actions">
          <button type="button" class="linkish" id="tagClear" hidden>Clear all</button>
          <button type="button" class="primary sm" id="tagBudgetBtn" hidden>Create budget</button>
        </div>
      </div>
      <div class="tagchips selected" id="tagSelected" aria-live="polite"></div>
    </section>

    <div id="tagBudget"></div>
    <div id="tagValues"></div>
    <div id="tagTrend"></div>
    <div id="tagDay"></div>
    <div id="tagResults"></div>`;
}

/**
 * Tags that exist on resources but have no spend behind them in this window.
 *
 * Without this the tab is silent about them, and silence reads as "your tag didn't work". It
 * nearly always did — a tag reaches cost data only by riding on a usage record, so one added
 * today, or sitting on a deallocated VM or a free resource, is real and simply has nothing to
 * bill.
 *
 * The heading carries that whole point, so it is not explained again underneath. The long
 * version earned its place once, when the behaviour was surprising; kept permanently it is a
 * paragraph of billing mechanics between someone and the list they came to read. The per-chip
 * tooltip still says how many resources carry each tag.
 *
 * Not selectable, and visually separate from the costed chips, because there is no cost to
 * add: offering them as filters would only ever produce zero.
 */
function uncostedMarkup(d) {
  const rest = d.uncosted || [];
  if (!rest.length) return "";

  const chips = rest.map((k) =>
    `<span class="chip ghost" title="${esc(k.key)} — on ${k.resources} resource${
      k.resources === 1 ? "" : "s"}, no cost in this window">
       <span class="chip-k">${esc(k.key)}</span>
       <span class="chip-n">${k.resources}</span>
     </span>`).join("");

  return `
    <section class="card taguncosted">
      <div class="tagpick-head">
        <h3>Tagged, but no spend in selected timeline</h3>
      </div>
      <div class="tagchips">${chips}</div>
    </section>`;
}

function wireTags() {
  for (const b of $("tabBody").querySelectorAll("[data-mode]")) {
    b.onclick = () => {
      tagState.mode = b.dataset.mode;
      for (const other of $("tabBody").querySelectorAll("[data-mode]")) {
        const on = other === b;
        other.classList.toggle("on", on);
        other.setAttribute("aria-checked", String(on));
      }
      paintTags();
    };
  }
  const filter = $("tagFilter");
  if (filter) {
    filter.value = tagState.filter;
    filter.oninput = () => {
      tagState.filter = filter.value.trim().toLowerCase();
      paintChips();
    };
  }
  const clear = $("tagClear");
  if (clear) {
    clear.onclick = () => {
      tagState.selected.clear();
      tagState.values.clear();
      paintTags();
    };
  }
  const budget = $("tagBudgetBtn");
  if (budget) {
    budget.onclick = () => {
      // A result on screen means the button reopens the form rather than toggling it shut,
      // which is what "Create budget" says it does.
      const open = !!tagState.budget?.created || !tagState.budget?.open;
      tagState.budget = { open };
      paintBudget();
      if (open) {
        $("tagBudget")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    };
  }
}

function toggleTag(k) {
  if (tagState.selected.has(k)) {
    tagState.selected.delete(k);
    // Deselecting a key drops the values narrowed under it. Keeping them would silently
    // re-apply a filter the user cannot see when they pick the key up again later.
    tagState.values.delete(k);
  } else {
    tagState.selected.add(k);
  }
  paintTags();
}

/** The value a resource carries for one tag key, or null if it does not carry that key. */
function tagValue(resource, k) {
  const i = resource.keys.indexOf(k);
  if (i < 0) return null;
  const pool = tagState.data.values || [];
  return pool[(resource.v || [])[i]] ?? "";
}

/**
 * A resource's tags as `key=value` pills.
 *
 * The key alone was ambiguous in exactly the way the value picker exists to fix: a row tagged
 * `Owner` tells you nothing about whose it is. Long values are truncated visually but kept
 * whole in the tooltip, because an app-insights resource id is a legitimate tag value and
 * would otherwise push every other column off the table.
 */
function tagPills(resource) {
  return (resource.keys || [])
    .map((k) => {
      const v = tagValue(resource, k);
      const shown = v === "" || v === null ? k : `${k}=${v}`;
      const on = tagState.selected.has(k)
        && (() => {
          const wanted = tagState.values.get(k);
          return !wanted || !wanted.size || wanted.has(v);
        })();
      return `<span class="minitag${on ? " on" : ""}" title="${esc(shown)}">${esc(shown)}</span>`;
    })
    .join("");
}

/**
 * Does this resource satisfy a selected key, including any value narrowing on it?
 *
 * Values within a key are OR: `Owner` narrowed to Deb and Ravi means either, because they are
 * alternatives for the same field. That is the ordinary meaning of a facet, and the AND/OR
 * toggle stays where it is — across keys, where the ambiguity actually was.
 */
function matchesKey(resource, k) {
  const v = tagValue(resource, k);
  if (v === null) return false;
  const wanted = tagState.values.get(k);
  return !wanted || wanted.size === 0 || wanted.has(v);
}

/** The chip list. Separated so typing in the filter doesn't recompute the costs below it. */
function paintChips() {
  const d = tagState.data;
  const cur = d.currency;
  const q = tagState.filter;
  const shown = d.keys.filter((k) => !q || k.key.toLowerCase().includes(q));

  $("tagChips").innerHTML = shown.length
    ? shown
        .map((k) => {
          const on = tagState.selected.has(k.key);
          const many = k.values > 1;
          return `<button type="button" class="chip${on ? " on" : ""}"
              data-tag="${esc(k.key)}" aria-pressed="${on}"
              title="${esc(k.key)} — ${esc(money(k.cost, cur))} across ${k.resources} resource${k.resources === 1 ? "" : "s"}${
                many ? `, ${k.values} different values` : ""}">
              <span class="chip-k">${esc(k.key)}</span>
              <span class="chip-n">${k.resources}</span>${
                many ? `<span class="chip-v" title="${k.values} values">${k.values}v</span>` : ""}
            </button>`;
        })
        .join("")
    : `<p class="muted empty">No tag matches “${esc(q)}”.</p>`;

  for (const b of $("tagChips").querySelectorAll("[data-tag]")) {
    b.onclick = () => toggleTag(b.dataset.tag);
  }
}

/**
 * Values under each selected key, so a key can be narrowed to the ones that matter.
 *
 * A tag key on its own is usually the wrong unit. `Owner` is not a cost centre; `Owner=Deb`
 * and `Owner=Ravi` are two cost centres that happen to share a key, and adding them together
 * answers a question nobody asked. Keys with a single value are skipped — there is nothing to
 * choose, and a picker with one option is just noise.
 *
 * Costs shown per value are for that value alone, independent of the other selected keys, so
 * the list stays a stable description of the key rather than shifting under every other click.
 */
function paintTagValues() {
  const host = $("tagValues");
  if (!host) return;

  const d = tagState.data;
  const cur = d.currency;
  const picked = [...tagState.selected];
  const blocks = [];

  for (const k of picked) {
    const totals = new Map();
    for (const r of d.resources) {
      const v = tagValue(r, k);
      if (v === null) continue;
      const slot = totals.get(v) || { cost: 0, n: 0 };
      slot.cost += r.cost;
      slot.n += 1;
      totals.set(v, slot);
    }
    if (totals.size < 2) continue;

    const wanted = tagState.values.get(k);
    const rows = [...totals.entries()]
      .map(([value, t]) => ({ value, ...t }))
      .sort((a, b) => b.cost - a.cost);

    blocks.push(`
      <section class="card tagvals">
        <div class="tagsel-head">
          <h3>${esc(k)} <span class="muted">· ${rows.length} values</span></h3>
          ${wanted && wanted.size
            ? `<button type="button" class="linkish" data-allvals="${esc(k)}">All values</button>`
            : ""}
        </div>
        <p class="muted tagpick-hint">${
          wanted && wanted.size
            ? `Narrowed to ${wanted.size} of ${rows.length} values.`
            : "Every value included — pick one or more to narrow."}</p>
        <div class="tagchips">${rows.map((r) => {
          const on = !!(wanted && wanted.has(r.value));
          const label = r.value === "" ? "(no value)" : r.value;
          return `<button type="button" class="chip${on ? " on" : ""}"
              data-vk="${esc(k)}" data-vv="${esc(r.value)}" aria-pressed="${on}"
              title="${esc(k)}=${esc(label)} — ${esc(money(r.cost, cur))} across ${r.n} resource${r.n === 1 ? "" : "s"}">
              <span class="chip-k">${esc(label)}</span>
              <span class="chip-n">${esc(money(r.cost, cur))}</span>
            </button>`;
        }).join("")}</div>
      </section>`);
  }

  host.innerHTML = blocks.join("");

  for (const b of host.querySelectorAll("[data-vk]")) {
    b.onclick = () => {
      const k = b.dataset.vk;
      const set = tagState.values.get(k) || new Set();
      if (set.has(b.dataset.vv)) set.delete(b.dataset.vv);
      else set.add(b.dataset.vv);
      if (set.size) tagState.values.set(k, set);
      else tagState.values.delete(k);
      paintTags();
    };
  }
  for (const b of host.querySelectorAll("[data-allvals]")) {
    b.onclick = () => {
      tagState.values.delete(b.dataset.allvals);
      paintTags();
    };
  }
}

// ------------------------------------------------------- budget from a selection

/**
 * Turn the tag selection into an Azure budget.
 *
 * The tab already answers "what does this combination cost". A budget is the standing version
 * of the same question, and until now the only way to get one was to retype the selection into
 * the portal's Create budget form — where the tag filter has to be rebuilt from memory, which
 * is exactly where it stops matching the number that prompted it.
 *
 * Three things do not survive the translation, and all three are shown rather than smoothed
 * over, because each one silently changes what gets tracked:
 *
 *   * a budget filter is a conjunction, so "Has any" across several tags has no equivalent;
 *   * a tag filter lists values explicitly, so a key selected whole is frozen to the values
 *     that exist today;
 *   * a budget belongs to one subscription, so a selection spanning several is not one budget.
 */
function budgetSelection() {
  const d = tagState.data;
  const picked = [...tagState.selected];
  const all = tagState.mode === "all";

  const matched = picked.length
    ? d.resources.filter((r) =>
        all ? picked.every((k) => matchesKey(r, k)) : picked.some((k) => matchesKey(r, k)))
    : [];

  // Values per key: the ones narrowed to, or every value that key carries. Enumerated from all
  // resources holding the key rather than from `matched`, so the filter describes the same set
  // the tab does instead of a snapshot narrowed by the other keys.
  const tags = picked.map((k) => {
    const chosen = tagState.values.get(k);
    if (chosen && chosen.size) return { key: k, values: [...chosen], whole: false };
    const seen = new Set();
    for (const r of d.resources) {
      const v = tagValue(r, k);
      if (v !== null) seen.add(v);
    }
    return { key: k, values: [...seen], whole: true };
  });

  // Which subscriptions the matched spend actually sits in, biggest first. The tag data carries
  // subscription names, so ids come from the picker's list.
  const byName = new Map();
  for (const r of matched) {
    const name = r.subscription || "(unknown)";
    byName.set(name, (byName.get(name) || 0) + r.cost);
  }
  const subs = allSubscriptions();
  const spread = [...byName.entries()]
    .map(([name, cost]) => ({ name, cost, id: (subs.find((s) => s.name === name) || {}).id }))
    .sort((a, b) => b.cost - a.cost);

  const days = d.dates?.length || d.days || 30;
  const sum = matched.reduce((t, r) => t + r.cost, 0);

  return { picked, tags, matched, sum, spread, subs, days, mode: tagState.mode, cur: d.currency };
}

/** A budget name that Azure will accept, derived from what was selected. */
function budgetName(sel) {
  const first = sel.tags[0];
  const value = first && !first.whole && first.values.length === 1 ? first.values[0] : "";
  const raw = [first ? first.key : "tags", value].filter(Boolean).join("-");
  const clean = raw.replace(/[^A-Za-z0-9_.-]/g, "-").replace(/-+/g, "-").slice(0, 40);
  return `${/^[A-Za-z0-9]/.test(clean) ? clean : `budget-${clean}`}-budget`.slice(0, 63);
}

function paintBudget() {
  const host = $("tagBudget");
  if (!host) return;

  const btn = $("tagBudgetBtn");

  // A budget that was just written is the one thing on this tab the tab cannot recompute, so
  // it is rendered rather than announced. A toast is gone in six seconds and takes the amount,
  // the reset period and the filter with it; what was actually created then exists only in the
  // portal, which is the round trip this form was built to avoid.
  const made = tagState.budget?.created;
  if (made) {
    if (btn) {
      btn.hidden = tagState.selected.size === 0;
      btn.textContent = "Create budget";
    }
    host.innerHTML = budgetResultMarkup(made);
    wireBudgetResult(made);
    return;
  }

  const open = tagState.budget?.open && tagState.selected.size > 0;
  if (btn) {
    btn.hidden = tagState.selected.size === 0;
    btn.textContent = open ? "Cancel" : "Create budget";
  }
  if (!open) {
    host.innerHTML = "";
    return;
  }

  const sel = budgetSelection();
  const cur = sel.cur;

  // Azure has no OR in a budget filter. Say so and stop, rather than offering a form whose
  // result would track a different set of resources than the figure above it.
  if (sel.mode === "any" && sel.picked.length > 1) {
    host.innerHTML = `<section class="card budgetform">
      <h3>Create a budget from this selection</h3>
      <div class="banner warn">An Azure budget can only combine tags with <strong>AND</strong>.
        “Has any” across ${sel.picked.length} tags cannot be expressed as one budget — switch to
        <strong>Has all</strong>, or select a single tag and create one budget per tag.</div>
    </section>`;
    return;
  }

  const perDay = sel.days ? sel.sum / sel.days : 0;
  const suggestion = suggestBudget(perDay, "Monthly");
  const spread = sel.spread.filter((s) => s.id);
  const missing = sel.spread.filter((s) => !s.id);
  const frozen = sel.tags.filter((t) => t.whole);
  const empty = sel.tags.filter((t) => !t.values.length);
  // Subscriptions that carry some of this selection first, then the rest. A combination with no
  // spend today is still a legitimate thing to budget for, so the picker never ends up empty
  // just because nothing matched.
  const others = sel.subs.filter((s) => s.id && !spread.some((p) => p.id === s.id));
  const canPick = spread.length + others.length > 0;

  const start = new Date();
  const startText = new Date(start.getFullYear(), start.getMonth(), 1)
    .toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" });
  // Formatted from the local parts, not via toISOString: east of UTC, midnight local is the
  // previous day in UTC, and the default expiry silently became the last of the month.
  const endDefault = isoDay(new Date(start.getFullYear() + 2, start.getMonth(), 1));

  const filterText = sel.tags
    .map((t) => `${t.key} = ${t.values.length > 3
      ? `${t.values.slice(0, 3).map(labelOf).join(", ")} +${t.values.length - 3}`
      : t.values.map(labelOf).join(", ")}`)
    .join("  AND  ");

  host.innerHTML = `<section class="card budgetform">
    <h3>Create a budget from this selection</h3>

    ${empty.length ? `<div class="banner warn">No values in this period for
      ${empty.map((t) => `<strong>${esc(t.key)}</strong>`).join(", ")}. An Azure budget matches
      tag values explicitly, so there is nothing to filter on yet.</div>` : ""}

    ${!sel.matched.length && !empty.length ? `<div class="banner warn">Nothing in this period
      carries ${sel.picked.length > 1 ? "all of these tags together" : "this tag"}, so the budget
      would start out tracking no spend. That is fine for something being set up ahead of time —
      choose the subscription it should watch.</div>` : ""}

    ${sel.matched.length && !spread.length ? `<div class="banner warn">The matched resources sit
      in ${missing.length ? esc(missing.map((m) => m.name).join(", ")) : "a subscription"} which
      this page cannot match to one you can pick, so choose the target subscription yourself.</div>` : ""}

    ${!canPick ? `<div class="banner warn">No subscription is available to create a budget
      on.</div>` : ""}

    <p class="muted note">Creates a real Azure budget
      (<code>Microsoft.Consumption/budgets</code>) filtered to these tags. It needs Cost Management
      Contributor on the subscription — held by your own account when delegated Azure access is
      enabled, and otherwise by the app's managed identity.</p>

    <div class="budget-filter">
      <span class="k">Filter</span>
      <span class="v">${esc(filterText || "—")}</span>
    </div>

    ${frozen.length ? `<p class="muted note">${frozen.map((t) => `<strong>${esc(t.key)}</strong>`).join(", ")}
      ${frozen.length === 1 ? "was" : "were"} selected without narrowing, so
      ${frozen.length === 1 ? "its" : "their"} current
      ${frozen.reduce((n, t) => n + t.values.length, 0)} value(s) are listed in the filter. A
      value created later is not covered until you update the budget.</p>` : ""}

    <div class="budget-grid">
      <label class="bfield"><span>Subscription</span>
        <select id="bSub" ${canPick ? "" : "disabled"}>
          ${spread.map((s, i) => `<option value="${esc(s.id)}" ${i === 0 ? "selected" : ""}>
            ${esc(s.name)} — ${esc(money(s.cost, cur))} of the selection</option>`).join("")}
          ${others.map((s) => `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join("")}
        </select>
      </label>

      <label class="bfield"><span>Budget name</span>
        <input id="bName" type="text" maxlength="63" value="${esc(budgetName(sel))}"
               autocomplete="off" spellcheck="false">
      </label>

      <label class="bfield"><span>Reset period</span>
        <select id="bGrain">
          <option value="Monthly" selected>Monthly</option>
          <option value="Quarterly">Quarterly</option>
          <option value="Annually">Annually</option>
        </select>
      </label>

      <label class="bfield"><span>Amount${cur ? ` (${esc(cur)})` : ""}</span>
        <input id="bAmount" type="number" min="1" step="1" value="${suggestion || ""}">
      </label>

      <label class="bfield"><span>Alert at (% of budget)</span>
        <input id="bThresholds" type="text" value="80, 100" autocomplete="off">
      </label>

      <label class="bfield"><span>Notify</span>
        <input id="bEmails" type="text" placeholder="you@example.com"
               autocomplete="off" spellcheck="false">
      </label>

      <label class="bfield"><span>Expires</span>
        <input id="bEnd" type="date" value="${esc(endDefault)}">
      </label>

      <div class="bfield static"><span>Starts</span><div class="bstatic">${esc(startText)}</div></div>
    </div>

    <p class="muted note" id="bSuggest">${suggestion
      ? `Suggested ${esc(money(suggestion, cur))} a month, from ${esc(money(perDay, cur))} a day
         across the last ${sel.days} day(s) of data. An average is a floor, not a budget —
         it is rounded up so an ordinary month does not trip the alert.`
      : "No spend in this period to suggest an amount from."}</p>

    <div class="build-bar">
      <button class="primary" id="bCreate" ${canPick && !empty.length ? "" : "disabled"}>
        Create budget</button>
      <span class="muted" id="bNote"></span>
    </div>
  </section>`;

  wireBudget(sel);
}

// ================================================================ shutdown savings

/**
 * Machines billed for 168 hours a week that are wanted for about 60.
 *
 * The honest framing matters more here than anywhere else in the app, because this is the one
 * tab that proposes turning something off. Every figure is a ceiling, every exclusion is stated,
 * and anything tagged production never appears — a saving that causes an outage is not a saving.
 */
async function renderShutdown(token) {
  const d = await get(`/api/dashboard/shutdown${qs()}`, { timeout: 120000 });
  if (token !== loadToken) return;

  const cur = d.currency;
  if (!d.candidates.length) {
    $("tabBody").innerHTML = `
      ${kpiRow([
        { label: "Examined", value: String(d.examined || 0), sub: "running VMs" },
        { label: "Schedulable", value: "0", sub: "none with a clear rhythm" },
        { label: "Busy or idle throughout", value: String(d.skipped_steady || 0),
          sub: "no daily pattern" },
        { label: "Tagged production", value: String(d.skipped_production || 0),
          sub: "never suggested" },
      ])}
      <section class="card">
        <h3>Nothing safe to schedule</h3>
        <p class="muted">${esc(d.note || "")}</p>
        <p class="muted note">A machine that is idle around the clock is waste rather than a
          scheduling opportunity — look at
          <button type="button" class="linkish" data-goto="waste">Orphaned resources</button>
          instead.</p>
      </section>`;
    wireBudgetLinks();
    return;
  }

  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: "Could be scheduled", value: String(d.count), sub: `of ${d.examined} running VMs` },
      { label: "Costing now", value: esc(money(d.total_current, cur)), sub: "per month, 24×7" },
      { label: "Up to", value: esc(money(d.total_saving, cur)), cls: "down",
        sub: "per month if scheduled" },
      { label: "Window", value: `${d.hours_kept}h`, sub: `of ${168}h a week` },
    ])}

    <section class="card">
      <div class="tagpick-head">
        <h3>Only used during the working day</h3>
        <span class="muted note">${esc(d.window)} · ${d.days_analysed} days of hourly CPU</span>
      </div>
      <p class="muted note"><strong>“Up to” is meant literally.</strong> The figure is the
        compute share of the bill for the hours saved. Disks and reserved IPs keep charging while
        a machine is deallocated, and a reservation or savings plan already discounts the hours —
        so the realised saving is lower. ${d.skipped_production
          ? `${d.skipped_production} VM${d.skipped_production === 1 ? " is" : "s are"} tagged
             production and excluded outright.` : ""}</p>
      <div class="anoms">${d.candidates.map(shutdownCard).join("")}</div>
    </section>`;
}

function shutdownCard(c) {
  const cur = c.currency;
  return `<article class="anom">
    ${cardHead({
      name: c.name,
      sub: `${c.size || ""} · ${c.resource_group || ""} · ${c.location || ""}`,
      figure: `−${esc(money(c.saving, cur, 2))}`,
      figureClass: "down",
      status: "per month",
      statusClass: "ok",
    })}
    <div class="budget-meta">
      <span><strong>${c.avg_business}%</strong> CPU in hours ·
        <strong>${c.avg_offhours}%</strong> outside</span>
      <span>${esc(money(c.current, cur, 2))} → ${esc(money(c.scheduled, cur, 2))}</span>
      <span>${c.hours_saved}h a week off</span>
    </div>
    <p class="muted note">Busy ${c.ratio}× more during ${esc(c.window)}.
      ${c.busy_offhours_hours
        ? `Active in ${c.busy_offhours_hours} off-hours sample(s) — check nothing runs overnight.`
        : "Never active outside those hours in the period sampled."}</p>
  </article>`;
}

// ================================================================ anomalies

/**
 * The days that did not look like the others.
 *
 * Framed as events rather than as a table of numbers, because the question is never "what was
 * the modified z-score" — it is "what changed, when, and how much did it cost me". The
 * statistic earns its place by choosing the rows; it does not need to be on screen.
 */
async function renderAnomalies(token) {
  const d = await get(`/api/dashboard/anomalies${qs()}`, { timeout: 45000 });
  if (token !== loadToken) return;

  const cur = d.currency;
  // The window that was judged, not everything the warehouse holds. These differ whenever the
  // period selector is set to less than the loaded range, and showing the wider one next to a
  // count derived from the narrower one made the two disagree on screen.
  const from = d.window_from || d.from;
  const to = d.window_to || d.to;

  if (!d.anomalies.length) {
    $("tabBody").innerHTML = `
      ${kpiRow([
        { label: "Anomalies", value: "0", sub: "nothing unusual" },
        { label: "Period", value: from && to ? `${from} → ${to}` : "—",
          sub: `last ${d.days || days} days` },
      ])}
      <section class="card">
        <h3>Nothing stands out</h3>
        <p class="muted">${esc(d.note || "")}</p>
      </section>`;
    return;
  }

  $("tabBody").innerHTML = `
    ${d.high ? `<div class="banner err"><strong>${d.high} high-impact
      ${d.high === 1 ? "change" : "changes"}</strong> in the last ${d.days || days} days.</div>` : ""}

    ${kpiRow([
      { label: "Anomalies", value: String(d.count), sub: `over ${esc(from || "")} → ${esc(to || "")}` },
      { label: "Increases", value: String(d.increases), cls: d.increases ? "up" : "",
        sub: `${esc(money(d.impact, cur, 2))} above baseline` },
      { label: "Decreases", value: String(d.decreases), cls: "down",
        sub: "spend that stopped" },
      { label: "High impact", value: String(d.high || 0),
        cls: d.high ? "up" : "", sub: d.high ? "needs attention" : "none" },
    ])}

    <div id="anomChart" class="card chart-card"></div>

    <section class="card">
      <div class="tagpick-head">
        <h3>What changed</h3>
        <span class="muted note">compared with a rolling 14-day median</span>
      </div>
      <p class="muted note">Each row is one day that broke its own pattern. The last
        ${esc(String(d.daily?.length && d.settled_to ? "two days" : "few days"))} are excluded —
        Azure restates cost data for a while after the fact, and a spike that resolves itself is
        worse than no alert at all.</p>
      <div class="anoms">${d.anomalies.map(anomalyCard).join("")}</div>
    </section>`;

  // The estate's own daily line, so a flagged day can be seen in context rather than trusted.
  if (d.daily?.length) {
    const flagged = new Set(d.anomalies.map((a) => a.day));
    drawChart($("anomChart"), {
      title: `Daily spend — flagged days marked`,
      type: "line",
      labels: d.daily.map((x) => x.day),
      datasets: [{ label: "Daily cost", data: d.daily.map((x) => x.cost) }],
      pointColors: d.daily.map((x) => (flagged.has(x.day) ? "up" : null)),
      currency: cur,
    });
  }
}

/**
 * The head every status card shares: a name, a qualifier under it, a figure and a pill.
 *
 * Budgets, anomalies and schedules had three copies of this markup with different class names,
 * which is three places to change when the shape does — and three chances for them to drift
 * apart. They are the same object visually and semantically: a thing, what it is, how it is
 * doing.
 */
function cardHead({ name, sub, figure, figureClass = "", status, statusClass = "" }) {
  return `<div class="anom-head">
    <div class="anom-id">
      <span class="anom-key">${esc(name || "—")}</span>
      ${sub ? `<span class="anom-sub">${esc(sub)}</span>` : ""}
    </div>
    <div class="anom-figs">
      ${figure ? `<span class="anom-delta ${esc(figureClass)}">${figure}</span>` : ""}
      ${status ? `<span class="bstatus ${esc(statusClass)}">${esc(status)}</span>` : ""}
    </div>
  </div>`;
}

function anomalyCard(a) {
  const up = a.direction === "up";
  const cur = a.currency;
  return `<article class="anom ${esc(a.severity)}">
    ${cardHead({
      name: a.key,
      sub: `${a.dimension} · ${a.day}`,
      figure: `${up ? "+" : ""}${esc(money(a.delta, cur, 2))}`,
      figureClass: up ? "up" : "down",
      status: a.severity,
      statusClass: a.severity,
    })}
    <div class="budget-meta">
      <span>${esc(money(a.baseline, cur, 2))} → <strong>${esc(money(a.cost, cur, 2))}</strong>
        on the day</span>
      <span>${a.percent == null ? "from nothing" : `${a.percent > 0 ? "+" : ""}${a.percent}%`}</span>
      <span>${a.score >= 50 ? "far outside" : `${a.score}×`} the usual variation</span>
    </div>
    ${a.also?.length ? `<p class="muted note">Also visible as
      ${a.also.map((x) => `${esc(x.dimension)} <strong>${esc(x.key)}</strong>`).join(", ")}.</p>` : ""}
  </article>`;
}

// ============================================================== cost exports

/**
 * The scheduled exports Azure writes on our behalf — created, run and deleted here.
 *
 * Why they are worth having: a refresh through the Cost Details API asks Azure to *generate* a
 * report and then waits — about forty seconds each, queued behind a per-subscription rate
 * limit, so minutes for a three-month load. A scheduled export inverts that. Azure writes the
 * data to blob storage overnight and a refresh becomes a blob read.
 *
 * The reason this is possible here at all is managed identity: the tenant blocks storage
 * account keys, which is how a Cost Management export normally authenticates, so an export
 * created without an identity fails. Everything created here gets one.
 *
 * This tab used to carry the raw-row export as well, under the name "Export settings". Two
 * unrelated things sharing a page that described neither: one creates a standing job in Azure,
 * the other hands you a file now. The file half moved to Export data, next to the report
 * builder, and what is left is named for what it is.
 */
async function renderSettings(token) {
  // All three together. `whoAmI` used to run after the others had landed, which added a round
  // trip to a tab that is already the slowest in the app — and it is needed for the same
  // render, so there is nothing to wait for.
  const [sched, arch, me] = await Promise.all([
    get("/api/schedules", { timeout: 45000 }).catch((e) => ({ error: e.message, schedules: [] })),
    get("/api/archive?limit=200", { timeout: 30000 }).catch(() => ({})),
    whoAmI().catch(() => null),
  ]);
  if (token !== loadToken) return;

  const admin = !!me?.user?.admin;
  const subs = sched.subscriptions || allSubscriptions();
  const mine = sched.schedules || [];

  $("tabBody").innerHTML = `
    ${sched.error ? `<div class="banner warn">${esc(sched.error)}</div>` : ""}

    <section class="card">
      <div class="tagpick-head">
        <h3>Scheduled exports</h3>
        <span class="muted note">${mine.length} configured</span>
      </div>
      <p class="muted note">A refresh through the Cost Details API waits for Azure to build a
        report — roughly forty seconds each, queued behind a per-subscription rate limit, so a
        three-month load takes minutes. A scheduled export turns that around: Azure writes the
        data to
        <code>${esc(arch.account || "storage")}/${esc(arch.container || "")}</code> on its own
        timetable, and the refresh becomes a blob read.</p>
      <p class="muted note">Each export authenticates with its own managed identity, because
        this tenant blocks storage account keys — the usual method — outright.</p>

      ${admin ? `
        <div class="budget-grid">
          <label class="bfield"><span>Subscription</span>
            <select id="sSub">${subs.map((s) =>
              `<option value="${esc(s.id)}">${esc(s.name || s.id)}</option>`).join("")}</select>
          </label>
          <label class="bfield"><span>Report</span>
            <select id="sMetric">
              <option value="FocusCost">FOCUS — actual and amortized in one file</option>
              <option value="AmortizedCost">Amortized — what the dashboard uses</option>
              <option value="ActualCost">Actual — billed on the day</option>
            </select>
          </label>
          <label class="bfield"><span>How often</span>
            <select id="sEvery">
              <option value="Daily">Daily</option>
              <option value="Weekly">Weekly</option>
              <option value="Monthly">Monthly</option>
            </select>
          </label>
        </div>
        <p class="muted note">FOCUS is the one schema that carries actual and amortized cost
          together, so a single export answers both questions — and it is what Refresh prefers
          when it finds one. Azure fills in the FOCUS version itself.</p>
        <div class="build-bar">
          <button class="primary" id="sCreate">Create schedule</button>
          <span class="muted" id="sNote"></span>
        </div>`
        : `<p class="muted note">Creating a schedule writes to Azure, so it is limited to
             admins.</p>`}

      <!-- The form first, the list second. The action is why someone opens this tab; the list
           is the record of having taken it. With fourteen schedules the form sat below a
           screenful of cards, so the one control on the page was the one thing you had to go
           looking for — and after creating one, the confirming render put the result at the
           top where it is already in view. -->
      ${mine.length ? `<h4 class="sub-head">Existing schedules</h4>
        <div class="schedules">${mine.map(scheduleRow).join("")}</div>`
        : `<p class="muted empty">Nothing scheduled yet.</p>`}
    </section>`;

  wireSettings();
}

/**
 * The archived days available as an export source, newest first.
 *
 * Grouped by dataset because "2026-08-27" alone is ambiguous once more than one report type is
 * archived — an amortized snapshot and an actual one from the same day are different numbers.
 */
function snapshotOptions(arch) {
  const days = arch.days || {};
  const label = { focus: "FOCUS", amortized: "Amortized", actual: "Actual" };
  return Object.keys(days)
    .sort()
    .map((set) => `<optgroup label="${esc(label[set] || set)}">${
      days[set].map((d) => `<option value="${esc(d)}" data-set="${esc(set)}">${esc(d)}</option>`)
        .join("")}</optgroup>`)
    .join("");
}

function scheduleRow(s) {
  const managed = s.identity;
  return `<article class="sched ${s.status === "Active" ? "on" : ""}">
    ${cardHead({
      name: s.name,
      sub: s.subscription,
      status: s.status || "unknown",
      statusClass: s.status === "Active" ? "ok" : "",
    })}
    <div class="budget-meta">
      <span><strong>${esc(s.metric || "—")}</strong> · ${esc(s.recurrence || "—")}</span>
      <span>→ ${esc(s.account || "?")}/${esc(s.container || "?")}/${esc(s.folder || "")}</span>
      <span>${managed ? "managed identity" : `<span class="no-alerts">no identity — cannot write
        to a key-disabled account</span>`}</span>
    </div>
    <div class="budget-foot">
      <span class="muted">${[
        s.starts ? `from ${String(s.starts).slice(0, 10)}` : "",
        // Azure returns this and it used to be dropped. On a daily export it is the single
        // most useful fact about a schedule: when it next produces something.
        s.next_run ? `next ${String(s.next_run).slice(0, 10)}` : "",
      ].filter(Boolean).map(esc).join(" · ")}</span>
      <span>
        <button type="button" class="linkish" data-run="${esc(s.name)}"
                data-sub="${esc(s.subscription_id)}">Run now</button>
        <button type="button" class="linkish" data-del="${esc(s.name)}"
                data-sub="${esc(s.subscription_id)}">Delete</button>
      </span>
    </div>
  </article>`;
}

/**
 * The raw-data export at the foot of the Export data tab.
 *
 * Separate from `wireSettings` because the section it drives is: this moved to sit under the
 * report builder, where someone looking for "get me the rows" actually goes, and the schedules
 * tab kept only the things that create and run exports in Azure.
 */
function wireSelectionExport() {
  const ticked = () =>
    [...$("tabBody").querySelectorAll("#xSubs input:checked")].map((i) => i.value);

  const setAll = (on) => {
    for (const i of $("tabBody").querySelectorAll("#xSubs input")) i.checked = on;
    paintExportNote();
  };
  // Guarded throughout: this runs on every render of the tab, and a section that failed to
  // draw — or a future tab that wires this without one — must not throw and blank everything
  // above it, which is how a working report builder used to disappear.
  const all = $("xAll");
  if (all) all.onclick = () => setAll(true);
  const none = $("xNone");
  if (none) none.onclick = () => setAll(false);
  for (const i of $("tabBody").querySelectorAll("#xSubs input")) i.onchange = paintExportNote;

  function selectionQuery() {
    const src = $("xSource");
    const opt = src?.selectedOptions?.[0];
    return `scope=${encodeURIComponent(ticked().join(","))}` +
      `&fmt=${encodeURIComponent($("xFmt")?.value || "csv")}` +
      `&since=${encodeURIComponent($("xFrom")?.value || "")}` +
      `&until=${encodeURIComponent($("xTo")?.value || "")}` +
      `&source=${encodeURIComponent(src?.value || "")}` +
      `&dataset=${encodeURIComponent(opt?.dataset?.set || "actual")}` +
      `&label=${encodeURIComponent($("xLabel")?.value || "")}`;
  }

  function paintExportNote() {
    const note = $("xNote");
    const btn = $("xExport");
    const dl = $("xDownload");
    const n = ticked().length;
    const from = $("xFrom")?.value;
    const to = $("xTo")?.value;
    const bad = from && to && from > to;
    // The button is the thing that would otherwise fail server-side with "select at least one",
    // so it says no first.
    if (btn) btn.disabled = n === 0 || bad;
    // A link has no disabled state, so an unusable selection has to lose its destination
    // instead — the same treatment the report builder's download links get, and for the same
    // reason: a link that is still clickable would download an error page.
    if (dl) {
      const off = n === 0 || bad;
      dl.classList.toggle("disabled", off);
      dl.setAttribute("aria-disabled", String(off));
      // Removing href is what actually stops it: an anchor with no href is not focusable and
      // does nothing when clicked, so the control cannot be reached round the back by keyboard
      // while it looks unavailable.
      if (off) dl.removeAttribute("href");
      else wireDownloadLink(dl, `/api/archive/export/download?${selectionQuery()}`);
    }
    if (note) {
      note.textContent = bad
        ? "The start date must be on or before the end date."
        : n
          ? `${n} subscription${n === 1 ? "" : "s"}${from || to
            ? `, ${from || "start"} → ${to || "end"}` : ""}`
          : "Tick at least one subscription.";
    }
  }
  // The label is typed rather than picked, so it needs `input` too — the download URL carries it
  // and would otherwise keep whatever the label was when the tab rendered.
  for (const id of ["xFrom", "xTo", "xSource", "xFmt"]) {
    const el = $(id);
    if (el) el.onchange = paintExportNote;
  }
  const lbl = $("xLabel");
  if (lbl) lbl.oninput = paintExportNote;
  paintExportNote();

  const exp = $("xExport");
  if (exp) {
    exp.onclick = async () => {
      const chosen = ticked();
      if (!chosen.length) return;
      exp.disabled = true;
      const before = exp.textContent;
      const note = $("xNote");
      exp.textContent = "Saving…";
      if (note) note.textContent = "Reading the warehouse and writing to storage…";
      try {
        const res = await fetch(`/api/archive/export?${selectionQuery()}`, { method: "POST" });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          toast(typeof d.detail === "string" ? d.detail : "Could not export.", "err");
          return;
        }
        const kb = Math.round((d.bytes || 0) / 1024).toLocaleString();
        const span = d.since || d.until ? ` for ${d.since || "start"} → ${d.until || "end"}` : "";
        toast(`Exported ${(d.rows || 0).toLocaleString()} rows${span} (${kb} KB) to ${d.name}.`);
      } finally {
        exp.disabled = false;
        exp.textContent = before;
        paintExportNote();
      }
    };
  }
}

/** Creating, running and deleting the scheduled exports on the Cost exports tab. */
function wireSettings() {
  const create = $("sCreate");
  if (create) {
    create.onclick = async () => {
      const note = $("sNote");
      create.disabled = true;
      const before = create.textContent;
      create.textContent = "Creating…";
      if (note) note.textContent = "Azure creates the identity, then it is granted access…";
      try {
        const res = await fetch("/api/schedules", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            subscription: $("sSub").value,
            metric: $("sMetric").value,
            recurrence: $("sEvery").value,
            run_now: true,
          }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) {
          toast(typeof d.detail === "string" ? d.detail : `Azure refused it (${res.status}).`, "err");
          return;
        }
        toast(`${d.name} scheduled — ${d.storage_access}${
          d.first_run_started ? ", first run started" : ""}.`);
        renderSettings(loadToken);
      } catch {
        toast("Could not reach the server.", "err");
      } finally {
        create.disabled = false;
        create.textContent = before;
        if (note) note.textContent = "";
      }
    };
  }

  for (const b of $("tabBody").querySelectorAll("[data-run]")) {
    b.onclick = async () => {
      b.disabled = true;
      const q = `subscription=${encodeURIComponent(b.dataset.sub)}&name=${encodeURIComponent(b.dataset.run)}`;
      const res = await fetch(`/api/schedules/run?${q}`, { method: "POST" });
      const d = await res.json().catch(() => ({}));
      toast(res.ok ? `${b.dataset.run} started. It writes to storage in a minute or two.`
        : (d.detail || "Could not start it."), res.ok ? "ok" : "err");
      b.disabled = false;
    };
  }

  for (const b of $("tabBody").querySelectorAll("[data-del]")) {
    b.onclick = async () => {
      const q = `subscription=${encodeURIComponent(b.dataset.sub)}&name=${encodeURIComponent(b.dataset.del)}`;
      const res = await fetch(`/api/schedules?${q}`, { method: "DELETE" });
      const d = await res.json().catch(() => ({}));
      if (res.ok) {
        toast(`${b.dataset.del} deleted. Data it already wrote is untouched.`);
        renderSettings(loadToken);
      } else {
        toast(d.detail || "Could not delete it.", "err");
      }
    };
  }
}

// ================================================================ history

/**
 * What Azure changed its mind about.
 *
 * The warehouse only ever holds the latest read, and Azure restates cost data for days after
 * the fact — so the dashboard physically cannot answer "what did we think this cost when we
 * reported it". Two archived snapshots can, which is the reason they are kept.
 *
 * Deliberately framed as restatement rather than as change: both figures describe the *same*
 * period. A difference here is not spend that happened, it is spend that was already there and
 * has only now been counted.
 */
let historyState = null;

async function renderHistory(token) {
  const a = await get("/api/archive?limit=200", { timeout: 45000 });
  if (token !== loadToken) return;

  if (!a.enabled || !a.reachable) {
    $("tabBody").innerHTML = `<section class="card">
      <h3>No archive to compare against</h3>
      <div class="banner warn">${esc(a.error || "The archive is not configured.")}</div>
      <p class="muted">Every refresh writes a dated snapshot. Once two exist for the same
        dataset, this tab shows what moved between them.</p></section>`;
    return;
  }

  const sets = a.comparable || [];
  if (!sets.length) {
    const have = Object.entries(a.days || {})
      .map(([k, v]) => `${k} (${v.length} day${v.length === 1 ? "" : "s"})`).join(", ");
    $("tabBody").innerHTML = `<section class="card">
      <h3>Not enough history yet</h3>
      <p class="muted">A comparison needs two snapshots of the same dataset on different days.
        The archive currently holds ${esc(have || "nothing")}. Refresh again tomorrow and this
        tab will show what Azure restated overnight.</p></section>`;
    return;
  }

  const pick = historyState && sets.includes(historyState.dataset)
    ? historyState
    : { dataset: sets[0], group_by: "ServiceName" };
  const days = a.days[pick.dataset] || [];
  historyState = {
    ...pick,
    later: days.includes(pick.later) ? pick.later : days[0],
    earlier: days.includes(pick.earlier) && pick.earlier !== days[0] ? pick.earlier : days[1],
    _days: a.days,
    _sets: sets,
  };

  $("tabBody").innerHTML = historyMarkup(a);
  wireHistory();
  await loadComparison(token);
}

const HISTORY_DIMENSIONS = [
  ["ServiceName", "Service"],
  ["SubAccountName", "Subscription"],
  ["ResourceGroup", "Resource group"],
  ["ResourceName", "Resource"],
  ["RegionName", "Region"],
  ["MeterName", "Meter"],
];

function historyMarkup(a) {
  const s = historyState;
  const days = a.days[s.dataset] || [];
  const label = { focus: "FOCUS", amortized: "Amortized", actual: "Actual" };

  return `
    <section class="card">
      <div class="tagpick-head">
        <h3>What Azure restated</h3>
        <span class="muted note">${a.count} snapshot(s) in ${esc(a.account)}/${esc(a.container)}</span>
      </div>
      <p class="muted note">Both columns describe the <strong>same period</strong>, read on two
        different days. Azure revises cost data for several days after the fact, so a difference
        here is not new spend — it is spend that was always there and has only now been counted.
        This is the one question the dashboard cannot answer, because it only ever holds the
        latest read.</p>

      <div class="budget-grid">
        <label class="bfield"><span>Dataset</span>
          <select id="hSet">${s._sets.map((k) =>
            `<option value="${esc(k)}" ${k === s.dataset ? "selected" : ""}>${esc(label[k] || k)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Read on</span>
          <select id="hEarlier">${days.map((d) =>
            `<option value="${esc(d)}" ${d === s.earlier ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Compared with</span>
          <select id="hLater">${days.map((d) =>
            `<option value="${esc(d)}" ${d === s.later ? "selected" : ""}>${esc(d)}</option>`).join("")}</select>
        </label>
        <label class="bfield"><span>Broken down by</span>
          <select id="hGroup">${HISTORY_DIMENSIONS.map(([v, t]) =>
            `<option value="${esc(v)}" ${v === s.group_by ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>
        </label>
      </div>
    </section>
    <div id="hResult"></div>`;
}

function wireHistory() {
  const set = $("hSet");
  if (set) {
    set.onchange = () => {
      // Changing dataset invalidates the day choices, so they are re-derived rather than kept.
      historyState = { dataset: set.value, group_by: historyState.group_by,
                       _days: historyState._days, _sets: historyState._sets };
      renderHistory(loadToken);
    };
  }
  for (const id of ["hEarlier", "hLater", "hGroup"]) {
    const el = $(id);
    if (!el) continue;
    el.onchange = () => {
      historyState = {
        ...historyState,
        earlier: $("hEarlier").value,
        later: $("hLater").value,
        group_by: $("hGroup").value,
      };
      loadComparison(loadToken);
    };
  }
}

async function loadComparison(token) {
  const host = $("hResult");
  if (!host) return;
  const s = historyState;
  if (!s.earlier || !s.later || s.earlier === s.later) {
    host.innerHTML = `<section class="card"><p class="muted empty">Pick two different days.</p></section>`;
    return;
  }
  host.innerHTML = `<section class="card"><p class="muted empty">Reading both snapshots…</p></section>`;
  try {
    const q = `dataset=${encodeURIComponent(s.dataset)}&earlier=${encodeURIComponent(s.earlier)}` +
      `&later=${encodeURIComponent(s.later)}&group_by=${encodeURIComponent(s.group_by)}&top=20`;
    const d = await get(`/api/archive/compare?${q}`, { timeout: 60000 });
    if (token !== loadToken) return;
    host.innerHTML = comparisonMarkup(d);
    wireTables(host);
  } catch (err) {
    host.innerHTML = `<section class="card">
      <div class="banner err">${esc(err.message || String(err))}</div></section>`;
  }
}

function comparisonMarkup(d) {
  const cur = d.currency;
  const up = d.total_delta > 0;
  const same = Math.abs(d.total_delta) < 0.005;

  return `
    ${kpiRow([
      { label: `Total as read ${d.earlier.day}`, value: esc(money(d.earlier.cost, cur)),
        sub: `${d.earlier.rows.toLocaleString()} rows` },
      { label: `Total as read ${d.later.day}`, value: esc(money(d.later.cost, cur)),
        sub: `${d.later.rows.toLocaleString()} rows` },
      { label: "Restated by",
        value: `${up ? "+" : ""}${esc(money(d.total_delta, cur, 2))}`,
        cls: same ? "" : up ? "up" : "down",
        sub: d.total_percent == null ? "" : `${d.total_percent > 0 ? "+" : ""}${d.total_percent}%` },
      { label: "Rows added", value: `${d.row_delta > 0 ? "+" : ""}${d.row_delta.toLocaleString()}`,
        sub: "usage records that arrived late" },
    ])}

    ${same ? `<div class="banner ok">Nothing moved. The two reads of
      ${esc(d.earlier.from || "")} → ${esc(d.earlier.to || "")} agree exactly, so this period has
      settled.</div>` : ""}

    ${d.changes.length
      ? table(`Biggest restatements by ${esc((HISTORY_DIMENSIONS.find((x) => x[0] === d.group_by) || [, d.group_by])[1])}`,
          d.changes,
          [
            { label: (HISTORY_DIMENSIONS.find((x) => x[0] === d.group_by) || [, "Key"])[1],
              cell: (r) => esc(r.key || "—") },
            { label: `As read ${d.earlier.day}`, num: true, value: (r) => r.was,
              cell: (r) => moneyHtml(r.was, cur, 2) },
            { label: `As read ${d.later.day}`, num: true, value: (r) => r.now,
              cell: (r) => moneyHtml(r.now, cur, 2) },
            { label: "Change", num: true, value: (r) => r.delta,
              cell: (r) => `<span class="delta ${r.delta > 0 ? "up" : "down"}">${
                r.delta > 0 ? "+" : ""}${esc(money(r.delta, cur, 2))}</span>` },
            { label: "%", num: true, value: (r) => r.percent ?? 0,
              cell: (r) => r.percent == null ? "—"
                : `<span class="delta ${r.percent > 0 ? "up" : "down"}">${
                  r.percent > 0 ? "+" : ""}${r.percent}%</span>` },
          ],
          "Only rows that actually moved are listed. A resource that appears in one read and not "
          + "the other shows the missing side as zero.")
      : ""}`;
}

// ================================================================ budgets

/**
 * Every budget in scope, worst first.
 *
 * Budgets had no home in this app: they could be created from the Tags tab and then only ever
 * seen again in the portal, which makes the one question a budget exists to answer — "am I
 * over?" — a trip to another product. The list is read live because budgets are not in a cost
 * export, and a stale "you're fine" is the one answer this page must never give.
 */
async function renderBudgets(token) {  const d = await get(`/api/budgets${budgetScopeQs()}`, { timeout: 45000 });
  if (token !== loadToken) return;

  const rows = d.budgets || [];
  if (!rows.length) {
    $("tabBody").innerHTML = `
      <section class="card">
        <h3>No budgets on the subscriptions in scope</h3>
        ${d.error ? `<div class="banner warn">${esc(d.error)}</div>` : ""}
        <p class="muted">A budget is how Azure tells you spend has moved before the invoice
          does. Create one from a tag selection on the
          <button type="button" class="linkish" data-goto="tags">Cost by Application /
          Tags</button> tab — the filter is built from what you have already selected there.</p>
      </section>`;
    wireBudgetLinks();
    return;
  }

  const cur = rows[0].currency;
  const totalAmount = rows.reduce((t, b) => t + (b.amount || 0), 0);
  const totalSpend = rows.reduce((t, b) => t + (b.current_spend || 0), 0);
  const over = rows.filter((b) => b.status === "over");
  const near = rows.filter((b) => b.status === "near");
  // Mixed currencies cannot be added. Saying so beats printing a total that is not a quantity.
  const mixed = new Set(rows.map((b) => b.currency).filter(Boolean)).size > 1;

  $("tabBody").innerHTML = `
    ${over.length ? `<div class="banner err"><strong>${over.length} budget${
      over.length === 1 ? " is" : "s are"} over.</strong> ${esc(
        over.slice(0, 3).map((b) => `${b.name} at ${b.percent_used}%`).join(", "))}${
      over.length > 3 ? ` and ${over.length - 3} more` : ""}.</div>` : ""}

    ${kpiRow([
      { label: "Budgets", value: String(rows.length),
        sub: `across ${new Set(rows.map((b) => b.subscription)).size} subscription(s)` },
      { label: "Over budget", value: String(over.length),
        cls: over.length ? "up" : "", sub: over.length ? "needs attention" : "none over" },
      { label: "Near the limit", value: String(near.length),
        sub: "at or past 80%" },
      { label: "Budgeted", value: mixed ? "—" : esc(money(totalAmount, cur)),
        sub: mixed ? "mixed currencies" : `${esc(money(totalSpend, cur, 2))} spent so far` },
    ])}

    <section class="card">
      <div class="tagpick-head">
        <h3>Budgets</h3>
        <button type="button" class="linkish" data-goto="tags">Create one from tags</button>
      </div>

      <!-- Ten budgets is already more than fits on a screen, and on a large estate it is
           dozens. The question someone opens this tab with is almost never "show me all of
           them" — it is "which ones need me", or "the one called X". Both are one click or a
           few keystrokes away rather than a scroll. -->
      <div class="budget-tools">
        <div class="seg" role="group" aria-label="Filter budgets">
          ${[["all", "All", rows.length],
             ["attention", "Needs attention", over.length + near.length],
             ["over", "Over", over.length],
             ["near", "Near", near.length],
             ["ok", "On track", rows.filter((b) => b.status === "ok").length]]
            .map(([id, label, n]) => `<button type="button" class="seg-btn${
              budgetFilter.status === id ? " on" : ""}" data-bstatus="${id}"
              ${n ? "" : "disabled"} aria-pressed="${budgetFilter.status === id}">${
              esc(label)} <span class="seg-n">${n}</span></button>`).join("")}
        </div>
        <input id="budgetFind" type="search" class="tagfind" placeholder="Find a budget…"
               value="${esc(budgetFilter.q)}" autocomplete="off" spellcheck="false"
               aria-label="Find a budget by name or subscription">
      </div>

      <p class="muted note">Spend is the current period as Azure last computed it, which lags
        real usage by up to a day. Reset shows when the counter goes back to zero.</p>
      <div class="budgets" id="budgetList"></div>
    </section>`;

  wireBudgetLinks();

  for (const btn of $("tabBody").querySelectorAll("[data-bstatus]")) {
    btn.onclick = () => {
      budgetFilter.status = btn.dataset.bstatus;
      for (const other of $("tabBody").querySelectorAll("[data-bstatus]")) {
        const on = other.dataset.bstatus === budgetFilter.status;
        other.classList.toggle("on", on);
        other.setAttribute("aria-pressed", String(on));
      }
      paintBudgetList(rows);
    };
  }
  const find = $("budgetFind");
  if (find) {
    find.oninput = () => {
      budgetFilter.q = find.value.trim().toLowerCase();
      paintBudgetList(rows);
    };
  }

  paintBudgetList(rows);
}

/**
 * Which budgets are on screen. Kept outside the render so a filter survives the tab being
 * left and returned to — losing it on every visit is what makes a filter not worth using.
 */
let budgetFilter = { status: "all", q: "" };

/**
 * Draw the filtered list.
 *
 * Separate from `renderBudgets` because filtering must not refetch: the budgets come from a
 * live Azure call that takes seconds, and re-running it to hide four cards would make the
 * control feel broken. The data is already here; this is only ever a redraw.
 */
function paintBudgetList(rows) {
  const host = $("budgetList");
  if (!host) return;

  const q = budgetFilter.q;
  const wanted = rows.filter((b) => {
    const byStatus = budgetFilter.status === "all"
      || (budgetFilter.status === "attention"
        ? b.status === "over" || b.status === "near"
        : b.status === budgetFilter.status);
    if (!byStatus) return false;
    if (!q) return true;
    // Name, subscription and what it tracks: three ways someone might recognise a budget, and
    // the filter text is as likely to be a tag as a name.
    return [b.name, b.subscription, b.filter]
      .filter(Boolean).some((s) => String(s).toLowerCase().includes(q));
  });

  if (!wanted.length) {
    host.innerHTML = `<p class="muted empty">No budget matches${
      q ? ` “${esc(q)}”` : ""}${budgetFilter.status === "all" ? "" : " in this state"}.</p>`;
    return;
  }

  // The index is into the *unfiltered* list, so the chart and the tag link still find their
  // own budget after a filter has reordered what is on screen.
  host.innerHTML = wanted.map((b) => budgetCard(b, rows.indexOf(b))).join("");

  for (const btn of host.querySelectorAll("[data-tagsfor]")) {
    btn.onclick = () => showTagsFor(rows[Number(btn.dataset.tagsfor)]?.tag_filter, rows[Number(btn.dataset.tagsfor)]?.name);
  }
  for (const el of host.querySelectorAll("[data-goto]")) {
    el.onclick = () => selectTab(el.dataset.goto);
  }
  // After the markup, because each chart needs its host in the document. Drawn per budget
  // rather than as one combined chart: budgets have different limits, different periods and
  // sometimes different currencies, so stacking them on one axis would compare quantities
  // that are not comparable.
  for (const chart of host.querySelectorAll(".budget-chart")) {
    drawBudgetChart(chart, rows[Number(chart.dataset.chart)]);
  }
}

/** The budget list follows the scope picker, like every other tab. */
function budgetScopeQs() {
  const scope = scopeIds();
  const cur = readStore("costCurrency", "");
  const parts = [
    scope.length ? `scope=${encodeURIComponent(scope.join(","))}` : "",
    cur ? `currency=${encodeURIComponent(cur)}` : "",
  ].filter(Boolean);
  return parts.length ? `?${parts.join("&")}` : "";
}

function wireBudgetLinks() {
  for (const b of $("tabBody").querySelectorAll("[data-goto]")) {
    b.onclick = () => selectTab(b.dataset.goto);
  }
}

/**
 * One budget, as a bar rather than a row of numbers.
 *
 * A percentage in a table is read; a bar that has run past its end is *seen*. Over budget gets
 * red and an explicit label, and the overflow is drawn as a distinct segment past the 100%
 * mark, so "20% over" is a visible quantity rather than a number above 100 that has to be
 * mentally subtracted from itself.
 */
function budgetCard(b, idx) {
  const pct = b.percent_used;
  const status = b.status || "unknown";
  const cur = b.currency;
  // Under budget, the track is the budget and the fill is what has been spent. Over budget,
  // the track becomes the *spend* — the solid part is the budget, the hatched part is the
  // overspend — so 199% draws as a bar that is half overrun instead of a full bar identical
  // to one sitting at exactly 100%.
  const blown = pct != null && pct > 100;
  const fill = pct == null ? 0 : blown ? (100 / pct) * 100 : pct;
  const spill = blown ? 100 - fill : 0;

  const label = { over: "Over budget", near: "Near the limit", ok: "On track",
                  unknown: "No spend reported" }[status];

  return `<article class="budget ${esc(status)}">
    <div class="budget-head">
      <div class="budget-id">
        <span class="budget-name">${esc(b.name || "—")}</span>
        <span class="budget-sub">${esc(b.subscription || "")}</span>
      </div>
      <div class="budget-figs">
        <span class="budget-pct">${pct == null ? "—" : `${pct}%`}</span>
        <span class="bstatus ${esc(status)}">${esc(label)}</span>
      </div>
    </div>

    <div class="bmeter" role="img"
         aria-label="${esc(`${b.name}: ${pct == null ? "no spend reported" : `${pct}% of budget used`}`)}">
      <span class="fill" style="width:${fill}%"
            title="${esc(`Budget: ${money(b.amount, cur)}`)}"></span>
      ${spill ? `<span class="spill" style="width:${spill}%"
            title="${esc(`Over by ${money(-(b.remaining || 0), cur, 2)}`)}"></span>` : ""}
    </div>

    <div class="budget-meta">
      <span><strong>${esc(money(b.current_spend || 0, cur, 2))}</strong> of
        ${esc(money(b.amount, cur))} ${esc(String(b.time_grain || "").toLowerCase())}</span>
      <span>${b.remaining == null ? ""
        : b.remaining >= 0
          ? `${esc(money(b.remaining, cur, 2))} left`
          : `<span class="over-by">${esc(money(-b.remaining, cur, 2))} over</span>`}</span>
      <span>${b.alerts ? `Alerts at ${b.alerting.slice(0, 4).map((t) => `${t}%`).join(", ")}${
        b.alerting.length > 4 ? ` +${b.alerting.length - 4}` : ""}`
        : `<span class="no-alerts">No alerts set</span>`}</span>
    </div>

    ${b.filter ? `<div class="budget-filter sm"><span class="k">Tracks</span>
      <span class="v">${esc(b.filter)}</span>${
        (b.tag_filter || []).length ? `<button type="button" class="linkish"
          data-tagsfor="${esc(idx)}">See what is in it</button>` : ""}</div>`
      : `<p class="muted note">Tracks all spend on the subscription.</p>`}

    ${b.trend ? `<div class="budget-chart" data-chart="${esc(idx)}"></div>` : ""}

    <div class="budget-foot">
      <span class="muted">${b.start && b.end ? esc(`${b.start} → ${b.end}`) : ""}</span>
      <a class="linkish" href="${budgetPortalUrl(b.subscription_id)}" target="_blank"
         rel="noopener">Edit in the portal</a>
    </div>
  </article>`;
}


/**
 * The shape behind a budget's one number.
 *
 * Azure reports a budget as a single figure with no history, so the meter above can say "93%"
 * without answering the question that actually follows: is that a steady climb that lands just
 * inside the limit, or did it breach three weeks ago and keep going? Cumulative spend against
 * a flat budget line answers both, and where the two cross is the day it went over.
 *
 * Cumulative rather than the daily bars the Overview draws, because a budget is a running
 * total by definition — a daily line makes you integrate it in your head to answer the only
 * question the tab exists for.
 */
function drawBudgetChart(host, b) {
  const t = b.trend;
  if (!host || !t || !(t.labels || []).length) return;

  const cur = b.currency;
  const amount = b.amount;
  const spent = t.total;

  // The warehouse and Azure are two sources for the same money and will differ a little: the
  // warehouse holds the last refresh, Azure's figure is live. A small gap is expected and not
  // worth a caption. A large one means the chart and the headline above it genuinely disagree,
  // and saying so is better than letting someone find it themselves.
  const azure = b.current_spend;
  const gap = azure && spent ? Math.abs(spent - azure) / azure : 0;
  const notes = [];
  if (!t.filtered_exactly) {
    notes.push("This budget filters on something the local data cannot reproduce exactly, so " +
               "the line covers the whole subscription and will read high.");
  } else if (gap > 0.15) {
    notes.push(`Local cost data totals ${money(spent, cur, 2)} for this period against ` +
               `Azure's ${money(azure, cur, 2)} — refresh to close the gap.`);
  }
  if (t.labels.length) {
    notes.push(`${t.labels[0]} to ${t.labels[t.labels.length - 1]}.`);
  }

  const GRAIN_WORD = { Monthly: "month", Quarterly: "quarter", Annually: "year" };

  drawChart(host, {
    type: "area",
    title: `Spend so far this ${GRAIN_WORD[b.time_grain] || "period"}`,
    // Shorter than a standalone chart. Ten budgets at the default height make a page five
    // thousand pixels long, and nobody scrolls to the tenth one — the shape of a cumulative
    // line against a flat limit reads perfectly well at this size.
    height: 150,
    labels: t.labels,
    datasets: [
      { label: "Spent", data: t.cumulative, pointLabels: t.labels },
      // A flat reference at the limit. Dashed and unlabelled-by-hue for the same reason the
      // Overview's previous-period line is: it is the backdrop being read against, not a
      // second measurement.
      { label: `Budget ${money(amount, cur)}`,
        data: t.labels.map(() => amount), dash: true },
    ],
    currency: cur,
    categorical: false,
    note: notes.join(" "),
  });
}


// --------------------------------------------------- what was actually created

/** Azure's own Budgets blade for a subscription, for anyone who wants to see it there. */
function budgetPortalUrl(sub) {
  const scope = encodeURIComponent(`/subscriptions/${sub}`);
  return `https://portal.azure.com/#view/Microsoft_Azure_CostManagement/Menu/~/budgets/scope/${scope}`;
}

/**
 * The budget as Azure now holds it.
 *
 * Deliberately the same facts the form asked for, read back from the response rather than from
 * the inputs: a budget is a PUT, so the amount that matters is the one Azure returned. The list
 * of every budget on that subscription is underneath because the write is an upsert — seeing
 * one row where two were expected is how you find out a name collided and overwrote something.
 */
function budgetResultMarkup(b) {
  const cur = b.currency || tagState.data?.currency;
  const alerts = (b.thresholds || []).length
    ? `${b.thresholds.map((t) => `${t}%`).join(", ")} to ${esc((b.emails || []).join(", ") || "nobody")}`
    : "none — this budget tracks spend but will not notify anyone";

  return `<section class="card budgetform budgetdone">
    <h3>Budget created</h3>
    <div class="banner ok"><strong>${esc(b.name)}</strong> now exists on Azure. It starts tracking
      the moment usage lands against the filter below — a budget created today shows
      ${esc(money(0, cur))} spent until the first usage records for these tags arrive, which is
      normal rather than a sign it did not take.</div>

    <div class="budget-filter">
      <span class="k">Filter</span>
      <span class="v">${esc(b.filter || "—")}</span>
    </div>

    <div class="budget-grid">
      <div class="bfield static"><span>Amount</span>
        <div class="bstatic">${esc(money(b.amount, cur))} ${esc(String(b.time_grain || "").toLowerCase())}</div></div>
      <div class="bfield static"><span>Spent so far</span>
        <div class="bstatic">${esc(money(b.current_spend || 0, cur, 2))}</div></div>
      <div class="bfield static"><span>Subscription</span>
        <div class="bstatic">${esc(subName(b.subscription))}</div></div>
      <div class="bfield static"><span>Starts</span><div class="bstatic">${esc(b.start || "—")}</div></div>
      <div class="bfield static"><span>Expires</span><div class="bstatic">${esc(b.end || "—")}</div></div>
      <div class="bfield static"><span>Alerts</span><div class="bstatic">${alerts}</div></div>
    </div>

    ${b.id ? `<p class="muted note">Resource <code>${esc(b.id)}</code></p>` : ""}

    <div class="build-bar">
      <button class="primary" id="bAgain">Create another</button>
      <button class="ghost" id="bDone">Done</button>
      <button type="button" class="linkish" data-goto="budgets">See all budgets</button>
      <a class="linkish" href="${budgetPortalUrl(b.subscription)}" target="_blank"
         rel="noopener">Open in the Azure portal</a>
    </div>

    <div class="budgetlist" id="bExisting">
      <h4>Budgets on this subscription</h4>
      <p class="muted note">Loading…</p>
    </div>
  </section>`;
}

/** A subscription's name if the picker knows it, otherwise the id — never nothing. */
function subName(id) {
  return (allSubscriptions().find((s) => s.id === id) || {}).name || id || "—";
}

function wireBudgetResult(made) {
  const again = $("bAgain");
  if (again) {
    again.onclick = () => {
      tagState.budget = { open: true };
      paintBudget();
      $("tagBudget")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    };
  }
  const done = $("bDone");
  if (done) {
    done.onclick = () => {
      tagState.budget = { open: false };
      paintBudget();
    };
  }
  loadSubBudgets(made.subscription, made.name);
  wireBudgetLinks();
}

/**
 * Every budget Azure holds on that subscription, read back live.
 *
 * Read rather than assumed: the created panel above is what we sent, and this is what Azure
 * has. If a name collided, the upsert replaced a budget someone else was relying on, and the
 * only way that becomes visible is by listing them.
 *
 * Cached on the state, because every tag click repaints this panel and a fresh ARM round trip
 * per click would rate-limit the tab for a list that cannot have changed in between.
 */
async function loadSubBudgets(sub, highlight) {
  const host = $("bExisting");
  if (!host) return;

  const cached = tagState.budget?.list;
  if (cached && cached.sub === sub) {
    host.innerHTML = subBudgetsMarkup(cached.data, highlight);
    return;
  }
  try {
    const data = await get(`/api/budgets?scope=${encodeURIComponent(sub)}`);
    if (tagState.budget?.created) tagState.budget.list = { sub, data };
    if ($("bExisting")) $("bExisting").innerHTML = subBudgetsMarkup(data, highlight);
  } catch (err) {
    if ($("bExisting")) {
      $("bExisting").innerHTML = `<h4>Budgets on this subscription</h4>
        <p class="muted note">Could not read them back: ${esc(err.message || String(err))}</p>`;
    }
  }
}

function subBudgetsMarkup(data, highlight) {
  const rows = (data.budgets || []).filter((b) => b.name);
  if (!rows.length) {
    return `<h4>Budgets on this subscription</h4>
      <p class="muted note">${esc(data.error
        ? `Could not read them back: ${data.error}`
        : "Azure reports none yet — a budget can take a moment to appear in the list.")}</p>`;
  }
  // The same cards the Budgets tab draws, so an over-budget neighbour is as obvious here as it
  // is there — and so there is one place to change when the treatment changes.
  return `<h4>Budgets on this subscription</h4>
    <div class="budgets">${rows.map((b) => b.name === highlight
      ? budgetCard(b).replace('<article class="budget ', '<article class="budget is-new ')
      : budgetCard(b)).join("")}</div>`;
}

const labelOf = (v) => (v === "" ? "(no value)" : v);

/** `YYYY-MM-DD` from a date's local parts, which is what a date input expects. */
function isoDay(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Client-side twin of the server's suggestion, so the field is filled before any round trip. */
function suggestBudget(perDay, grain) {
  const months = { Monthly: 1, Quarterly: 3, Annually: 12 }[grain] || 1;
  const projected = perDay * 30.4 * months;
  if (!(projected > 0)) return null;
  let step = 1;
  while (projected / step >= 100) step *= 10;
  return Math.ceil(projected / step) * step;
}

function wireBudget(sel) {
  const grain = $("bGrain");
  const amount = $("bAmount");
  const note = $("bSuggest");
  const perDay = sel.days ? sel.sum / sel.days : 0;

  // Changing the reset period changes what a sensible amount is. Left alone, a monthly figure
  // silently becomes an annual budget a twelfth the size it should be.
  if (grain && amount) {
    grain.onchange = () => {
      const next = suggestBudget(perDay, grain.value);
      if (next) {
        amount.value = next;
        if (note) {
          note.textContent =
            `Suggested ${money(next, sel.cur)} per ${grain.value.toLowerCase()
              .replace("ly", "")} period, from ${money(perDay, sel.cur)} a day.`;
        }
      }
    };
  }

  const create = $("bCreate");
  if (create) create.onclick = () => submitBudget(sel);

  whoAmI().then((me) => {
    const box = $("bEmails");
    if (box && !box.value && me?.user?.email) box.value = me.user.email;
  }).catch(() => {});
}

async function submitBudget(sel) {
  const btn = $("bCreate");
  const note = $("bNote");
  const sub = $("bSub")?.value;
  const name = $("bName")?.value.trim();
  const amount = Number($("bAmount")?.value);
  const grain = $("bGrain")?.value || "Monthly";
  const end = $("bEnd")?.value || "";
  const thresholds = ($("bThresholds")?.value || "")
    .split(/[,\s]+/).map((t) => Number(t)).filter((t) => t > 0);
  const emails = ($("bEmails")?.value || "").split(/[,;\s]+/).filter(Boolean);

  if (!sub) return toast("Choose a subscription for the budget.", "err");
  if (!name) return toast("Give the budget a name.", "err");
  if (!(amount > 0)) return toast("Enter a budget amount greater than zero.", "err");
  if (thresholds.length && !emails.length) {
    return toast("An alert threshold needs an email address to notify.", "err");
  }

  btn.disabled = true;
  const before = btn.textContent;
  btn.textContent = "Creating…";
  if (note) note.textContent = "";

  try {
    const res = await fetch("/api/budgets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        subscription: sub,
        name,
        amount,
        time_grain: grain,
        end,
        mode: sel.mode,
        tags: sel.tags.map((t) => ({ key: t.key, values: t.values })),
        thresholds,
        emails,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const why = typeof data.detail === "string" ? data.detail : `Azure refused it (${res.status}).`;
      if (note) note.textContent = why;
      toast(why, "err");
      return;
    }
    toast(`Budget “${data.name}” created — ${money(data.amount, sel.cur)} ${
      String(data.time_grain || "").toLowerCase()}, alerting ${data.notifications} threshold(s).`);
    // Keep the panel open and show what Azure returned. Closing the form on success threw away
    // the only record of the budget the moment the toast expired.
    tagState.budget = { open: true, created: data };
    paintBudget();
    $("tagBudget")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch {
    const why = "Could not reach the server to create the budget.";
    if (note) note.textContent = why;
    toast(why, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = before;
  }
}

/** Everything that depends on the selection. Pure arithmetic over data already in memory. */
function paintTags() {
  const d = tagState.data;
  const cur = d.currency;
  const picked = [...tagState.selected];
  const all = tagState.mode === "all";

  paintChips();
  paintTagValues();
  paintBudget();

  const narrowed = picked.filter((k) => (tagState.values.get(k) || new Set()).size);
  const narrowNote = narrowed.length
    ? ` Narrowed by value on ${narrowed.map((k) => `“${k}”`).join(", ")}.`
    : "";
  $("tagModeHint").textContent = (picked.length < 2
    ? "Pick one or more tags. Costs update as you choose."
    : all
      ? `Resources carrying all ${picked.length} selected tags.`
      : `Resources carrying at least one of the ${picked.length} selected tags.`) + narrowNote;

  // The selected line, kept separate from the picker so what is chosen is readable at a glance
  // rather than hidden among everything that was not.
  $("tagClear").hidden = picked.length === 0;
  $("tagSelected").innerHTML = picked.length
    ? picked
        .map((k) => {
          const wanted = tagState.values.get(k);
          const suffix = wanted && wanted.size
            ? ` = ${[...wanted].map((v) => (v === "" ? "(no value)" : v)).join(", ")}`
            : "";
          return `<button type="button" class="chip on removable" data-drop="${esc(k)}"
            aria-label="Remove ${esc(k)}"><span class="chip-k">${esc(k + suffix)}</span>
            <span class="chip-x" aria-hidden="true">×</span></button>`;
        })
        .join("")
    : `<p class="muted empty">Nothing selected — pick a tag above.</p>`;
  for (const b of $("tagSelected").querySelectorAll("[data-drop]")) {
    b.onclick = () => toggleTag(b.dataset.drop);
  }

  const matched = picked.length
    ? d.resources.filter((r) =>
        all ? picked.every((k) => matchesKey(r, k)) : picked.some((k) => matchesKey(r, k)))
    : [];
  const sum = matched.reduce((t, r) => t + r.cost, 0);

  $("tagSum").innerHTML = moneyHtml(sum, cur, 2);
  $("tagSumSub").textContent = picked.length
    ? `${matched.length.toLocaleString()} resource${matched.length === 1 ? "" : "s"} · ${
        d.total ? ((sum / d.total) * 100).toFixed(1) : "0"}% of all spend`
    : "nothing selected";

  // The trend covers the whole estate when nothing is picked, and the selection once there is
  // one. Drawn either way: a tab that shows no chart until you happen to click a tag hides the
  // one view most people came for.
  paintTagTrend(picked.length ? matched : null);
  paintTagDay(picked.length ? matched : null);

  if (!picked.length) {
    $("tagResults").innerHTML = "";
    return;
  }

  if (!matched.length) {
    $("tagResults").innerHTML = `<section class="card"><h3>Matching resources</h3>
      <p class="muted empty">No resource carries ${all ? "all" : "any"} of these tags${
        narrowed.length ? " with the values you have narrowed to" : ""}${
        all && picked.length > 1 ? " — try “Has any”." : "."}</p></section>`;
    return;
  }

  // What the money is actually going on, at two levels: the services, then the resources.
  const byService = new Map();
  for (const r of matched) {
    const k = r.service || "(unknown)";
    byService.set(k, (byService.get(k) || 0) + r.cost);
  }
  const services = [...byService.entries()]
    .map(([name, cost]) => ({ name, cost }))
    .sort((a, b) => b.cost - a.cost);

  $("tagResults").innerHTML =
    table(
      "By service",
      services,
      [
        { label: "Service", cell: (r) => esc(r.name) },
        { label: "Cost", num: true, value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
        { label: "Share", num: true, sort: false,
          cell: (r) => `${sum ? ((r.cost / sum) * 100).toFixed(1) : "0"}%` },
      ],
      null,
      { pageSize: 10 }
    ) +
    table(
      "Matching resources",
      matched.slice().sort((a, b) => b.cost - a.cost),
      [
        { label: "Resource", cell: (r) => esc(r.name) },
        { label: "Service", cell: (r) => esc(r.service || "—") },
        { label: "Resource group", cell: (r) => esc(r.group || "—") },
        { label: "Subscription", cell: (r) => esc(r.subscription || "—") },
        { label: "Tags", sort: false, cell: (r) => tagPills(r) },
        { label: "Cost", num: true, value: (r) => r.cost,
          cell: (r) => moneyHtml(r.cost, cur, 2),
          total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2) },
      ],
      null,
      { pageSize: 15 }
    );

  wireTables($("tagResults"));
}

/** Daily totals for a set of resources, aligned to `dates`. Sparse pairs summed into a series. */
function tagSeries(resources) {
  const d = tagState.data;
  const out = new Array((d.dates || []).length).fill(0);
  for (const r of resources) {
    for (const [i, c] of r.d || []) out[i] += c;
  }
  return out;
}

/**
 * Daily spend across the window.
 *
 * A bar per day rather than a line: these are discrete daily charges, and a line drawn between
 * them implies a continuous rate that was never measured. Bars also give the drill-down an
 * obvious target — clicking a day is a normal thing to do to a bar and a strange thing to do
 * to a point on a line.
 */
function paintTagTrend(matched) {
  const d = tagState.data;
  const host = $("tagTrend");
  if (!host) return;
  host.innerHTML = "";

  const dates = d.dates || [];
  if (!dates.length) return;

  if (d.daily_dropped) {
    host.innerHTML = `<section class="card"><h3>Daily spend</h3>
      <p class="muted empty">Too many resource-days in this window to chart without
      truncating them. Narrow the subscription scope or shorten the period.</p></section>`;
    return;
  }

  const series = matched ? tagSeries(matched) : (d.daily_total || []);
  const label = matched ? "Selected tags" : "All spend";

  drawChart(host, {
    type: "bar",
    title: `Daily spend · last ${dates.length} day${dates.length === 1 ? "" : "s"}`,
    labels: dates,
    datasets: [{ label, data: series.map((v) => Math.round(v * 100) / 100) }],
    note: "Click any day to see what ran that day. "
        + (matched ? "Showing the selected tags." : "Showing all spend — pick a tag to narrow it."),
    onSelect: (i) => {
      // Clicking the day already open closes it, so the chart is a toggle rather than a
      // one-way trip that needs a separate control to undo.
      tagState.day = tagState.day === dates[i] ? null : dates[i];
      paintTagDay(matched);
      $("tagDay")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    },
  });
}

/**
 * What ran on one particular day.
 *
 * The window totals answer "what does this cost"; this answers "what did I spend on the 24th",
 * which is the question people actually ask when a number moves. Same data, no extra request —
 * the daily cells are already in memory, so picking a day is a filter, not a fetch.
 */
function paintTagDay(matched) {
  const d = tagState.data;
  const host = $("tagDay");
  if (!host) return;

  const dates = d.dates || [];
  const day = tagState.day;
  if (!day || !dates.length) {
    host.innerHTML = "";
    return;
  }

  const i = dates.indexOf(day);
  if (i < 0) {
    // The selected day is not in this window any more — a period or scope change can do that.
    tagState.day = null;
    host.innerHTML = "";
    return;
  }

  const cur = d.currency;
  const pool = matched || d.resources;
  const rows = [];
  for (const r of pool) {
    const hit = (r.d || []).find(([idx]) => idx === i);
    if (hit && hit[1]) rows.push({ ...r, dayCost: hit[1] });
  }
  rows.sort((a, b) => b.dayCost - a.dayCost);
  const total = rows.reduce((t, r) => t + r.dayCost, 0);

  const pretty = new Date(day + "T00:00:00").toLocaleDateString(undefined, {
    weekday: "long", day: "numeric", month: "long", year: "numeric",
  });

  host.innerHTML = `
    <section class="card tagday">
      <div class="tagsel-head">
        <h3>${esc(pretty)}</h3>
        <button type="button" class="linkish" id="tagDayClose">Close</button>
      </div>
      <p class="muted tagpick-hint">
        ${moneyHtml(total, cur, 2)} across ${rows.length.toLocaleString()} resource${
          rows.length === 1 ? "" : "s"}${matched ? " carrying the selected tags" : ""}.
      </p>
    </section>` +
    (rows.length
      ? table(
          "Resources that billed this day",
          rows,
          [
            { label: "Resource", cell: (r) => esc(r.name) },
            { label: "Service", cell: (r) => esc(r.service || "—") },
            { label: "Resource group", cell: (r) => esc(r.group || "—") },
            { label: "Subscription", cell: (r) => esc(r.subscription || "—") },
            { label: "Tags", sort: false, cell: (r) => tagPills(r) },
            { label: "Cost", num: true, value: (r) => r.dayCost,
              cell: (r) => moneyHtml(r.dayCost, cur, 2),
              total: (rs) => moneyHtml(rs.reduce((n, r) => n + r.dayCost, 0), cur, 2) },
          ],
          null,
          { pageSize: 15 }
        )
      : `<section class="card"><p class="muted empty">Nothing billed on this day${
          matched ? " for the selected tags" : ""}.</p></section>`);

  const close = $("tagDayClose");
  if (close) {
    close.onclick = () => {
      tagState.day = null;
      paintTagDay(matched);
    };
  }
  wireTables(host);
}

async function renderCommitments(token) {
  const d = await cached(key("commitments"), () =>
    get(`/api/dashboard/commitments${qs()}`, { timeout: 30000 }));
  if (token !== loadToken) return;

  const c = d.coverage || {};
  const s = d.spot || {};
  const cur = c.currency;

  if (c.empty) {
    $("tabBody").innerHTML = `<p class="muted empty">No cost data loaded for this period yet.</p>`;
    return;
  }

  const split = c.split || {};
  const compute = c.compute_split || {};
  const hasCommitment = (split.committed || 0) > 0;

  const mix = (obj, total) =>
    Object.entries(obj)
      .sort((a, b) => b[1] - a[1])
      .map(
        ([k, v]) => `<tr>
          <td>${esc(k)}</td>
          <td class="num">${total ? ((v / total) * 100).toFixed(1) : "—"}%</td>
          <td class="num">${moneyHtml(v, cur, 2)}</td>
        </tr>`
      )
      .join("");

  $("tabBody").innerHTML = `
    ${kpiRow([
      {
        label: "Compute on a commitment",
        value: c.compute_coverage_pct == null ? "—" : `${c.compute_coverage_pct}%`,
        cls: (c.compute_coverage_pct || 0) > 0 ? "down" : "",
        sub: `of ${esc(money(c.compute_total, cur))} compute spend`,
      },
      {
        label: "All spend on a commitment",
        value: c.coverage_pct == null ? "—" : `${c.coverage_pct}%`,
        sub: `of ${esc(money(c.total, cur))} total`,
      },
      {
        label: "Spot",
        value: c.spot_pct == null ? "—" : `${c.spot_pct}%`,
        sub: s.empty ? "no Spot usage" : `saved ${esc(money(s.total_saved, cur))}`,
      },
      {
        label: "Active commitments",
        value: String((c.benefits || []).length),
        sub: hasCommitment ? "reservations and plans" : "none found",
      },
    ])}

    <div id="mixChart" class="card chart-card"></div>

    <div class="grid-2">
      <section class="card">
        <h3>How every dollar is priced</h3>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Pricing model</th><th class="num">Share</th><th class="num">Cost</th></tr></thead>
            <tbody>${mix(split, c.total)}</tbody>
          </table>
        </div>
      </section>
      <section class="card">
        <h3>Compute only</h3>
        <p class="muted note">Commitments apply to compute, so this is the number that matters —
          a storage-heavy estate looks permanently uncovered otherwise.</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Pricing model</th><th class="num">Share</th><th class="num">Cost</th></tr></thead>
            <tbody>${mix(compute, c.compute_total)}</tbody>
          </table>
        </div>
      </section>
    </div>

    ${
      (c.benefits || []).length
        ? table(
            "Reservations and savings plans in use",
            c.benefits,
            [
              { label: "Benefit", cell: (r) => esc(r.name || "—") },
              { label: "Kind", cell: (r) => esc(r.model || "—") },
              {
                label: "Resources",
                num: true,
                value: (r) => r.resources,
                cell: (r) => String(r.resources),
              },
              {
                label: `Cost, ${c.days}d`,
                num: true,
                value: (r) => r.cost,
                cell: (r) => moneyHtml(r.cost, r.currency || cur, 2),
                total: (rows) => moneyHtml(rows.reduce((n, r) => n + r.cost, 0), cur, 2),
              },
            ],
            null
          )
        : `<section class="card"><h3>Reservations and savings plans in use</h3>
           <p class="muted empty">${
             c.amortized
               ? "No reservations or savings plans are being applied to this spend."
               : "No commitment usage found. If you do hold reservations, the warehouse was " +
                 "built from an actual-cost export, which bills a purchase as a single charge " +
                 "on the day it was made and carries no benefit attribution. Refresh the data " +
                 "to pick up an amortized view."
           }</p></section>`
    }

    ${
      !s.empty
        ? table(
            "Spot savings, month by month",
            s.trend || [],
            [
              { label: "Month", cell: (r) => esc(r.month) },
              {
                label: "Paid",
                num: true,
                value: (r) => r.spot_cost,
                cell: (r) => moneyHtml(r.spot_cost, cur, 2),
              },
              {
                label: "On-demand equivalent",
                num: true,
                value: (r) => r.would_have_cost,
                cell: (r) => moneyHtml(r.would_have_cost, cur, 2),
              },
              {
                label: "Saved",
                num: true,
                value: (r) => r.saved,
                cell: (r) =>
                  r.saved > 0
                    ? `<span class="delta down">${money(r.saved, cur, 2)}</span>`
                    : '<span class="muted">—</span>',
                total: (rows) =>
                  moneyHtml(rows.reduce((n, r) => n + r.saved, 0), cur, 2),
              },
              {
                label: "Discount",
                num: true,
                value: (r) => r.saved_pct || 0,
                cell: (r) => (r.saved_pct == null ? "—" : `${r.saved_pct}%`),
              },
            ],
            s.note
          )
        : ""
    }

    <div id="trendChart" class="card chart-card"></div>`;

  const trend = c.trend || [];
  if (trend.length > 1) {
    drawChart($("trendChart"), {
      type: "stacked_bar",
      title: "Spend by pricing model, month by month",
      labels: trend.map((t) => t.month),
      datasets: [
        { label: "On demand", data: trend.map((t) => t.on_demand) },
        { label: "Committed", data: trend.map((t) => t.committed) },
        { label: "Spot", data: trend.map((t) => t.spot) },
      ],
      currency: cur,
    });
  } else {
    $("trendChart")?.remove();
  }

  // A doughnut of one meaningful slice is a circle. Only draw the mix when there is genuinely
  // a mix — below 1% the second slice is invisible anyway and the table says it better.
  const entries = Object.entries(compute).filter(([, v]) => v > 0);
  const smallest = entries.length ? Math.min(...entries.map(([, v]) => v)) : 0;
  if (entries.length > 1 && c.compute_total && smallest / c.compute_total >= 0.01) {
    drawChart($("mixChart"), {
      type: "doughnut",
      title: `Compute spend by pricing model — last ${c.days} days`,
      labels: entries.map(([k]) => k),
      datasets: [{ label: "Cost", data: entries.map(([, v]) => v) }],
      // Pricing models are three different things, not one thing ranked, so they take the
      // categorical hues — and they match the stacked bar directly below, where the same three
      // names appear. Two charts of the same data in different colours is its own bug.
      categorical: true,
      currency: cur,
    });
  } else {
    $("mixChart")?.remove();
  }
}

async function renderEsu(token) {
  const d = await live(token, `/api/dashboard/esu${qs()}`, 120000);
  if (!d) return;

  const machines = d.machines || [];
  const cur = d.currency;
  const status = (m) =>
    m.status === "out of support"
      ? '<span class="pill bad">out of support</span>'
      : '<span class="pill warn">ending soon</span>';

  if (!machines.length) {
    $("tabBody").innerHTML = `
      ${kpiRow([
        { label: "Out of support", value: "0" },
        { label: "Support ending soon", value: "0" },
        { label: "ESU billed to date", value: esc(money(d.billed_esu || 0, cur, 2)) },
      ])}
      <p class="muted empty">Nothing in scope is out of support or close to it. Checked
        ${d.scanned?.vms ?? 0} virtual machine(s), ${d.scanned?.arc ?? 0} Arc-connected server(s)
        and ${d.scanned?.sql ?? 0} SQL Server VM(s) across ${d.subscriptions ?? 0} subscription(s).
        ${d.error ? esc(d.error) : ""}</p>
      <p class="muted note">${esc(d.note || "")}</p>`;
    return;
  }

  $("tabBody").innerHTML = `
    ${kpiRow([
      { label: "Out of support", value: String(d.out_of_support), cls: d.out_of_support ? "up" : "" },
      { label: "Ending soon", value: String(d.ending_soon) },
      {
        label: "Uncovered",
        value: String(d.exposed),
        cls: d.exposed ? "up" : "down",
        sub: d.exposed ? "would need an ESU licence" : "all covered",
      },
      {
        label: "Estimated ESU / month",
        value: d.priced ? esc(money(d.estimated_monthly_cost, cur, 2)) : "no price list",
        sub: "for uncovered machines",
      },
      { label: "ESU billed to date", value: esc(money(d.billed_esu || 0, cur, 2)) },
    ])}
    <p class="muted note">${esc(d.note || "")}</p>
    ${table(
      "Machines past or near end of support",
      machines,
      [
        { label: "Name", cell: (r) => esc(r.name || "—") },
        { label: "Product", cell: (r) => `${esc(r.product)} ${status(r)}` },
        { label: "Where", cell: (r) => esc(r.kind) },
        { label: "Support ended", cell: (r) => esc(r.support_ends) },
        { label: "ESU position", cell: (r) => esc(r.coverage) },
        {
          label: "Est. / month",
          num: true,
          cell: (r) =>
            r.monthly_esu_cost == null
              ? "—"
              : r.monthly_esu_cost === 0
                ? "included"
                : esc(money(r.monthly_esu_cost, cur, 2)),
        },
      ],
      `Checked ${d.scanned?.vms ?? 0} VM(s), ${d.scanned?.arc ?? 0} Arc server(s), ` +
        `${d.scanned?.sql ?? 0} SQL VM(s).`
    )}`;
}

function wireShell() {
  for (const btn of $("period").querySelectorAll("[data-days]")) {
    btn.classList.toggle("on", Number(btn.dataset.days) === days);
    btn.onclick = () => {
      days = Number(btn.dataset.days);
      writeStore("costDays", days);
      for (const b of $("period").querySelectorAll("[data-days]")) {
        b.classList.toggle("on", b === btn);
      }
      // The analysis was computed for the old window; every figure in it now answers a
      // question nobody is asking.
      invalidateAnalysis();
      loadTabs();
    };
  }

  // Display currency. Only shown when there is a real choice — a lone "USD" dropdown is a
  // control that does nothing, and one of those is worse than none.
  get("/api/currency", { timeout: 15000 })
    .then((c) => {
      const sel = $("currency");
      if (!sel || (c.supported || []).length < 2) return;
      const saved = readStore("costCurrency", "");
      sel.innerHTML =
        `<option value="">As billed</option>` +
        c.supported
          .map((code) => `<option value="${esc(code)}"${code === saved ? " selected" : ""}>${
            esc(code)}</option>`)
          .join("");
      sel.hidden = false;
      sel.title = c.source ? `Rates: ${c.source}` : "";
      sel.onchange = () => {
        writeStore("costCurrency", sel.value);
        clearCache();
        invalidateAnalysis();
        refreshHeader();
        loadTabs();
      };
    })
    .catch(() => {});

  // Refreshing writes to the shared warehouse, so it is offered only to those allowed to do it.
  // Shares the header's identity request rather than making its own.
  whoAmI()
    .then((me) => {
      if (!me?.user?.admin) return;
      $("refresh").hidden = false;
      applyFreshness();
      $("refreshBtn").onclick = (e) => {
        e.stopPropagation();
        openRefresh($("refreshMenu").hidden);
      };
      document.addEventListener("click", (e) => {
        if (!$("refresh").contains(e.target)) openRefresh(false);
      });
      document.addEventListener("keydown", (e) => {
        if (e.key === "Escape") openRefresh(false);
      });
    })
    .catch(() => {
      /* the control simply stays hidden */
    });

  const side = $("side");
  const setOpen = (open) => {
    document.body.classList.toggle("side-closed", !open);
    $("askToggle").setAttribute("aria-expanded", String(open));
    writeStore("costAskOpen", open);
    if (open) $("q")?.focus();
  };
  // Default open on a wide screen, closed on a narrow one where it would cover the dashboard.
  const wide = window.matchMedia("(min-width: 1100px)").matches;
  setOpen(readStore("costAskOpen", wide));

  // The analyst opens the analysis over the board — the full width, because the findings are
  // arguments rather than table rows and a 400px column cannot carry one.
  mountBot($("chatFab"), () => (isAnalysisOpen() ? closeAnalysis() : openAnalysis()));
  // The analysis asks the same question the dashboard is showing — same period, same
  // subscriptions, same currency — so a finding can be checked against the tab beside it.
  setAnalysisContext(() => ({
    days,
    scope: scopeIds(),
    currency: readStore("costCurrency", ""),
  }));
  wireAnalysis();

  // A question asked from the analysis. `inPlace` keeps the analysis open and lets the answer
  // stream into the panel's own conversation area — the finding being asked about stays on
  // screen, which is the whole reason for asking from there. Anything else opens the side panel
  // as before.
  window.addEventListener("cl:ask", (e) => {
    const detail = e.detail;
    const q = String(typeof detail === "string" ? detail : detail?.q || "").trim();
    if (!q) return;
    if (!(typeof detail === "object" && detail.inPlace)) {
      closeAnalysis();
      setOpen(true);
    }
    askAgent(q);
  });
  // A finding names the tab its number came from; clicking it goes there.
  window.addEventListener("cl:goto-tab", (e) => selectTab(e.detail));

  // The analyst reflects the conversation. app.js announces what the model is doing rather than
  // calling in here, so the chat stays unaware of the launcher entirely.
  window.addEventListener("cl:agent", (e) => {
    if (e.detail === "done") setBotState("idle");
    else setBotState(e.detail);
  });

  $("askToggle").onclick = () => setOpen(document.body.classList.contains("side-closed"));
  $("sideClose").onclick = () => setOpen(false);
  side.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !window.matchMedia("(min-width: 1100px)").matches) setOpen(false);
  });

  wireExport();

  // The picker drives both halves of the page — and invalidates the analysis, which was
  // computed against the subscriptions that were selected when it ran.
  onScopeChange(() => {
    invalidateAnalysis();
    loadTabs();
  });
}

wireShell();
loadTabs();

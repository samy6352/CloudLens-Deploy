/**
 * CloudLens — chat client.
 *
 * Design notes:
 *  - Chart.js (200KB) is fetched on demand the first time a chart arrives, so a session that
 *    only asks text questions never pays for it.
 *  - Every fetch has a timeout and the summary strip retries, because a throttled Azure call
 *    should degrade to "—" rather than hang the page forever.
 *  - Streaming output is announced to screen readers once per step, not per token.
 */

const $ = (id) => document.getElementById(id);
const chat = $("chat");
const liveRegion = $("live");

let history = [];
let busy = false;
let controller = null;

// Which subscriptions to analyse. Empty = all. Persisted across reloads.
let allSubs = [];
let selected = new Set(readStore("costScope", []));
let introTemplate = "";
let pinned = true;
// Who is signed in, once known. The picker's caption depends on it, so it lives here rather
// than inside loadIdentity — two functions writing the same line is how the caption ended up
// silently reverting to the generic wording.
let identity = null;

// ------------------------------------------------------------------- utils
function readStore(key, fallback) {
  // Private browsing and some hardened configs throw on access rather than returning null.
  try {
    return JSON.parse(localStorage.getItem(key)) ?? fallback;
  } catch {
    return fallback;
  }
}
function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* not fatal — the choice just won't survive a reload */
  }
}

const esc = (s) =>
  String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const money = (n, cur = "USD", digits = 0) =>
  n == null || Number.isNaN(n)
    ? "—"
    : new Intl.NumberFormat(undefined, {
        style: "currency",
        currency: cur || "USD",
        maximumFractionDigits: digits,
      }).format(n);

/**
 * The same figure, with the currency symbol marked up separately.
 *
 * A symbol set at the same size and weight as the digits competes with them, and in a column
 * it is the symbol that lines up rather than the numerals. Splitting it lets CSS hold it back
 * a shade so the quantity reads first — the detail that separates a financial tool from a
 * spreadsheet dump.
 *
 * Safe to insert as HTML: every part comes from Intl's own formatter, not from user input.
 */
const moneyHtml = (n, cur = "USD", digits = 0) => {
  if (n == null || Number.isNaN(n)) return "—";
  const parts = new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: cur || "USD",
    maximumFractionDigits: digits,
  }).formatToParts(n);
  return parts
    .map((p) =>
      p.type === "currency" ? `<span class="cur">${esc(p.value)}</span>` : esc(p.value)
    )
    .join("");
};

// The dashboard needs the same formatting, fetching and charting as the chat. Exporting them
// keeps one implementation rather than two that drift.
export { esc, money, moneyHtml };

const announce = (text) => {
  if (liveRegion) liveRegion.textContent = text;
};

/** Send the person to the login page, remembering where they were trying to be. */
function toLogin() {
  const here = location.pathname + location.search;
  location.replace(`/login?next=${encodeURIComponent(here)}`);
}

/** fetch with a timeout, so a stalled request can't wedge the UI. */
export async function get(url, { timeout = 20000, ...opts } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const r = await fetch(url, { ...opts, signal: ac.signal });
    // An expired session shouldn't look like a broken app: bounce to the login page instead
    // of painting every tile with an error.
    if (r.status === 401) {
      toLogin();
      throw new Error("Signed out");
    }
    if (r.status === 403) {
      const body = await r.json().catch(() => ({}));
      const err = new Error(body.detail || "You don't have access to that.");
      err.forbidden = true;
      throw err;
    }
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return await r.json();
  } finally {
    clearTimeout(timer);
  }
}

/**
 * A write, with the server's own explanation preserved.
 *
 * `get` turns anything that is not 401/403 into "409 Conflict" — the status text and nothing
 * else. That is fine for reads, where a failure means a panel does not draw, but these
 * endpoints refuse with a sentence worth showing: which subscription was not permitted, or
 * that setup has already been done. Throwing away the body would replace all of that with a
 * number.
 */
export async function post(url, body = null, { timeout = 30000 } = {}) {
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), timeout);
  try {
    const r = await fetch(url, {
      method: "POST",
      signal: ac.signal,
      ...(body === null ? {} : {
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }),
    });
    if (r.status === 401) {
      toLogin();
      throw new Error("Signed out");
    }
    const data = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(data.detail || `${r.status} ${r.statusText}`);
      err.status = r.status;
      throw err;
    }
    return data;
  } finally {
    clearTimeout(timer);
  }
}

// marked + DOMPurify are 57 KB and only matter once the agent answers, so they load on first
// use. Streaming starts before they arrive, hence the escaped-text fallback below: early tokens
// render as plain text and the first full re-render after loading upgrades them to markdown.
let mdLib = null;

function loadMarkdown() {
  if (mdLib) return mdLib;
  mdLib = Promise.all([
    loadScript("/assets/vendor/marked.min.js"),
    loadScript("/assets/vendor/purify.min.js"),
  ]).catch(() => null); // a failed fetch must degrade to plain text, never break the answer
  return mdLib;
}

function loadScript(src) {
  return new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = resolve;
    s.onerror = () => reject(new Error(`failed to load ${src}`));
    document.head.appendChild(s);
  });
}

function render(md) {
  if (!window.marked || !window.DOMPurify) {
    loadMarkdown();
    return esc(md).replace(/\n/g, "<br>");
  }
  return DOMPurify.sanitize(marked.parse(md || ""));
}

/** Wrap tables so they scroll independently instead of stretching the page on mobile. */
function enhance(container) {
  for (const t of container.querySelectorAll("table")) {
    if (t.parentElement?.classList.contains("table-wrap")) continue;
    const wrap = document.createElement("div");
    wrap.className = "table-wrap";
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "Table, scrollable");
    t.replaceWith(wrap);
    wrap.appendChild(t);
  }
}

const TOOL_LABEL = {
  query_costs: "warehouse SQL",
  render_chart: "chart",
  warehouse_status: "warehouse status",
  find_waste: "idle resources",
  vm_utilisation: "VM utilisation",
  advisor_recommendations: "Advisor",
  list_subscriptions: "subscriptions",
  cost_summary: "live spend",
  cost_trend: "live trend",
  cost_changes: "live changes",
  cost_forecast: "forecast",
  budgets: "budgets",
  top_resources: "top resources",
};

// -------------------------------------------------------------------- boot
async function boot() {
  introTemplate = document.querySelector(".intro").outerHTML;
  wire();
  wireTheme();
  loadIdentity();
  loadHealth();
  loadSubscriptions();
  loadGlance();
  loadWarehouse();
  loadFirstRun();
}

// ---------------------------------------------------------------- first run
/**
 * The empty-deployment case, which is the one moment nobody has any context.
 *
 * A warehouse with no rows and an estate that genuinely spent nothing draw exactly the same
 * dashboard — six zeroes and some empty tables — and only one of those is a fact about
 * anyone's spend. Left alone it reads as a broken product, and the actual fix (create a Cost
 * Management export, point it at storage, grant its identity a role, wait, then load the
 * blobs) is spread across two tabs and a form.
 *
 * So when there is nothing at all, say so and offer to go and find it. The offer is shown
 * only while the deployment is genuinely empty and unanswered: once rows land, the condition
 * that produced it is false forever, which is why "only once" needs almost no bookkeeping.
 */
let firstRunPoll = null;

/* Prerequisites — what the app's own identity is missing, and offering to fix it.
 *
 * Two failures this surfaces, both of which otherwise look like something else. An
 * unregistered CostManagementExports provider makes setup fall back to the slow API path, so
 * the data arrives and nothing appears wrong. A managed identity without Reader and Cost
 * Management Reader reads an empty estate, which looks like having no spend rather than no
 * permission.
 *
 * Shown only when something is actually missing. A panel that says "everything is fine" on a
 * healthy deployment is a panel people learn to scroll past, and this one needs reading on the
 * day it appears.
 *
 * The preview is not decoration. Granting the app's identity a role is standing access that
 * outlives the session, so what will be granted, to which principal, on which subscriptions, is
 * on screen before the button is pressed — and the button only ever does what was shown.
 */
async function loadPrereqs() {
  const host = document.getElementById("frPrereq");
  if (!host) return;

  let p;
  try {
    p = await get("/api/prereqs", { timeout: 60000 });
  } catch {
    return; // advisory: never let this stop somebody setting their data up
  }
  paintPrereqs(p);
}

function prereqRow(sub) {
  const needs = [
    ...(sub.roles_missing || []),
    ...(sub.providers_unregistered || []).map((n) => n.replace("Microsoft.", "")),
  ];
  // `can_grant === false` is a refusal; null is "we could not ask", and the two want different
  // words — one is a blocker to hand to an administrator, the other just an unknown.
  const state =
    sub.ready ? `<span class="ok">ready</span>`
    : sub.can_grant === false ? `<span class="warn">you cannot grant here</span>`
    : sub.can_grant === null ? `<span class="muted">rights unknown</span>`
    : `<span>will be fixed</span>`;
  return `<tr>
      <td>${esc(sub.name)}</td>
      <td>${needs.length ? esc(needs.join(", ")) : "<span class='muted'>nothing</span>"}</td>
      <td>${state}</td>
    </tr>`;
}

function paintPrereqs(p) {
  const host = document.getElementById("frPrereq");
  if (!host) return;
  host.innerHTML = "";
  if (!p || !p.subscriptions?.length) return;

  if (p.note && !p.principal_id) {
    host.innerHTML = `<div class="banner warn">${esc(p.note)}</div>`;
    return;
  }

  const unready = p.subscriptions.filter((s) => !s.ready);
  if (!unready.length) return;   // nothing to say, so say nothing

  const blocked = unready.filter((s) => s.can_grant === false);
  const commands = blocked.map((s) => s.command).filter(Boolean);

  host.innerHTML = `<section class="card fr-prereq">
      <h3>This deployment is missing some access</h3>
      <p class="muted">CloudLens reads cost as its own identity
        (<code>${esc(p.principal_id || "unknown")}</code>). Where that identity has no roles it
        reads an empty estate, which looks like having no spend rather than no permission.</p>
      <table class="fr-prereq-table">
        <thead><tr><th>Subscription</th><th>Missing</th><th></th></tr></thead>
        <tbody>${unready.map(prereqRow).join("")}</tbody>
      </table>
      ${p.can_apply
        ? `<div class="fr-actions">
             <button type="button" id="frFix" class="primary">Grant access and register providers</button>
           </div>
           <p class="muted note">Runs with your own Azure access, so it can only do what you
             could do yourself. Role assignments take a few minutes to take effect.</p>`
        : `<p class="muted note">An administrator of this deployment can grant these.</p>`}
      ${commands.length
        ? `<p class="muted note">For the subscriptions you cannot grant on, an administrator can
             run:</p><pre class="fr-cmd">${esc(commands.join("\n\n"))}</pre>`
        : ""}
    </section>`;

  const fix = document.getElementById("frFix");
  if (!fix) return;
  fix.onclick = async () => {
    fix.disabled = true;
    fix.textContent = "Granting…";
    let r;
    try {
      r = await post("/api/prereqs/apply", {}, { timeout: 120000 });
    } catch (err) {
      fix.disabled = false;
      fix.textContent = "Grant access and register providers";
      host.insertAdjacentHTML(
        "beforeend",
        `<div class="banner warn">${esc(err?.message || "Could not apply.")}</div>`
      );
      return;
    }
    const failed = r.subscriptions.filter((s) => !s.ok);
    const cmds = failed.map((s) => s.command).filter(Boolean);
    host.innerHTML = `<section class="card fr-prereq">
        <h3>${r.changed ? "Access granted" : "Nothing needed changing"}</h3>
        <p class="muted">${esc(r.note || "")}</p>
        ${failed.length
          ? `<p class="warn">${failed.length} subscription${failed.length === 1 ? "" : "s"}
               could not be changed with your access.</p>
             ${cmds.length ? `<pre class="fr-cmd">${esc(cmds.join("\n\n"))}</pre>` : ""}`
          : ""}
      </section>`;
  };
}

async function loadFirstRun() {
  let s;
  try {
    s = await get("/api/onboarding", { timeout: 30000 });
  } catch {
    return; // never let this be the reason the page fails to render
  }
  paintFirstRun(s);
}

function paintFirstRun(s) {
  const host = document.getElementById("firstRun");
  const shell = document.getElementById("shell");
  if (!host) return;

  // Nothing to say once there is data, or once the question has been answered. Restores the
  // board in case a scan just finished under a card that was covering it.
  if (!s || (!s.first_run && !s.running)) {    host.hidden = true;
    host.innerHTML = "";
    if (shell) shell.hidden = false;
    if (firstRunPoll) { clearTimeout(firstRunPoll); firstRunPoll = null; }
    return;
  }

  const steps = s.progress?.steps || [];
  const running = !!s.running;
  // Somebody else's ingest — the boot-time one, or a colleague's refresh. The card waits it
  // out rather than offering to do the same job again.
  const waiting = !!s.ingesting;

  // The board is hidden while this is up. A row of zeroes behind an offer to go and find the
  // numbers invites reading the zeroes as the answer.
  if (shell) shell.hidden = true;
  host.hidden = false;

  const icon = { done: "✓", running: "…", failed: "✕", skipped: "–" };
  const stepList = steps.length
    ? `<ol class="fr-steps">${steps
        .map(
          (st) => `<li class="fr-step ${esc(st.status)}">
            <span class="fr-mark" aria-hidden="true">${icon[st.status] || "·"}</span>
            <span><strong>${esc(st.name)}</strong>${
              st.detail ? ` — ${esc(st.detail)}` : ""
            }</span></li>`
        )
        .join("")}</ol>`
    : "";

  if (running) {
    host.innerHTML = `<section class="card fr-card">
        <h2>Setting up your cost data</h2>
        <p class="muted">This runs against your own Azure access. Cost reports take a few
          minutes to generate — you can leave this page open.</p>
        ${stepList}
      </section>`;
  } else if (waiting) {
    host.innerHTML = `<section class="card fr-card">
        <h2>Loading your cost data</h2>
        <p class="muted">${esc(s.blocked || "This takes a few minutes.")}</p>
        ${stepList}
      </section>`;
  } else if (s.blocked) {
    host.innerHTML = `<section class="card fr-card">
        <h2>No cost data yet</h2>
        <p class="muted">${esc(s.blocked)}</p>
        ${stepList}
      </section>`;
  } else if (s.can_scan) {
    // Named for what it does to their Azure, not for what it does to this page. "Scan" alone
    // sounds read-only, and this may create an export.
    host.innerHTML = `<section class="card fr-card">
        <h2>Welcome — there is no cost data loaded yet</h2>
        <p>CloudLens can set itself up against the
          ${s.subscriptions} subscription${s.subscriptions === 1 ? "" : "s"} your Azure
          account can see. It will look for cost exports Azure is already writing, create a
          daily one if there are none, and load the last three months of history.</p>
        <p class="muted">Everything runs with your own access, and nothing is deleted or
          changed in your estate beyond adding a cost export. This is offered once.</p>
        ${stepList}
        <div id="frPrereq"></div>
        <div class="fr-actions">
          <button type="button" id="frScan" class="primary">Set up my cost data</button>
          <button type="button" id="frSkip" class="ghost">Not now</button>
        </div>
        <p class="muted note">Choosing "not now" hides this for good — you can still load data
          any time from Refresh, or the Cost exports tab.</p>
      </section>`;
    // Asked here rather than on every page load: this is the one moment the answer changes
    // what somebody should do next, and the check costs an ARM call per subscription.
    loadPrereqs();
  } else {
    host.innerHTML = `<section class="card fr-card">
        <h2>No cost data yet</h2>
        <p class="muted">Nothing has been loaded into this deployment.</p>
        ${stepList}
      </section>`;
  }

  const scan = document.getElementById("frScan");
  if (scan) {
    scan.onclick = async () => {
      scan.disabled = true;
      scan.textContent = "Starting…";
      try {
        await post("/api/onboarding/scan");
      } catch (err) {
        scan.disabled = false;
        scan.textContent = "Set up my cost data";
        const host2 = document.getElementById("firstRun");
        host2.insertAdjacentHTML(
          "beforeend",
          `<div class="banner warn">${esc(err?.message || "Could not start setup.")}</div>`
        );
        return;
      }
      loadFirstRun();
    };
  }

  const skip = document.getElementById("frSkip");
  if (skip) {
    skip.onclick = async () => {
      skip.disabled = true;
      try {
        await post("/api/onboarding/dismiss");
      } catch {
        skip.disabled = false;
        return;
      }
      paintFirstRun(null);
    };
  }

  // Poll while anything is in flight — this card's scan, or an ingest it is waiting on. Stops
  // as soon as it settles, so an idle page costs nothing — and reloads the rest of the
  // dashboard once the data is actually there.
  if ((running || waiting) && !firstRunPoll) {
    firstRunPoll = setTimeout(async () => {
      firstRunPoll = null;
      const before = !!document.getElementById("firstRun")?.hidden;
      await loadFirstRun();
      const after = !!document.getElementById("firstRun")?.hidden;
      // The card came down: the page was drawn against an empty warehouse, so everything on
      // it is now stale.
      if (!before && after) {
        loadSubscriptions();
        loadGlance();
        loadWarehouse();
        window.dispatchEvent(new CustomEvent("cost:data-loaded"));
      }
    }, 5000);
  }
}

/** The caption under the heading. One writer, so a later render can't clobber it. */
function describeScope(all, label) {
  const intro = $("introScope");
  if (!intro) return;
  if (!all) {
    intro.textContent = `Scoped to ${label}. Read-only.`;
    return;
  }
  // An SSO sign-in is already limited to their own access, so "every subscription" would
  // overstate it — say how many they actually have.
  if (identity?.user?.source === "entra") {
    const n = identity.subscriptions ?? 0;
    intro.textContent =
      `Covers the ${n} subscription${n === 1 ? "" : "s"} your Azure access allows. Read-only.`;
  } else {
    intro.textContent = "Covers every subscription your signed-in account can see. Read-only.";
  }
}

/**
 * Who is signed in, fetched at most once.
 *
 * Two modules need this — the header chip and the admin-only refresh control — and both used
 * to ask for it independently, which cost a duplicate request on every load. The promise is
 * shared rather than the result, so a second caller arriving mid-flight waits on the first
 * request instead of starting a second.
 */
let identityPromise = null;
export function whoAmI() {
  identityPromise = identityPromise || get("/api/auth/me", { timeout: 30000 });
  return identityPromise;
}

/** Show who is signed in. `/api/auth/me` is public, so this never bounces on its own. */
async function loadIdentity() {
  try {
    // Resolving someone's subscriptions means asking Azure about their role assignments, which
    // is several calls on a cold session — hence a longer deadline than the other boot requests.
    const me = await whoAmI();
    identity = me;
    if (!me.required) return; // auth switched off — no chip, nothing to sign out of
    if (!me.authenticated) return toLogin();
    $("whoName").textContent = me.user.email || me.user.name;
    $("whoName").title = me.user.name;
    $("whoAdmin").hidden = !me.user.admin;
    $("who").hidden = false;

    if (me.user.source === "entra") {
      describeScope(isAllSelected(), "");
      if (me.problem) banner(me.problem);
      else if ((me.subscriptions ?? 0) === 0) {
        banner(
          "Your account can't see any Azure subscription here, so there is nothing to report. " +
            "Ask a subscription owner for the Cost Management Reader role."
        );
      }
    }
  } catch {
    /* the other loaders will surface a connection problem */
  }
}

/** A page-level notice, for things that aren't tied to one answer. */
function banner(text) {
  const el = document.createElement("div");
  el.className = "banner warn";
  el.textContent = text;
  chat.prepend(el);
}

async function loadHealth() {
  try {
    const h = await get("/api/health");
    const ok = h.status === "ok";
    $("dot").className = `dot ${ok ? "ok" : "err"}`;
    $("dot").setAttribute("aria-label", ok ? `Connected, model ${h.model}` : "Service degraded");
    $("dot").title = ok ? `model ${h.model}` : h.detail || "degraded";
  } catch {
    $("dot").className = "dot err";
    $("dot").setAttribute("aria-label", "API unreachable");
    $("dot").title = "API unreachable";
  }
}

async function loadGlance(attempt = 0) {
  const tiles = $("glance").children;
  const scope = scopeIds();
  const want = readStore("costCurrency", "");
  const qs = "?" + [
    scope.length ? `scope=${encodeURIComponent(scope.join(","))}` : "",
    want ? `currency=${encodeURIComponent(want)}` : "",
  ].filter(Boolean).join("&");
  try {
    const o = await get(`/api/overview${qs}`, { timeout: 30000 });

    // The unscoped snapshot is built on a background timer; poll gently until it lands.
    if (o.status === "loading") {
      if (attempt < 40) setTimeout(() => loadGlance(attempt + 1), 3000);
      return;
    }
    if (o.status === "error") throw new Error(o.detail);

    const cur = o.currency;
    setTile(tiles[0], "Month to date", money(o.month_to_date, cur));
    setTile(tiles[1], "Last month", money(o.last_month, cur));
    setTile(tiles[2], "Forecast 30d", money(o.forecast_30d, cur));

    if (o.budget) {
      const b = o.budget;
      // Over budget is not a stronger shade of "watch this" — it is a different fact, and the
      // amber used for 80% made a blown budget read as an approaching one. The subtitle carries
      // the count when several are over, because the tile can only name the worst.
      const more = b.over > 1 ? `${b.over} budgets over` : b.name;
      setTile(tiles[3], "Budget", `${b.percent_used}%`, more,
              b.status === "over" ? "bad" : b.status === "near" ? "warn" : "");
    } else {
      setTile(tiles[3], "Budget", "none set");
    }
  } catch (err) {
    for (const t of tiles) t.classList.remove("loading");
    // Being told "you can see nothing" is a settled answer, not a hiccup: retrying it wastes
    // two round trips and still ends with blank tiles. Say so once and stop.
    if (err && err.forbidden) {
      setTile(tiles[0], "Month to date", "—", "no Azure access");
      setTile(tiles[1], "Last month", "—");
      setTile(tiles[2], "Forecast 30d", "—");
      setTile(tiles[3], "Budget", "—");
      return;
    }
    // One quiet retry — these calls can be throttled by Azure.
    if (attempt < 2) setTimeout(() => loadGlance(attempt + 1), 8000);
  }
}

async function loadWarehouse(attempt = 0) {
  const tile = $("glance").children[4];
  try {
    const w = await get("/api/warehouse");
    const ing = w.ingest || {};
    if (ing.status === "running") {
      setTile(tile, "Local data", `${ing.done || 0}/${ing.total || 0}`, "ingesting…");
      if (attempt < 300) setTimeout(() => loadWarehouse(attempt + 1), 5000);
      return;
    }
    if (!w.rows) return setTile(tile, "Local data", "empty", "using live API");
    setTile(tile, "Local data", `${(w.rows / 1000).toFixed(1)}k rows`, `${w.from} → ${w.to}`);
  } catch {
    tile.classList.remove("loading");
  }
}

/**
 * One tile in the estate strip.
 *
 * `tone` is "", "warn" or "bad" rather than a boolean, because a budget at 105% and one at 85%
 * are different answers and colouring both amber loses the only distinction that matters.
 */
function setTile(tile, key, value, sub, tone) {
  tile.classList.remove("loading");
  const cls = tone === true ? "warn" : tone || "";
  tile.innerHTML =
    `<span class="k">${esc(key)}</span>` +
    `<span class="v${cls ? ` ${cls}` : ""}">${esc(value)}</span>` +
    (sub ? `<span class="sub">${esc(sub)}</span>` : "");
}

// ------------------------------------------------------- subscription picker
async function loadSubscriptions() {
  let backfill = null;
  try {
    const r = await get("/api/subscriptions", { timeout: 30000 });
    allSubs = r.subscriptions || [];
    backfill = r.backfill || null;
  } catch {
    allSubs = [];
  }
  const known = new Set(allSubs.map((s) => s.id));
  for (const id of [...selected]) if (!known.has(id)) selected.delete(id);
  renderPicker();
  paintBackfill(backfill);
  scopeChanged();
}

// While a backfill is running the warehouse gains rows the page has already drawn without, so
// it is re-checked until it settles. Cheap: one small request, and only while something is
// actually outstanding.
let backfillPoll = null;

/**
 * Say which of this person's subscriptions are still loading, and which could not be loaded.
 *
 * A dashboard that is simply missing a subscription looks identical to one whose spend is
 * genuinely zero, and the difference matters enormously — the first is "wait", the second is
 * "you are done". Neither is safe to infer from silence.
 */
function paintBackfill(state) {
  const host = document.getElementById("backfill");
  if (!host) return;

  const pending = state?.pending || [];
  const failed = state?.failed || [];
  if (!pending.length && !failed.length) {
    host.hidden = true;
    host.innerHTML = "";
    if (backfillPoll) { clearTimeout(backfillPoll); backfillPoll = null; }
    return;
  }

  const name = (s) => esc(s.name || s.id);
  const parts = [];
  if (pending.length) {
    parts.push(`<div class="banner info">
      <strong>Loading ${pending.length} subscription${pending.length === 1 ? "" : "s"}</strong>
      you can see but this dashboard had not pulled yet:
      ${pending.map(name).join(", ")}. Cost reports take a few minutes; the numbers below
      exclude them until they land.</div>`);
  }
  if (failed.length) {
    parts.push(`<div class="banner warn">
      <strong>Could not load ${failed.length} subscription${failed.length === 1 ? "" : "s"}</strong>
      ${failed.map((f) => `${name(f)}${f.detail ? ` — ${esc(f.detail)}` : ""}`).join("; ")}.
      Cost is read with this app's identity, so a subscription you can see but it cannot needs
      the app's managed identity granted access. Totals below exclude these.</div>`);
  }

  host.innerHTML = parts.join("");
  host.hidden = false;

  if (pending.length && !backfillPoll) {
    backfillPoll = setTimeout(() => {
      backfillPoll = null;
      loadSubscriptions();
    }, 30000);
  }
}

export function scopeIds() {
  // All (or none) selected means "everything" — send an empty scope so the server can serve the
  // fast unscoped snapshot instead of recomputing an identical filtered view.
  return selected.size === 0 || selected.size === allSubs.length ? [] : [...selected];
}

/**
 * Every subscription this person can pick, whatever the current scope.
 *
 * The picker filters by *what to analyse*; anything that needs to name a subscription — the
 * budget form, principally — needs the full list and its ids, because the tag data carries
 * subscription names only. A copy, so a caller cannot reorder the picker's own array.
 */
export function allSubscriptions() {
  return allSubs.map((s) => ({ ...s }));
}

// Anything else that depends on the chosen subscriptions — the dashboard, principally — has to
// redraw when the picker changes. A subscription list rather than a direct call keeps app.js
// unaware of what else is on the page.
const scopeListeners = [];

export function onScopeChange(fn) {
  scopeListeners.push(fn);
}

function scopeChanged() {
  for (const fn of scopeListeners) {
    try {
      fn(scopeIds());
    } catch {
      /* one bad listener must not stop the others */
    }
  }
}

function isAllSelected() {
  return selected.size === 0 || selected.size === allSubs.length;
}

function toggleSub(id) {
  if (selected.size === 0) allSubs.forEach((x) => selected.add(x.id)); // "all" -> explicit set
  selected.has(id) ? selected.delete(id) : selected.add(id);
  if (selected.size === 0) allSubs.forEach((x) => selected.add(x.id)); // never end up with none
  writeStore("costScope", [...selected]);
  renderPicker();
  loadGlance();
  scopeChanged();
}

function renderPicker() {
  const list = $("pickerList");
  list.innerHTML = "";

  if (!allSubs.length) {
    list.innerHTML = `<p class="muted" style="padding:10px;font-size:13px">No subscriptions found.</p>`;
  }

  allSubs.forEach((s) => {
    const on = selected.size === 0 || selected.has(s.id);
    const b = document.createElement("button");
    b.className = "pick" + (s.in_warehouse ? "" : " missing");
    b.type = "button";
    b.setAttribute("role", "option");
    b.setAttribute("aria-checked", String(on));
    b.setAttribute("aria-selected", String(on));
    b.title = s.in_warehouse ? s.id : `${s.id} — no local data yet`;
    b.innerHTML =
      `<span class="box" aria-hidden="true">${on ? "✓" : ""}</span>` +
      `<span class="nm">${esc(s.name || s.id)}</span>` +
      `<span class="amt">${s.cost != null ? money(s.cost) : "—"}</span>`;
    b.onclick = () => toggleSub(s.id);
    list.appendChild(b);
  });

  const all = isAllSelected();
  const n = selected.size;
  const label = all
    ? `All subscriptions${allSubs.length ? ` (${allSubs.length})` : ""}`
    : n === 1
      ? allSubs.find((s) => selected.has(s.id))?.name || "1 subscription"
      : `${n} of ${allSubs.length} subscriptions`;

  $("pickerLabel").textContent = label;
  $("pickerBtn").setAttribute("aria-label", `Subscriptions to analyse: ${label}`);
  $("pickerBtn").classList.toggle("narrowed", !all);
  $("pickAll").textContent = all ? "Clear" : "Select all";

  describeScope(all, label);
}

function openPicker(open) {
  const menu = $("pickerMenu");
  const btn = $("pickerBtn");
  menu.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (open) menu.querySelector(".pick")?.focus();
}

// -------------------------------------------------------------------- wiring
/** Re-read the header figures. Exported so the refresh control can update them when an
    ingest finishes, rather than leaving yesterday's numbers above fresh tabs. */
export function refreshHeader() {
  loadGlance();
  loadWarehouse();
}

// ------------------------------------------------------------------- theme
const THEMES = ["dark", "light"];

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("costTheme", theme);
  } catch {
    /* private browsing — the choice just won't survive a reload */
  }
  // The meta tag drives the browser's own chrome on mobile; leaving it dark under a light
  // page is the kind of seam that makes a theme feel half-finished.
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", theme === "light" ? "#ffffff" : "#1B1A19");
  document.getElementById("themeBtn")?.setAttribute(
    "title", theme === "light" ? "Switch to dark" : "Switch to light");
  retintCharts();
}

function wireTheme() {
  const btn = $("themeBtn");
  if (!btn) return;
  applyTheme(currentTheme()); // sync the title and meta with whatever the inline script chose

  btn.onclick = () => applyTheme(currentTheme() === "light" ? "dark" : "light");

  // Follow the system only while the user hasn't expressed a preference of their own.
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
    try {
      if (localStorage.getItem("costTheme")) return;
    } catch {
      /* fall through and follow the system */
    }
    document.documentElement.dataset.theme = e.matches ? "light" : "dark";
    retintCharts();
  });
}

function wire() {
  watchScroll();

  const menu = $("pickerMenu");
  const btn = $("pickerBtn");

  btn.onclick = (e) => {
    e.stopPropagation();
    openPicker(menu.hidden);
  };
  menu.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => openPicker(false));

  // Roving keyboard navigation inside the picker.
  menu.addEventListener("keydown", (e) => {
    const items = [...menu.querySelectorAll(".pick")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const next = e.key === "ArrowDown" ? i + 1 : i - 1;
      items[(next + items.length) % items.length]?.focus();
    } else if (e.key === "Escape") {
      openPicker(false);
      btn.focus();
    } else if (e.key === "Tab") {
      openPicker(false);
    }
  });

  $("pickAll").onclick = () => {
    selected = isAllSelected() ? new Set([allSubs[0]?.id].filter(Boolean)) : new Set();
    writeStore("costScope", [...selected]);
    renderPicker();
    loadGlance();
    scopeChanged();
  };

  $("form").onsubmit = (e) => {
    e.preventDefault();
    ask($("q").value.trim());
  };

  $("signout").onclick = async () => {
    // Clear the remembered scope too: the next person at this browser shouldn't inherit
    // a filter they never chose.
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } catch {
      /* signing out locally still matters */
    }
    try {
      localStorage.removeItem("costScope");
    } catch {
      /* private browsing */
    }
    location.replace("/login");
  };

  $("reset").onclick = () => {
    history = [];
    chat.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.innerHTML = introTemplate;
    chat.appendChild(wrap.firstElementChild);
    $("q").focus();
    announce("Conversation cleared.");
  };
  $("stop").onclick = () => controller?.abort();
  $("jump").onclick = () => {
    pinned = true;
    chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
    $("jump").hidden = true;
  };

  chat.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (chip) ask(chip.textContent.trim());
  });

  const q = $("q");
  const autoGrow = () => {
    q.style.height = "auto";
    q.style.height = Math.min(q.scrollHeight, 160) + "px";
  };
  q.addEventListener("input", autoGrow);
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask(q.value.trim());
    }
  });
  q.focus();
}

/** Follow new content unless the user has scrolled away.
 *
 *  Auto-follow uses instant scrolling so the position we read back is always the final one;
 *  with smooth scrolling the mid-animation values look like the user scrolling away and the
 *  view detaches itself. Only the explicit Jump button animates.
 */
function watchScroll() {
  const near = () => chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  chat.addEventListener(
    "scroll",
    () => {
      pinned = near();
      $("jump").hidden = pinned;
    },
    { passive: true }
  );
}

function scroll(force = true) {
  if (force) chat.scrollTop = chat.scrollHeight;
}

// --------------------------------------------------------------------- ask
/**
 * Put a question to the agent from outside this module.
 *
 * The analysis view has its own ask bar — a reader who has just been told about an idle VM
 * should not have to find the side panel to ask which one. It calls this rather than filling
 * the textarea and firing a synthetic submit, which would break the moment the form changes.
 */
export function askAgent(text) {
  const q = String(text || "").trim();
  if (!q) return;
  ask(q);
}

async function ask(text) {
  if (!text || busy) return;
  busy = true;
  pinned = true;
  // Start fetching the markdown renderer now rather than when the first token lands: the
  // model takes seconds to think, which is ample time for 57 KB to arrive unnoticed.
  loadMarkdown();
  $("send").disabled = true;
  $("stop").hidden = false;
  document.querySelector(".intro")?.remove();

  const q = $("q");
  q.value = "";
  q.style.height = "auto";

  addYou(text);
  const view = addAgent();
  controller = new AbortController();
  const started = performance.now();
  announce("Working on your question.");
  // The analyst reflects what the model is doing. Announced as an event rather than by calling
  // into bot.js, so this module keeps knowing nothing about the launcher — app.js is imported
  // *by* the dashboard, and a dependency back the other way would be a cycle.
  window.dispatchEvent(new CustomEvent("cl:agent", { detail: "thinking" }));

  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, history, scope: scopeIds() }),
      signal: controller.signal,
    });
    if (res.status === 401) {
      view.note("Your session expired. Taking you back to sign in…");
      setTimeout(toLogin, 1200);
    } else if (res.status === 403) {
      // Almost always "you have no Azure subscriptions", not "this app forbids you". Saying
      // the latter sends people to ask the wrong person for the wrong thing.
      const body = await res.json().catch(() => ({}));
      view.fail(body.detail || "Your account isn't allowed to do that.", null);
    } else if (!res.ok) {
      view.fail(`Request failed: ${res.status} ${res.statusText}`, () => ask(text));
    } else {
      await consume(res.body, view);
    }
  } catch (err) {
    if (err.name === "AbortError") view.note("Stopped.");
    else view.fail(String(err.message || err), () => ask(text));
  } finally {
    view.finish(performance.now() - started);
    busy = false;
    controller = null;
    $("send").disabled = false;
    $("stop").hidden = true;
    q.focus();
    window.dispatchEvent(new CustomEvent("cl:agent", { detail: "done" }));
  }
}

async function consume(body, view) {
  const reader = body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  let speaking = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() || "";
    for (const f of frames) {
      let type = "message";
      const lines = [];
      for (const line of f.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7).trim();
        else if (line.startsWith("data: ")) lines.push(line.slice(6));
      }
      if (!lines.length) continue;
      // The moment the answer starts arriving the analyst stops thinking and starts explaining.
      // Fired once: the stream sends hundreds of `delta` frames and re-announcing on each would
      // restart the animation on every one.
      if (!speaking && type === "delta") {
        speaking = true;
        window.dispatchEvent(new CustomEvent("cl:agent", { detail: "explaining" }));
      }
      try {
        view.on(type, JSON.parse(lines.join("\n")));
      } catch {
        /* ignore a malformed frame rather than killing the stream */
      }
    }
  }
}

// ------------------------------------------------------------------ render
function addYou(text) {
  const n = document.createElement("div");
  n.className = "msg you";
  n.innerHTML = `<div class="who">You</div><div class="body"></div>`;
  n.querySelector(".body").textContent = text;
  chat.appendChild(n);
  scroll();
}

function addAgent() {
  const n = document.createElement("article");
  n.className = "msg agent";
  n.innerHTML = `
    <div class="who">CloudLens</div>
    <div class="steps"></div>
    <div class="charts"></div>
    <div class="live"><div class="thinking"><span class="spin"></span><span>Thinking…</span></div></div>
    <div class="banners"></div>
    <div class="body"></div>
    <div class="tools-row"><button class="copy ghost" hidden>Copy</button><span class="elapsed"></span></div>`;
  chat.appendChild(n);

  const steps = n.querySelector(".steps");
  const charts = n.querySelector(".charts");
  const live = n.querySelector(".live");
  const banners = n.querySelector(".banners");
  const body = n.querySelector(".body");
  const copy = n.querySelector(".copy");
  const elapsed = n.querySelector(".elapsed");

  const open = new Map();
  let md = "";
  let announcedAnswer = false;

  const say = (t) =>
    (live.innerHTML = t
      ? `<div class="thinking"><span class="spin"></span><span>${esc(t)}</span></div>`
      : "");

  return {
    on(type, d) {
      if (type === "tool") {
        const label = TOOL_LABEL[d.name] || d.name;
        if (d.status === "running") {
          say(`Reading ${label}…`);
          announce(`Reading ${label}`);
          const s = document.createElement("details");
          s.className = "step";
          s.dataset.id = d.id || d.name;
          s.innerHTML = `
            <summary>
              <span class="spin"></span>
              <span class="tname">${esc(label)}</span>
            </summary>
            <div class="inner">
              <div class="label">Arguments</div><pre>${esc(JSON.stringify(d.arguments, null, 2))}</pre>
              <div class="out"></div>
            </div>`;
          steps.appendChild(s);
          open.set(s.dataset.id, s);
        } else {
          const s = open.get(d.id || d.name);
          if (s) {
            const bad = d.status === "failed";
            s.querySelector("summary").innerHTML =
              `<span class="tick">${bad ? "✕" : "✓"}</span>` +
              `<span class="tname">${esc(label)}</span>` +
              `<span class="ms">${d.ms != null ? (d.ms / 1000).toFixed(1) + "s" : ""}</span>`;
            if (bad) {
              s.classList.add("failed");
              s.open = true;
            }
            s.querySelector(".out").innerHTML =
              `<div class="label">${bad ? "Error" : "Result"}</div>` +
              `<pre>${esc(JSON.stringify(d.result, null, 2))}</pre>`;
            open.delete(d.id || d.name);
          }
          say("Writing the answer…");
        }
        scroll(pinned);
      } else if (type === "chart") {
        say("");
        drawChart(charts, d).catch(() =>
          this.warn("The chart could not be drawn, but the figures above are unaffected.")
        );
        announce(`Chart ready: ${d.title || ""}`);
        scroll(pinned);
      } else if (type === "delta") {
        say("");
        md += d.text || "";
        body.innerHTML = render(md);
        body.classList.add("cursor");
        if (!announcedAnswer) {
          announce("Answer is being written.");
          announcedAnswer = true;
        }
        scroll(pinned);
      } else if (type === "done") {
        history = d.messages || history;
        say("");
      } else if (type === "error") {
        this.fail(d.message);
      }
    },
    note(msg) {
      live.innerHTML = `<div class="thinking">${esc(msg)}</div>`;
    },
    warn(msg) {
      const b = document.createElement("div");
      b.className = "banner warn";
      b.textContent = msg;
      banners.appendChild(b);
    },
    fail(msg, retry) {
      live.innerHTML = "";
      const b = document.createElement("div");
      b.className = "banner err";
      b.textContent = msg;
      if (retry) {
        const again = document.createElement("button");
        again.textContent = "Try again";
        again.onclick = () => {
          b.remove();
          retry();
        };
        b.appendChild(again);
      }
      banners.appendChild(b);
      announce(`Error: ${msg}`);
      scroll(pinned);
    },
    finish(ms) {
      say("");
      body.classList.remove("cursor");
      enhance(body);
      if (md) {
        copy.hidden = false;
        copy.onclick = async () => {
          try {
            await navigator.clipboard.writeText(md);
            copy.textContent = "Copied";
            setTimeout(() => (copy.textContent = "Copy"), 1400);
          } catch {
            copy.textContent = "Press Ctrl+C";
          }
        };
        elapsed.textContent = `${(ms / 1000).toFixed(1)}s`;
        announce("Answer complete.");
      }
      scroll(pinned);
    },
  };
}

// ------------------------------------------------------------------ charts
// A single-hue ramp, not a rainbow. Chart libraries default to maximally distinct hues so that
// twelve arbitrary categories stay separable — but these categories are not arbitrary, they are
// *ordered by cost*, and a sequence that steps monotonically encodes that ordering in the colour
// itself. The eye reads "biggest" before it reads any label.
//
// Distinct hues would say each area is a different kind of thing. They are not: they are all
// spend. One hue also keeps the chart from competing with the accent, which is reserved for
// things a person can act on.
//
// The ramp runs in opposite directions per theme, because "prominent" is light on a dark
// surface and dark on a light one. A single fixed ramp would put the largest slice closest to
// the background in one of the two themes, which is exactly backwards.
//
// Both are one blue family — the same hue the mark and the accent use, so a chart and the logo
// above it are the same blue rather than two that nearly match, which is worse than not matching
// at all. The last two entries are neutral, for the long tail of small slices that should
// recede rather than compete.
const RAMP_DARK = [
  "#9FCFFA", "#8AC2F8", "#74B5F7", "#5FA8F6", "#479EF5",
  "#3590E8", "#2886DE", "#1E76C8", "#175FA3", "#124B80",
  "#A19F9D", "#797775",
];
const RAMP_LIGHT = [
  "#004578", "#005A9E", "#106EBE", "#0078D4", "#2B88D8",
  "#4FA0DD", "#7BB8E5", "#A6D0EE", "#CCE4F6", "#E5F1FB",
  "#605E5C", "#A19F9D",
];
const palette = () =>
  document.documentElement.dataset.theme === "light" ? RAMP_LIGHT : RAMP_DARK;

// The ordered ramp above is right for slices ranked by cost and wrong for named categories.
// "On demand", "Committed" and "Spot" are not more and less of one thing, they are three
// different things — and rendered from a single-hue ramp they came out as three barely
// distinguishable blues, which is a legend you have to decode rather than read.
//
// Hue order is chosen for colour vision deficiency, not just for contrast against each other:
// blue and amber are the one pair that stays separable under every common form of CVD, so they
// take the first two slots. Green third, then cyan — those four are the supporting palette, and
// a chart needing more than four series has a problem colour cannot fix. Two greys follow rather
// than more hues, because at that point the honest signal is "and the rest".
//
// Series also get their own point style (circle / triangle / rectangle …), so the legend does
// not rely on colour alone. That is the actual fix: colour is a hint, shape is the answer.
const CATEGORICAL_DARK = [
  "#479EF5", "#FFB900", "#6BB700", "#2ED2E0", "#A19F9D", "#797775",
];
const CATEGORICAL_LIGHT = [
  "#0078D4", "#8A6100", "#107C10", "#0E7C86", "#605E5C", "#8A8886",
];
const categorical = () =>
  document.documentElement.dataset.theme === "light" ? CATEGORICAL_LIGHT : CATEGORICAL_DARK;

// Categories that appear in more than one chart get a fixed slot, so "Spot" is the same colour
// in the doughnut and in the stacked bar below it — even though it is the third series in one
// and whichever position the data happened to yield in the other. Two charts of the same three
// things in different colours is worse than no colour coding at all.
//
// Anything not named here falls back to its position, which is correct for one-off series.
const CATEGORY_SLOT = new Map([
  ["on demand", 0], ["ondemand", 0],
  ["committed", 1], ["reserved", 1],
  ["spot", 2],
  ["other", 4],
  ["unknown", 5],
]);
const slotFor = (label, fallback) => {
  const key = String(label ?? "").trim().toLowerCase();
  return CATEGORY_SLOT.has(key) ? CATEGORY_SLOT.get(key) : fallback;
};

// Redundant encoding for the legend and the hover marker. Ordered so the first two — the most
// common case — are the most different from each other.
const POINT_STYLES = ["circle", "triangle", "rect", "rectRot", "star", "crossRot"];

const TICK_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * Axis labels, shortened to what a reader actually needs.
 *
 * A daily series arrives as ISO dates, which are 10 characters that collide at any useful
 * density — so the previous version tilted them 45°. Rotated text is slower to read and eats
 * a third of the plot height. "12 Aug" is unambiguous in context and fits horizontally, which
 * buys back the space and the legibility at once.
 */
function shortenTick(raw) {
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw);
  if (iso) return `${Number(iso[3])} ${TICK_MONTHS[Number(iso[2]) - 1]}`;
  return raw.length > 16 ? raw.slice(0, 15) + "\u2026" : raw;
}

const CHART_KIND = {
  line: "line", area: "line", bar: "bar", hbar: "bar",
  stacked_bar: "bar", pie: "pie", doughnut: "doughnut",
};

let chartLib = null;
/** Load Chart.js on first use — it is 200KB and most questions never need it. */
function loadChartLib() {
  if (chartLib) return chartLib;
  chartLib = new Promise((resolve, reject) => {
    if (window.Chart) return resolve(window.Chart);
    const s = document.createElement("script");
    s.src = "/assets/vendor/chart.umd.min.js";
    s.onload = () => (window.Chart ? resolve(window.Chart) : reject(new Error("Chart.js missing")));
    s.onerror = () => reject(new Error("Chart.js failed to load"));
    document.head.appendChild(s);
  });
  return chartLib;
}

/** Read a design token at draw time, so charts follow the theme without duplicating its palette. */
function token(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// Live charts, so a theme change can restyle them in place. A WeakSet would not let us iterate,
// and re-fetching every tab to recolour a canvas would be absurd.
const liveCharts = new Set();

/** Restyle every chart on screen for the current theme. Options only — no data is refetched. */
/**
 * The colour a dataset should be, given how many there are and what it is.
 *
 * One function for both the first draw and every re-tint. These were two implementations —
 * `drawChart` chose categorical hues and spread ordered series across the ramp; `retintCharts`
 * assigned `ramp[i]` — so a theme switch silently converted a legible chart into two adjacent
 * shades of the same blue. A chart that changes meaning when someone toggles dark mode is worse
 * than one that was never right.
 */
function seriesColour({ index, label, count, categorical: cat, isPie }) {
  const ramp = palette();
  const cats = categorical();
  if (cat) return cats[slotFor(label, index) % cats.length];
  // Adjacent entries of the ordered ramp are one step apart and barely distinguishable — the
  // point when they are ranked slices of one series, a defect when they are separate lines.
  // Spread them across the legible first half instead; the tail is deliberately close to the
  // background so small slices recede, and a whole line rendered there disappears.
  const BAND = 5;
  const spread = !isPie && count > 1
    ? Math.round((index * BAND) / Math.max(1, count - 1))
    : index;
  return ramp[spread % ramp.length];
}

export function retintCharts() {
  const grid = token("--grid-line", "rgba(255,255,255,.06)");
  const tick = token("--muted", "#8f98a6");
  const surface = token("--surface", "#14181e");
  const surface2 = token("--surface-2", "#1b2027");
  const borderStrong = token("--border-strong", "#333b47");
  const text = token("--text", "#e9ebef");

  for (const chart of liveCharts) {
    if (!chart.canvas?.isConnected) {
      liveCharts.delete(chart); // the tab it belonged to has been replaced
      continue;
    }
    const o = chart.options;
    if (o.plugins?.legend?.labels) o.plugins.legend.labels.color = tick;
    if (o.plugins?.tooltip) {
      Object.assign(o.plugins.tooltip, {
        backgroundColor: surface2, borderColor: borderStrong,
        titleColor: text, bodyColor: text,
      });
    }
    for (const axis of Object.values(o.scales || {})) {
      if (axis.grid) axis.grid.color = grid;
      if (axis.ticks) axis.ticks.color = tick;
    }
    // Slices and series are re-coloured in place — still no refetch, just a different set of
    // strings assigned to the same data. Both go through `seriesColour`, so a re-tint produces
    // exactly what the first draw produced, in the other theme's values.
    const ramp = palette();
    const cats = categorical();
    // Recorded at draw time: whether this chart's series are categories or a ranked sequence.
    // Re-deriving it here would guess, and guessing is what made the two paths disagree.
    const isCat = chart.$clCategorical === true;
    const isPieChart = chart.config?.type === "pie" || chart.config?.type === "doughnut";
    const count = chart.data.datasets.length;
    for (const [i, ds] of chart.data.datasets.entries()) {
      if (Array.isArray(ds.backgroundColor)) {
        const labels = chart.data.labels || [];
        ds.backgroundColor = ds.backgroundColor.map((_, j) =>
          isCat ? cats[slotFor(labels[j], j) % cats.length] : ramp[j % ramp.length]);
      } else if (ds.borderColor && !Array.isArray(ds.borderColor)) {
        const colour = seriesColour({
          index: i, label: ds.label, count, categorical: isCat, isPie: isPieChart,
        });
        ds.borderColor = colour;
        if (typeof ds.backgroundColor === "string") {
          ds.backgroundColor = ds.backgroundColor.length === 9 ? colour + "33" : colour;
        }
      }
      if (ds.borderWidth === 2 && Array.isArray(ds.backgroundColor)) ds.borderColor = surface;
    }
    chart.update("none"); // no animation: a theme switch should feel instant
  }
}

export async function drawChart(container, spec) {
  // Accept an element or an id. Every call site but one passed an element; the odd one out
  // passed a string and threw `container.appendChild is not a function` at runtime — invisible
  // to every static check, and only on a tab most people open occasionally. An implicit
  // contract that fails this quietly is better made explicit.
  const host = typeof container === "string" ? document.getElementById(container) : container;
  if (!host) return null;

  const Chart = await loadChartLib();

  const kind = CHART_KIND[spec.type] || "bar";
  const isPie = kind === "pie" || kind === "doughnut";
  const stacked = spec.type === "stacked_bar";
  const horizontal = spec.type === "hbar";

  const card = document.createElement("figure");
  card.className = "chart";
  card.innerHTML = `
    <div class="chart-head">
      <figcaption>${esc(spec.title || "")}</figcaption>
      <div class="chart-actions">
        <button type="button" data-act="png" title="Download as PNG">PNG</button>
        <button type="button" data-act="csv" title="Download the underlying data">CSV</button>
      </div>
    </div>
    <div class="canvas-wrap"><canvas role="img"></canvas></div>
    ${spec.note ? `<p class="chart-note">${esc(spec.note)}</p>` : ""}`;
  host.appendChild(card);

  const bars = spec.labels.length;
  // `spec.height` for callers that know better than the default. A chart repeated once per
  // item in a list — one per budget, say — is read as a row in a list rather than as a panel,
  // and at the standard height ten of them make a page nobody scrolls to the end of.
  const height = spec.height
    ? spec.height
    : horizontal
      ? Math.max(220, Math.min(bars * 28 + 70, 640))
      : isPie
        ? 260
        : Math.min(300, Math.max(220, window.innerHeight * 0.3));
  card.querySelector(".canvas-wrap").style.height = `${height}px`;

  const canvas = card.querySelector("canvas");
  canvas.setAttribute(
    "aria-label",
    `${spec.title}. ${spec.datasets.length} series across ${bars} categories.`
  );

  // Several series means several *things*, so they need distinct hues; one series split into
  // slices means one thing ranked, so it keeps the ordered ramp. Either can say so explicitly:
  // a period-over-period comparison is two series of the *same* thing, and giving the reference
  // line its own hue would promote it from context to peer.
  const manySeries = !isPie && spec.datasets.length > 1;
  const useCategorical = spec.categorical ?? manySeries;

  const datasets = spec.datasets.map((ds, i) => {
    const ramp = palette();
    const cats = categorical();
    const slot = useCategorical ? slotFor(ds.label, i) : i;
    const colour = seriesColour({
      index: i, label: ds.label, count: spec.datasets.length,
      categorical: useCategorical, isPie,
    });
    if (isPie) {
      const slice = (j) =>
        useCategorical
          ? cats[slotFor(spec.labels[j], j) % cats.length]
          : ramp[j % ramp.length];
      return {
        label: ds.label,
        data: ds.data,
        backgroundColor: spec.labels.map((_, j) => slice(j)),
        // Slices are separated by a ring the colour of the card behind them, so it reads as a
        // gap rather than an outline.
        borderColor: token("--surface", "#14181e"),
        borderWidth: 2,
      };
    }
    return {
      label: ds.label,
      data: ds.data,
      // Per-point labels for series aligned by position rather than by date. Carried on the
      // dataset so the tooltip can name the day each number actually came from.
      pointLabels: ds.pointLabels,
      borderColor: colour,
      backgroundColor: kind === "line" ? colour + "33" : colour,
      fill: spec.type === "area" && !ds.dash,
      // Gentler than a spline: enough to read as a trend, not so much that it invents peaks
      // between two points that were never measured.
      tension: 0.22,
      pointRadius: bars > 40 ? 0 : 2.5,
      pointHoverRadius: 4,
      borderWidth: ds.dash ? 1.5 : 2,
      borderRadius: kind === "bar" ? 3 : 0,
      // Shape as well as colour, so the legend survives being printed in grey or read by
      // someone who cannot tell the hues apart.
      pointStyle: POINT_STYLES[slot % POINT_STYLES.length],
      // A comparison series has to recede: same shape, dashed and unfilled, so the eye reads
      // it as the reference rather than as a second thing of equal weight.
      ...(ds.dash ? { borderDash: [4, 4], pointRadius: 0, pointHoverRadius: 3 } : {}),
    };
  });

  const fmt = (v) =>
    typeof v === "number"
      ? v.toLocaleString(undefined, { maximumFractionDigits: 2 })
      : v;

  const gridColour = token("--grid-line", "rgba(255,255,255,.06)");
  const tickColour = token("--muted", "#8f98a6");
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  const chart = new Chart(canvas, {
    type: kind,
    data: { labels: spec.labels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: reduceMotion ? false : { duration: 400 },
      indexAxis: horizontal ? "y" : "x",
      interaction: { mode: isPie ? "nearest" : "index", intersect: false },
      plugins: {
        legend: {
          display: isPie || datasets.length > 1,
          position: isPie ? "right" : "top",
          align: isPie ? "center" : "end",
          labels: {
            color: tickColour, boxWidth: 8, boxHeight: 8,
            font: { size: 11 },
            // Each series keeps its own marker shape here rather than every entry being a
            // circle — the point of the shapes is that the legend does not depend on colour.
            // Pie slices stay circular: they share one dataset and are told apart by position.
            usePointStyle: true,
            ...(isPie ? { pointStyle: "circle" } : {}),
            padding: 14,
          },
        },
        tooltip: {
          backgroundColor: token("--surface-2", "#1b2027"),
          borderColor: token("--border-strong", "#333b47"),
          borderWidth: 1,
          titleColor: token("--muted", "#8f98a6"),
          bodyColor: token("--text", "#e9ebef"),
          padding: 10,
          cornerRadius: 4,
          displayColors: !isPie,
          boxWidth: 8,
          boxHeight: 8,
          usePointStyle: true,
          titleFont: { size: 11, weight: "600" },
          bodyFont: { size: 12 },
          callbacks: {
            label: (c) => {
              const v = fmt(isPie ? c.parsed : c.parsed.y ?? c.parsed.x);
              // A dataset can carry its own per-point labels — used where two series are
              // aligned by *position* rather than by date, as the daily-spend comparison is.
              // Without this the tooltip shows one date above two numbers, and a reader quite
              // reasonably assumes both were measured on it. They were not: the second is the
              // same day of the previous window, which is the whole point of the comparison.
              const own = c.dataset.pointLabels?.[c.dataIndex];
              const name = c.dataset.label || c.label;
              return ` ${own ? `${name} (${own})` : name}: ${v}`;
            },
          },
        },
      },
      scales: isPie
        ? {}
        : {
            // With indexAxis:'y' the categories sit on y and the values on x, so the number
            // formatter has to follow the value axis or the labels become indices.
            x: {
              stacked,
              // Gridlines only on the axis that carries quantity. Lines running down through
              // a time series are a grid for its own sake: nobody reads a value off them, and
              // they cut every shape on the chart into slices.
              grid: { display: horizontal, color: gridColour, drawBorder: false },
              border: { color: gridColour },
              ticks: {
                color: tickColour, font: { size: 11 },
                maxRotation: 0, autoSkipPadding: 12, padding: 6,
                ...(horizontal
                  ? { callback: fmt }
                  : {
                      // Chart.js drops category labels when they collide. On a short
                      // series that silently hides which bar is which, so keep every
                      // label and shorten the long ones instead.
                      autoSkip: spec.labels.length > 8,
                      callback(value, index) {
                        const raw = String(spec.labels[index] ?? "");
                        return shortenTick(raw);
                      },
                    }),
              },
            },
            y: {
              stacked,
              // Money starts at zero. A baseline cropped to the data makes a 5% wobble look
              // like a crisis, which on a cost chart is not a style choice but a wrong answer.
              beginAtZero: !horizontal,
              grid: { display: !horizontal, color: gridColour, drawBorder: false },
              border: { display: false },
              ticks: {
                color: tickColour, font: { size: 11 }, padding: 8, maxTicksLimit: 6,
                ...(horizontal ? { autoSkip: false, crossAlign: "far" } : { callback: fmt }),
              },
            },
          },
    },
  });

  // Recorded on the chart so a re-tint can reproduce exactly this decision rather than guessing
  // it back from the rendered colours.
  chart.$clCategorical = useCategorical;
  liveCharts.add(chart);

  // Optional drill-down. Chart.js reports the element under the pointer; the caller gets the
  // category index and does whatever it likes with it. Kept opt-in so charts that have nothing
  // behind them do not grow a pointer cursor that promises a click and then does nothing.
  if (typeof spec.onSelect === "function") {
    canvas.style.cursor = "pointer";
    canvas.onclick = (ev) => {
      const hit = chart.getElementsAtEventForMode(ev, "index", { intersect: false }, true);
      if (hit.length) spec.onSelect(hit[0].index, spec.labels[hit[0].index]);
    };
  }

  card.querySelector('[data-act="png"]').onclick = () => {
    const a = document.createElement("a");
    a.href = chart.toBase64Image("image/png", 1);
    a.download = `${slug(spec.title)}.png`;
    a.click();
  };
  card.querySelector('[data-act="csv"]').onclick = () => downloadCsv(spec);
}

function slug(s) {
  return (s || "chart").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
}

function downloadCsv(spec) {
  const cell = (v) => {
    const s = String(v ?? "");
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const header = ["label", ...spec.datasets.map((d) => d.label)];
  const lines = [header.map(cell).join(",")];
  spec.labels.forEach((lab, i) => {
    lines.push([lab, ...spec.datasets.map((d) => d.data[i] ?? "")].map(cell).join(","));
  });
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${slug(spec.title)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

boot();

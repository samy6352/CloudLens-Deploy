/**
 * The AI Analysis view.
 *
 * Takes over the board rather than living in the side panel, because this is the one screen in
 * the app whose entire job is to be read. A 400px column turns a paragraph of reasoning into
 * eight lines of column-width prose, and the findings here are arguments — cause, trade-off,
 * sequence — not table rows. They need the width.
 *
 * The design rule throughout: a claim and its warrant travel together. Every finding shows what
 * it is worth, how confident that is, which tab the number came from, what is likely causing
 * it, what could go wrong if you act, and — behind one click — the full derivation. Nothing here
 * is generated prose; the server builds findings from measured values and this renders them.
 */
import { esc, money } from "./app.js";
import { setBotCount, setBotState } from "./bot.js";

const $ = (id) => document.getElementById(id);

let analysis = null;
let running = false;
let open = false;
// Whether a conversation is showing inside the panel. Tracked rather than read from the DOM,
// because `render()` rebuilds the suggestions and would otherwise put them back over a live
// answer every time the analysis redraws.
let talking = false;

const SEVERITY_WORD = {
  critical: "Act now",
  high: "High impact",
  medium: "Worth doing",
  low: "Minor",
};

const CONFIDENCE_WORD = {
  high: "Measured",
  medium: "Strong signal",
  low: "Needs judgement",
};

/**
 * Where the analysis gets its scope, period and currency.
 *
 * Injected rather than imported: this module renders a view, and the period and subscription
 * picker belong to the dashboard. Reaching across for them — or worse, reading globals — would
 * couple the two in the direction that makes both harder to test.
 */
let context = () => ({ days: 30, scope: [], currency: "" });

export function setAnalysisContext(fn) {
  context = fn;
}

/** Colour is never the only carrier: every badge is also a word. */
function badge(kind, value) {
  const word = kind === "severity"
    ? (SEVERITY_WORD[value] || value)
    : (CONFIDENCE_WORD[value] || value);
  return `<span class="an-badge an-${kind}-${esc(value)}">${esc(word)}</span>`;
}

function evidenceTable(f) {
  if (!(f.evidence || []).length) return "";
  return `<table class="an-evidence"><tbody>${f.evidence.map((e) => {
    const name = e.name || e.problem || e.key || e.day || e.resource || "—";
    const val = e.cost ?? e.savings ?? e.spend ?? e.saving ?? e.untagged ?? null;
    const extra = e.detail || e.group || e.size
      || (e.avg_cpu != null ? `${e.avg_cpu}% avg CPU · peak ${e.max_cpu}%` : "")
      || (e.percent != null ? `${e.percent}% of limit` : "")
      || (e.share_pct != null ? `${e.share_pct}% of spend` : "");
    return `<tr>
        <td class="an-ev-name">${esc(String(name).slice(0, 70))}</td>
        <td class="an-ev-extra">${esc(String(extra || "").slice(0, 48))}</td>
        <td class="an-ev-val">${val != null ? esc(money(val, f.currency)) : ""}</td>
      </tr>`;
  }).join("")}</tbody></table>`;
}

function findingCard(f, i) {
  const cur = f.currency;
  // A finding that honestly claims no saving shows no figure rather than a zero: "$0.00" reads
  // as "we checked and it is worth nothing", which is the opposite of what it means.
  const figure = f.impact > 0
    ? `<div class="an-impact">
         <span class="an-amount">${esc(money(f.impact, cur))}</span>
         <span class="an-period">${f.impact_period === "year" ? "per year" : `over ${
           analysis?.days || 30} days`}</span>
       </div>`
    : `<div class="an-impact an-no-figure"><span class="an-period">No saving claimed</span></div>`;

  return `<article class="an-card an-sev-${esc(f.severity)}" style="--i:${i}">
      <div class="an-num" aria-hidden="true">${i + 1}</div>

      <div class="an-main">
        <header class="an-card-head">
          <div class="an-titles">
            <h3>${esc(f.title)}</h3>
            <p class="an-detail">${esc(f.detail)}</p>
          </div>
          ${figure}
        </header>

        <div class="an-meta">
          ${badge("severity", f.severity)}
          ${badge("confidence", f.confidence)}
          ${f.effort ? `<span class="an-effort">${esc(f.effort)}</span>` : ""}
          <button class="an-source" data-goto="${esc(f.tab)}" type="button"
                  title="Open the tab this came from">${esc(f.source)} →</button>
        </div>

        <div class="an-reasoning">
          ${f.because ? `<div class="an-block">
              <h4>What is likely going on</h4>
              <p>${esc(f.because)}</p>
            </div>` : ""}
          ${f.risk ? `<div class="an-block an-block-risk">
              <h4>Before you act</h4>
              <p>${esc(f.risk)}</p>
            </div>` : ""}
        </div>

        ${(f.steps || []).length ? `<div class="an-block an-block-steps">
            <h4>How to approach it</h4>
            <ol class="an-steps">${f.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
          </div>` : ""}

        ${f.caveat ? `<p class="an-caveat">${esc(f.caveat)}</p>` : ""}

        <div class="an-foot">
          <details class="an-how">
            <summary>How was this calculated?</summary>
            <p class="an-basis">${esc(f.basis)}</p>
            ${evidenceTable(f)}
          </details>
          <button type="button" class="an-followup" data-q="${esc(followupFor(f))}">
            Ask about this
          </button>
        </div>
      </div>
    </article>`;
}

/** The question someone would actually type after reading this finding. */
function followupFor(f) {
  switch (f.kind) {
    case "waste": return `Which resources are behind "${f.title}", and what has each cost?`;
    case "rightsizing": return "Show the idle VMs with their sizes, CPU and monthly cost.";
    case "schedule": return "Which VMs could be scheduled, and what would each save?";
    case "anomaly": return `What changed on ${f.evidence?.[0]?.day || "that day"}, by resource?`;
    case "budget": return "Which budgets are over, by how much, and what is driving each?";
    case "advisor": return "List the Advisor cost recommendations with their savings.";
    case "governance": return "Which untagged resources cost the most?";
    case "commitment": return "Which compute is steady enough to be worth a reservation?";
    default: return `Tell me more about "${f.title}".`;
  }
}

/**
 * Questions worth offering, given what the analysis found.
 *
 * Two thirds specific, one third general. The specific ones come from the findings, so they name
 * real resources and real numbers — "which two VMs?" is a question someone actually has after
 * reading that two VMs are idle. The general ones are there because the agent answers anything
 * about Azure, and a box that only ever suggests follow-ups teaches people it only does
 * follow-ups.
 */
function suggestions() {
  const out = [];
  const seen = new Set();
  const add = (label, q) => {
    // Two anomalies produce two "What caused the spike?" chips — same words, different
    // questions, and a row that repeats itself reads as a rendering fault rather than a choice.
    if (seen.has(label)) return;
    seen.add(label);
    out.push({ label, q });
  };

  for (const f of (analysis?.findings || []).slice(0, 4)) {
    if (out.length >= 3) break;
    add(shortLabel(f), followupFor(f));
  }

  // Always available, whatever was found. These are the questions people arrive with.
  const general = [
    { label: "Biggest movers", q: "What changed most in my Azure spend this month, and why?" },
    { label: "Cut costs", q: "What are my top opportunities to reduce Azure spend, in order?" },
    { label: "Forecast", q: "What will my Azure bill be at the end of the month?" },
    { label: "By subscription", q: "Break my spend down by subscription and resource group." },
  ];
  for (const g of general) {
    if (out.length >= 5) break;
    add(g.label, g.q);
  }
  return out;
}

/** A two-or-three word handle for a finding, for a chip that has to fit on one line. */
function shortLabel(f) {
  switch (f.kind) {
    case "waste": return "Which resources?";
    case "rightsizing": return "Which VMs?";
    case "schedule": return "Which to schedule?";
    case "anomaly": return "What caused the spike?";
    case "budget": return "Why over budget?";
    case "advisor": return "Advisor detail";
    case "governance": return "What is untagged?";
    case "commitment": return "Worth reserving?";
    default: return "Tell me more";
  }
}

function renderSuggestions() {
  const host = $("anSuggest");
  if (!host) return;
  host.innerHTML = suggestions()
    .map((s) => `<button type="button" class="an-chip" data-q="${esc(s.q)}"
                   title="${esc(s.q)}">${esc(s.label)}</button>`)
    .join("");
  for (const b of host.querySelectorAll("[data-q]")) {
    b.onclick = () => sendQuestion(b.dataset.q);
  }
  // Re-rendering must not bring the chips back over a live conversation. `render()` runs
  // whenever the analysis is redrawn, and without this a follow-up answer arrived with the
  // suggestions sitting on top of it.
  host.hidden = talking;
}

/**
 * Hand a question to the agent, in place.
 *
 * The analysis stays open and the ask bar stays where it is; the suggestion chips give way to
 * the answer streaming in above them. Sending someone to a different panel to read the reply
 * would mean the finding they asked about is no longer on screen — which is the one thing they
 * need while reading it.
 */
function sendQuestion(q) {
  const text = String(q || "").trim();
  if (!text) return;
  const box = $("anAsk");
  if (box) box.value = "";
  // The chat panel still owns the conversation — its renderer handles tool steps, charts and
  // streaming. This asks it to answer without stealing the screen.
  window.dispatchEvent(new CustomEvent("cl:ask", { detail: { q: text, inPlace: true } }));
  showConversation();
}

/**
 * Give the conversation the whole panel.
 *
 * An answer is often a table of twenty resource groups; in a strip at the foot of the page that
 * is a scroll box inside a scroll box. The findings step aside instead — they are one click
 * back, not gone, and the ask bar stays exactly where it was so the next question is typed in
 * the same place as the last.
 */
function showConversation() {
  const suggest = $("anSuggest");
  const host = $("anConversation");
  const body = $("analysisBody");
  const chat = $("chat");
  if (!host || !chat) return;
  talking = true;
  if (suggest) suggest.hidden = true;
  if (body) body.hidden = true;
  if (chat.parentElement !== host) {
    host.appendChild(chat);
    // The chat element carries its own scroller; in here it fills the panel rather than
    // keeping the side panel's height.
    chat.classList.add("in-analysis");
  }
  host.hidden = false;
  requestAnimationFrame(() => { chat.scrollTop = chat.scrollHeight; });
}

/** Back to the findings. The transcript stays where it is — returning to it is one click. */
function showFindings() {
  talking = false;
  const host = $("anConversation");
  const body = $("analysisBody");
  const suggest = $("anSuggest");
  if (host) host.hidden = true;
  if (body) body.hidden = false;
  if (suggest) suggest.hidden = false;
}

/** Put the conversation back in the side panel — on close, so the chat works there again. */
function hideConversation() {
  talking = false;
  const host = $("anConversation");
  const body = $("analysisBody");
  const chat = $("chat");
  const shell = document.querySelector(".chat-shell");
  if (chat && shell && chat.parentElement !== shell) {
    shell.insertBefore(chat, shell.firstChild);
    chat.classList.remove("in-analysis");
  }
  if (host) host.hidden = true;
  if (body) body.hidden = false;
  const suggest = $("anSuggest");
  if (suggest) suggest.hidden = false;
}

function render() {
  const body = $("analysisBody");
  if (!body || !analysis) return;

  const d = analysis;
  const findings = d.findings || [];
  const s = d.summary || {};
  const unavailable = Object.entries(d.unavailable || {});
  const money0 = (v) => money(v, d.currency);

  if (!findings.length) {
    body.innerHTML = `
      <div class="an-clear">
        <h2>${esc(s.headline || "Nothing worth flagging")}</h2>
        <p>${esc(s.situation || "")}</p>
        ${unavailableNote(unavailable)}
        <button class="ghost" id="rerun">Run again</button>
      </div>`;
    wire();
    return;
  }

  body.innerHTML = `
    <section class="an-brief">
      <div class="an-brief-main">
        <p class="an-eyebrow">Analysis · last ${d.days} days · ${esc(d.currency)}</p>
        <h2>${esc(s.headline || "")}</h2>
        <p class="an-situation">${esc(s.situation || "")}</p>
        ${s.priority ? `<p class="an-priority">${esc(s.priority)}</p>` : ""}
      </div>
      <aside class="an-brief-figure">
        <span class="an-brief-amount">${esc(money0(d.addressable))}</span>
        <span class="an-brief-label">addressable over ${d.days} days</span>
        <span class="an-brief-sub">${findings.length} findings${
          d.subscriptions ? ` · ${d.subscriptions} subscription${
            d.subscriptions === 1 ? "" : "s"}` : ""}</span>
      </aside>
    </section>

    ${(s.sequence || []).length ? `<section class="an-order">
        <h3>Where to start</h3>
        <ol class="an-order-list">${s.sequence.map((step, i) => `
          <li><span class="an-order-n">${i + 1}</span>
            <span class="an-order-t">${esc(step.title)}</span>
            <span class="an-order-w">${esc(step.why)}</span>
            ${step.impact > 0 ? `<span class="an-order-v">${esc(money0(step.impact))}</span>` : ""}
          </li>`).join("")}</ol>
      </section>` : ""}

    ${unavailableNote(unavailable)}

    <div class="an-list">${findings.map(findingCard).join("")}</div>

    <details class="an-method">
      <summary>How this analysis works</summary>
      <p>${esc(d.method)}</p>
      <p class="an-stamp">Read ${esc(d.generated_at || "")} · figures in ${esc(d.currency)}</p>
    </details>

    <div class="an-rerun"><button class="ghost" id="rerun">Run the analysis again</button></div>`;
  wire();
}

function unavailableNote(entries) {
  if (!entries.length) return "";
  // Named rather than swallowed: a shorter list with no explanation invites the reader to
  // conclude there was nothing to find, which is a different statement entirely.
  return `<div class="an-gap">
      <strong>${entries.length} source${entries.length !== 1 ? "s" : ""} could not be read.</strong>
      ${entries.map(([k]) => esc(k)).join(", ")} — findings from
      ${entries.length !== 1 ? "these are" : "this is"} missing below.
    </div>`;
}

function wire() {
  const rerun = $("rerun");
  if (rerun) rerun.onclick = () => run(true);

  for (const b of $("analysisBody").querySelectorAll("[data-goto]")) {
    b.onclick = () => {
      if (!b.dataset.goto) return;
      close();
      window.dispatchEvent(new CustomEvent("cl:goto-tab", { detail: b.dataset.goto }));
    };
  }
  // "Ask about this" goes through the same path as the ask bar, so a follow-up from a finding
  // behaves identically to one typed by hand — and leaves the finding on screen.
  for (const b of $("analysisBody").querySelectorAll("[data-q]")) {
    b.onclick = () => sendQuestion(b.dataset.q);
  }
  renderSuggestions();
  // Re-rendering the findings must not yank the reader out of a conversation. `render()` runs
  // on every redraw; without this, an answer arriving would be replaced by the findings behind
  // it mid-sentence.
  const body = $("analysisBody");
  if (body) body.hidden = talking;
}

/** Run the analysis, driving the bot through the states it is actually in. */
export async function run(force = false) {
  if (running) return;
  if (analysis && !force) { render(); return; }

  running = true;
  setBotState("analyzing");
  const body = $("analysisBody");
  const started = Date.now();

  // A named wait with a running clock. The alternative — a spinner — says only "something is
  // happening", and after fifteen seconds people stop believing it.
  body.innerHTML = `
    <div class="an-running">
      <div class="an-running-inner">
        <p class="an-step" id="anStep">Reading spend, resources and utilisation…</p>
        <div class="an-bar"><div class="an-bar-fill" id="anFill"></div></div>
        <p class="an-clock"><span id="anClock">0s</span> · reading eight sources across every
          subscription in scope</p>
      </div>
    </div>`;

  // Weighted to what actually takes the time: the live Azure calls dominate, so the bar moves
  // quickly through the warehouse reads and then slows, rather than pretending to be linear.
  const steps = [
    [0, "Reading spend, resources and utilisation…"],
    [6, "Checking idle and orphaned resources…"],
    [13, "Looking for anomalies and budget breaches…"],
    [20, "Cross-referencing and ranking…"],
  ];
  const tick = setInterval(() => {
    const s = Math.round((Date.now() - started) / 1000);
    const clock = $("anClock");
    if (clock) clock.textContent = `${s}s`;
    const step = [...steps].reverse().find(([at]) => s >= at);
    const el = $("anStep");
    if (el && step) el.textContent = step[1];
    const fill = $("anFill");
    // Approaches 92% asymptotically and never claims to be finished before it is.
    if (fill) fill.style.width = `${Math.min(92, 100 - 100 * Math.exp(-s / 11))}%`;
  }, 250);

  try {
    const { days, scope, currency } = context();
    const p = new URLSearchParams({ days: String(days || 30) });
    if (scope?.length) p.set("scope", scope.join(","));
    if (currency) p.set("currency", currency);

    const r = await fetch(`/api/insights?${p}`);
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    analysis = await r.json();

    const findings = analysis.findings || [];
    const urgent = findings.some((f) => f.severity === "critical");
    setBotCount(findings.length);
    // The state reports what was found, not that the request completed.
    setBotState(urgent ? "alert" : findings.length ? "insight" : "celebrating", { hold: 6000 });
    render();
  } catch (err) {
    setBotState("idle");
    body.innerHTML = `<div class="an-error">
        <strong>The analysis could not finish.</strong>
        <p class="muted">${esc(err.message || String(err))}</p>
        <button class="ghost" id="rerun">Try again</button>
      </div>`;
    wire();
  } finally {
    clearInterval(tick);
    running = false;
  }
}

/**
 * Open over the board.
 *
 * The board is left in place underneath rather than unmounted: closing has to return you to the
 * tab you were reading, at the scroll position you were at, and re-rendering a tab to do that
 * would cost a request and lose the position anyway.
 */
export function openAnalysis() {
  if (open) return;
  open = true;
  document.body.classList.add("analysing");
  const view = $("analysisView");
  view.hidden = false;
  // Two frames: one for `hidden` to clear, one so the class animates from its start state
  // rather than being applied in the same paint and skipping the transition entirely.
  requestAnimationFrame(() => requestAnimationFrame(() => view.classList.add("in")));
  view.focus({ preventScroll: true });
  if (!analysis) run();
  else render();
}

export function close() {
  if (!open) return;
  open = false;
  // The conversation goes home before the panel does, or it would be inside a hidden element
  // and the side panel would look empty.
  hideConversation();
  const view = $("analysisView");
  view.classList.remove("in");
  document.body.classList.remove("analysing");
  // Wait out the transition before hiding, or it vanishes instead of leaving.
  setTimeout(() => { if (!open) view.hidden = true; }, 220);
}

export const isOpen = () => open;
export const hasAnalysis = () => analysis !== null;

/**
 * Throw away the analysis because the question changed.
 *
 * The findings are computed for one period, one scope and one currency. Change any of them and
 * every figure on screen is answering a question nobody is asking any more — so the result is
 * discarded rather than left up with a stale caption, and the view closes if it was open. The
 * next press of the analyst re-runs it against what is now on screen.
 */
export function invalidate() {
  analysis = null;
  setBotCount(0);
  close();
}

export function wireAnalysis() {
  const start = $("runAnalysis");
  if (start) start.onclick = () => run(true);
  const shut = $("analysisClose");
  if (shut) shut.onclick = close;
  const back = $("anBack");
  if (back) back.onclick = showFindings;

  const form = $("anAskForm");
  if (form) {
    form.onsubmit = (e) => {
      e.preventDefault();
      sendQuestion($("anAsk")?.value);
    };
  }
  // The suggestions exist before the first analysis too — the general ones are useful on an
  // empty panel, and an ask bar with no examples reads as a search box nobody knows the syntax
  // for.
  renderSuggestions();

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !open) return;
    // Escape unwinds one step at a time: clear the box, then leave the conversation, then close
    // the panel. Jumping straight out from a half-typed question is the kind of small thing that
    // feels careless.
    const box = $("anAsk");
    if (document.activeElement === box && box.value) {
      box.value = "";
      return;
    }
    if (talking) {
      showFindings();
      return;
    }
    close();
  });
}

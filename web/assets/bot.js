/**
 * The analyst.
 *
 * A small robot that replaces the chat bubble, and tells you what it is doing while it does it.
 * Not decoration: an analysis that reads eight Azure sources takes twenty-five seconds, and a
 * button that does nothing visible for twenty-five seconds is a button people press twice.
 *
 * The states are the honest ones — each maps to something the app is genuinely doing, and none
 * of them is shown speculatively:
 *
 *   idle        nothing is happening
 *   thinking    a question has been sent to the model
 *   analyzing   the analysis endpoint is running
 *   insight     the analysis found something worth acting on
 *   explaining  the model is streaming an answer
 *   alert       something is over budget or spiking
 *   celebrating the estate came back clean
 *
 * Drawn as one SVG whose parts are moved by CSS rather than as seven images: the face has to
 * transition between states without a reload, and swapping bitmaps would flash. Every animation
 * is behind `prefers-reduced-motion`, which leaves a static face that still reads correctly —
 * the eyes and the badge carry the state, the movement only reinforces it.
 */

const STATES = {
  idle: { label: "Ask the analyst", hint: "Analyse this estate" },
  thinking: { label: "Thinking…", hint: "Working on your question" },
  analyzing: { label: "Analysing…", hint: "Reading every source" },
  insight: { label: "Found something", hint: "The analysis has findings" },
  explaining: { label: "Explaining…", hint: "Writing the answer" },
  alert: { label: "Needs attention", hint: "Something is over budget or spiking" },
  celebrating: { label: "All clear", hint: "Nothing worth flagging" },
};

/** The face. One markup block; CSS decides which parts are visible for the current state. */
const FACE = `
<svg class="bot-face" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <defs>
    <linearGradient id="bot-shell" x1="20%" y1="0%" x2="80%" y2="100%">
      <stop offset="0%" stop-color="#FFFFFF"/>
      <stop offset="100%" stop-color="#D2D0CE"/>
    </linearGradient>
    <linearGradient id="bot-visor" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#323130"/>
      <stop offset="100%" stop-color="#1B1A19"/>
    </linearGradient>
  </defs>

  <!-- The ring it hovers over. Sized so it reads at 26px in the button and at 44px in the
       panel header without a second drawing. -->
  <ellipse class="bot-ring" cx="32" cy="55" rx="19" ry="4"/>

  <!-- Antenna. The tip is the status light: it is the one part that changes colour, so the
       state is legible even at button size where the eyes are four pixels across. -->
  <path class="bot-stalk" d="M32 13V7" stroke="#94A3B8" stroke-width="2" stroke-linecap="round"/>
  <circle class="bot-tip" cx="32" cy="5" r="3"/>

  <!-- Body, then head, so the head overlaps. -->
  <path class="bot-body" d="M17 52a15 15 0 0 1 30 0z" fill="url(#bot-shell)"/>
  <circle class="bot-core" cx="32" cy="45" r="3.4"/>
  <ellipse class="bot-arm bot-arm-l" cx="14" cy="44" rx="4.4" ry="5.4" fill="url(#bot-shell)"/>
  <ellipse class="bot-arm bot-arm-r" cx="50" cy="44" rx="4.4" ry="5.4" fill="url(#bot-shell)"/>

  <rect class="bot-head" x="11" y="13" width="42" height="30" rx="14" fill="url(#bot-shell)"/>
  <rect class="bot-visor" x="15" y="17" width="34" height="22" rx="10" fill="url(#bot-visor)"/>

  <!-- Eyes. Two shapes for one pair: open for most states, curved for the happy ones, and CSS
       picks which is shown. Drawing both keeps the switch instant. -->
  <g class="bot-eyes-open">
    <ellipse cx="25" cy="28" rx="3.1" ry="3.6"/>
    <ellipse cx="39" cy="28" rx="3.1" ry="3.6"/>
  </g>
  <g class="bot-eyes-happy" fill="none" stroke-width="2.4" stroke-linecap="round">
    <path d="M22 29.5c1.6-2.6 4.4-2.6 6 0"/>
    <path d="M36 29.5c1.6-2.6 4.4-2.6 6 0"/>
  </g>

  <!-- The badge: a lightbulb for an insight, a warning for an alert. Only one is ever shown.
       Both take supporting-palette values rather than their own, so the analyst never
       introduces a colour the rest of the interface does not use. -->
  <g class="bot-badge bot-bulb">
    <circle cx="49" cy="11" r="6.5" fill="#FFB900"/>
    <path d="M49 14.5v2.5" stroke="#6B4C00" stroke-width="1.6" stroke-linecap="round"/>
    <path d="M46.6 7.5h4.8" stroke="#FFF4D6" stroke-width="1.4" stroke-linecap="round"/>
  </g>
  <g class="bot-badge bot-warn">
    <path d="M49 5.5l6 10.5H43z" fill="#D13438"/>
    <path d="M49 9.5v3.2M49 14.4v.1" stroke="#FFFFFF" stroke-width="1.6" stroke-linecap="round"/>
  </g>

  <!-- Thinking dots, inside the visor so they read as the bot's own thought rather than a
       separate loading indicator competing with it. -->
  <g class="bot-dots">
    <circle cx="26" cy="28" r="2"/><circle cx="32" cy="28" r="2"/><circle cx="38" cy="28" r="2"/>
  </g>

  <!-- The scan sweep for analysing: a line crossing the visor, clipped to it. -->
  <clipPath id="bot-visor-clip"><rect x="15" y="17" width="34" height="22" rx="10"/></clipPath>
  <g clip-path="url(#bot-visor-clip)">
    <rect class="bot-scan" x="13" y="17" width="6" height="22"/>
  </g>
</svg>`;

let host = null;
let state = "idle";
let resetTimer = 0;

/** Build the launcher in place of the old chat button. */
export function mountBot(container, onClick) {
  host = container;
  host.className = "bot";
  host.dataset.state = "idle";
  host.type = "button";
  host.innerHTML = `${FACE}<span class="bot-pip" aria-hidden="true"></span>`;
  host.setAttribute("aria-label", STATES.idle.label);
  host.title = STATES.idle.hint;
  host.onclick = onClick;
  return host;
}

/**
 * Move the bot to a state.
 *
 * `hold` returns it to idle afterwards, for the states that describe a moment rather than an
 * activity — "found something" is news, not a condition, and a bot that celebrates permanently
 * has stopped meaning anything.
 */
export function setBotState(next, { hold = 0 } = {}) {
  if (!host || !STATES[next]) return;
  clearTimeout(resetTimer);
  state = next;
  host.dataset.state = next;
  host.setAttribute("aria-label", STATES[next].label);
  host.title = STATES[next].hint;
  if (hold) resetTimer = setTimeout(() => setBotState("idle"), hold);
}

export const botState = () => state;

/** A count on the launcher — how many findings are waiting. Cleared by passing 0. */
export function setBotCount(n) {
  if (!host) return;
  const pip = host.querySelector(".bot-pip");
  if (!pip) return;
  pip.textContent = n > 9 ? "9+" : String(n || "");
  pip.hidden = !n;
}

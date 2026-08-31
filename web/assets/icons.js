/**
 * Rail icons.
 *
 * One line-art glyph per tab, drawn on a 20×20 grid at 1.5px stroke — Fluent's proportions, so
 * they sit correctly next to Segoe UI at 13px and stay legible at the 20px the collapsed rail
 * uses.
 *
 * Inline SVG rather than an icon font or sprite sheet, for three reasons that matter here:
 *   - `currentColor` means one glyph serves both themes and the selected state, instead of a
 *     light and a dark copy that drift apart.
 *   - No extra request, and no flash of unstyled glyph before a font loads.
 *   - The rail re-renders on every group toggle; markup is cheaper than a <use> lookup.
 *
 * Every glyph is stroke-only with no fill, so nothing needs a background to read against. The
 * shapes deliberately echo what the tab shows — a pie for Overview, a pulse for Anomalies, a
 * clock for History — rather than being decorative: an icon that has to be learned is worse than
 * no icon, because it still costs the space.
 */

const S = 'fill="none" stroke="currentColor" stroke-width="1.5" '
  + 'stroke-linecap="round" stroke-linejoin="round"';

/** Service families keep the shape of the thing they bill for. */
const PATHS = {
  // ---- Spend ------------------------------------------------------------
  // A pie with one slice lifted: the estate, broken down.
  overview: `<path ${S} d="M10 3.2a6.8 6.8 0 1 0 6.8 6.8"/>`
    + `<path ${S} d="M11.6 2.2A6.8 6.8 0 0 1 17.8 8.4H11.6z"/>`,
  // A clock face with a coin behind it: what an hour costs. The hands sit at a readable angle
  // rather than at 12, so the glyph still reads as a clock at 20px.
  uptime: `<circle ${S} cx="9.2" cy="10.4" r="6.4"/>`
    + `<path ${S} d="M9.2 6.6v3.8l2.6 1.6"/>`
    + `<path ${S} d="M13.4 3.4a6.4 6.4 0 0 1 3.4 5.6"/>`,
  // Stacked servers.
  compute: `<rect ${S} x="3.2" y="3.6" width="13.6" height="5.2" rx="1.6"/>`
    + `<rect ${S} x="3.2" y="11.2" width="13.6" height="5.2" rx="1.6"/>`
    + `<path ${S} d="M6.2 6.2h.01M6.2 13.8h.01"/>`,
  // A hub and its nodes.
  networking: `<circle ${S} cx="10" cy="4.6" r="1.9"/><circle ${S} cx="4.8" cy="15" r="1.9"/>`
    + `<circle ${S} cx="15.2" cy="15" r="1.9"/>`
    + `<path ${S} d="M8.6 6.2 6 13.2M11.4 6.2 14 13.2M6.7 15h6.6"/>`,
  // A database cylinder.
  storage: `<ellipse ${S} cx="10" cy="5.2" rx="5.6" ry="2.2"/>`
    + `<path ${S} d="M4.4 5.2v9.6c0 1.2 2.5 2.2 5.6 2.2s5.6-1 5.6-2.2V5.2"/>`
    + `<path ${S} d="M15.6 10c0 1.2-2.5 2.2-5.6 2.2S4.4 11.2 4.4 10"/>`,
  // Three linked nodes — a model, not a database. Deliberately different from `networking`,
  // which is also a node graph: that one is a hub with two children (a topology), this is a
  // layered stack with a wide base (a model). Drawn as the same idea they were indistinguishable
  // in the rail, which is the one thing an icon must never be.
  data: `<circle ${S} cx="10" cy="4.4" r="1.8"/>`
    + `<path ${S} d="M10 6.2v2.4M10 8.6H5.8a1.4 1.4 0 0 0-1.4 1.4v1M10 8.6h4.2a1.4 1.4 0 0 1 1.4 1.4v1"/>`
    + `<rect ${S} x="2.6" y="11" width="3.6" height="3.6" rx="1"/>`
    + `<rect ${S} x="8.2" y="11" width="3.6" height="3.6" rx="1"/>`
    + `<rect ${S} x="13.8" y="11" width="3.6" height="3.6" rx="1"/>`
    + `<path ${S} d="M10 8.6v2.4"/>`,
  // A link: things joined to other things.
  integration: `<path ${S} d="M8.3 11.7a3.2 3.2 0 0 1 0-4.5l2.2-2.2a3.2 3.2 0 0 1 4.5 4.5l-1 1"/>`
    + `<path ${S} d="M11.7 8.3a3.2 3.2 0 0 1 0 4.5l-2.2 2.2a3.2 3.2 0 0 1-4.5-4.5l1-1"/>`,
  // A shield with a tick.
  security: `<path ${S} d="M10 2.8 4.6 5v4.6c0 3.5 2.3 6.4 5.4 7.6 3.1-1.2 5.4-4.1 5.4-7.6V5z"/>`
    + `<path ${S} d="M7.8 9.9 9.4 11.5 12.4 8.3"/>`,
  // The catch-all: an ellipsis in a circle.
  other: `<circle ${S} cx="10" cy="10" r="7.2"/>`
    + `<path ${S} d="M7 10h.01M10 10h.01M13 10h.01"/>`,
  // A luggage tag.
  tags: `<path ${S} d="M10.6 2.9H16a1.2 1.2 0 0 1 1.2 1.2v5.4a1.6 1.6 0 0 1-.47 1.13l-5.9 5.9a1.6 1.6 0 0 1-2.26 0l-4.6-4.6a1.6 1.6 0 0 1 0-2.26l5.9-5.9a1.6 1.6 0 0 1 .73-.42z"/>`
    + `<path ${S} d="M13.7 6.3h.01"/>`,

  // ---- Optimize ---------------------------------------------------------
  // A downward arrow through a coin: spend, brought down. One glyph where there were six,
  // matching the one entry that replaced them.
  savings: `<circle ${S} cx="10" cy="10" r="7.1"/>`
    + `<path ${S} d="M10 6v8M7.2 11.2 10 14l2.8-2.8"/>`,
  // A hexagon: a commitment held.
  commitments: `<path ${S} d="M10 2.6 16.2 6v8L10 17.4 3.8 14V6z"/>`
    + `<circle ${S} cx="10" cy="10" r="2.4"/>`,
  // Arrows pushing outward — resize.
  rightsizing: `<path ${S} d="M4 8V4h4M16 12v4h-4M4 4l4.6 4.6M16 16l-4.6-4.6"/>`
    + `<path ${S} d="M16 8V4h-4M4 12v4h4M16 4l-4.6 4.6M4 16l4.6-4.6"/>`,
  // A power button.
  shutdown: `<path ${S} d="M10 3v6"/>`
    + `<path ${S} d="M14.6 6a6.4 6.4 0 1 1-9.2 0"/>`,
  // A box, unattached.
  waste: `<path ${S} d="M10 2.9 16.6 6.4v7.2L10 17.1 3.4 13.6V6.4z"/>`
    + `<path ${S} d="M3.4 6.4 10 10l6.6-3.6M10 10v7.1"/>`,
  // A rising line with an arrow: a better rate.
  rates: `<path ${S} d="M3.4 13.6 8 9l3 3 5.6-5.6"/>`
    + `<path ${S} d="M12.6 6.4h4v4"/>`,
  // A lightbulb: a recommendation.
  advisor: `<path ${S} d="M7.6 13.4a4.8 4.8 0 1 1 4.8 0v1.4a1 1 0 0 1-1 1h-2.8a1 1 0 0 1-1-1z"/>`
    + `<path ${S} d="M8.6 17.4h2.8"/>`,

  // ---- Monitor ----------------------------------------------------------
  // A heartbeat trace.
  anomalies: `<path ${S} d="M2.8 10h3l1.8-5 3 10 2-7 1.6 2h3"/>`,
  // A clock with an arrow curling back into it: looking at an earlier read of the same data.
  // Distinct from `health`, which is also a clock — that one is time running *out*.
  history: `<path ${S} d="M3.2 10a6.8 6.8 0 1 0 2-4.8L3.2 7.2"/>`
    + `<path ${S} d="M3 3.4v3.9h3.9"/>`
    + `<path ${S} d="M10 6.4V10l2.6 1.6"/>`,
  // A wallet.
  budgets: `<path ${S} d="M4 6.2A1.6 1.6 0 0 1 5.6 4.6h9.6a1.2 1.2 0 0 1 1.2 1.2v1.6"/>`
    + `<rect ${S} x="4" y="6.2" width="12.4" height="9.2" rx="1.6"/>`
    + `<path ${S} d="M13.2 10.8h.01"/>`,

  // ---- Govern -----------------------------------------------------------
  // A checklist on a clipboard: rules, and whether they are met. Not a shield — Security & Ops
  // already owns the shield, and the two sat four rows apart reading as the same thing.
  governance: `<rect ${S} x="4.2" y="3.6" width="11.6" height="13" rx="1.6"/>`
    + `<path ${S} d="M7.6 3.6V2.9a1 1 0 0 1 1-1h2.8a1 1 0 0 1 1 1v.7"/>`
    + `<path ${S} d="m7.2 9 1.2 1.2 2.2-2.4M12.6 12.8H7.2"/>`,
  // A calendar with a cross: a date something stops.
  esu: `<rect ${S} x="3.4" y="4.6" width="13.2" height="12" rx="1.6"/>`
    + `<path ${S} d="M3.4 8.4h13.2M7 2.9v3.2M13 2.9v3.2"/>`
    + `<path ${S} d="m8.4 11.4 3.2 3.2M11.6 11.4l-3.2 3.2"/>`,
  // A bell with a line through the clapper: a notice with a deadline on it. Not a clock — the
  // History tab already owns clocks, and two clocks in one rail is one clock too many.
  health: `<path ${S} d="M6 8.4a4 4 0 0 1 8 0c0 3.2.9 4.6 1.7 5.4H4.3C5.1 13 6 11.6 6 8.4z"/>`
    + `<path ${S} d="M8.6 16.2a1.8 1.8 0 0 0 2.8 0"/>`
    + `<path ${S} d="M10 2.8v1.6"/>`,

  // ---- Reports ----------------------------------------------------------
  // A page with an arrow leaving it.
  report: `<path ${S} d="M11.4 2.9H6.2a1.6 1.6 0 0 0-1.6 1.6v11a1.6 1.6 0 0 0 1.6 1.6h7.6a1.6 1.6 0 0 0 1.6-1.6V6.7z"/>`
    + `<path ${S} d="M11.4 2.9v3.8h3.9"/>`
    + `<path ${S} d="M7.4 12.6h4.4M10.2 10.8l1.8 1.8-1.8 1.8"/>`,
  // A cog.
  settings: `<circle ${S} cx="10" cy="10" r="2.6"/>`
    + `<path ${S} d="M15.9 12.2a1.3 1.3 0 0 0 .26 1.43l.05.05a1.6 1.6 0 1 1-2.26 2.26l-.05-.05a1.3 1.3 0 0 0-1.43-.26 1.3 1.3 0 0 0-.79 1.19v.13a1.6 1.6 0 0 1-3.2 0v-.07a1.3 1.3 0 0 0-.85-1.19 1.3 1.3 0 0 0-1.43.26l-.5.05a1.6 1.6 0 1 1-2.26-2.26l.05-.05a1.3 1.3 0 0 0 .26-1.43 1.3 1.3 0 0 0-1.19-.79h-.13a1.6 1.6 0 0 1 0-3.2h.07a1.3 1.3 0 0 0 1.19-.85 1.3 1.3 0 0 0-.26-1.43l-.05-.05a1.6 1.6 0 1 1 2.26-2.26l.5.05a1.3 1.3 0 0 0 1.43.26h.06a1.3 1.3 0 0 0 .79-1.19v-.13a1.6 1.6 0 0 1 3.2 0v.07a1.3 1.3 0 0 0 .79 1.19 1.3 1.3 0 0 0 1.43-.26l.05-.05a1.6 1.6 0 1 1 2.26 2.26l-.5.05a1.3 1.3 0 0 0-.26 1.43v.06a1.3 1.3 0 0 0 1.19.79h.13a1.6 1.6 0 0 1 0 3.2h-.07a1.3 1.3 0 0 0-1.19.79z"/>`,
};

/**
 * The glyph for a tab, as markup. Falls back to a neutral dot rather than nothing: a rail where
 * some rows have an icon and others have a ragged gap is worse than one with a placeholder, and
 * a missing icon should be visible in review rather than silently absent.
 */
export function railIcon(id) {
  const d = PATHS[id] || `<circle ${S} cx="10" cy="10" r="3"/>`;
  return `<svg class="rail-icon" viewBox="0 0 20 20" aria-hidden="true" focusable="false">${d}</svg>`;
}

export const hasRailIcon = (id) => Object.hasOwn(PATHS, id);

/**
 * Spend hierarchy — a squarified treemap.
 *
 * A bar chart already answers "who is biggest". A treemap answers a different question: what
 * *share* of the whole each one holds, and how much of the estate the top few account for. Area
 * is the only encoding people read as proportion without doing arithmetic, and the tiles fill the
 * panel, so "these three are most of it" is visible before a single number is read.
 *
 * Squarified rather than a naive slice-and-dice. Slice-and-dice produces long thin slivers whose
 * areas are genuinely impossible to compare by eye and which cannot hold a label; squarified
 * keeps aspect ratios near 1, so tiles stay comparable and there is room for the name and the
 * figure inside them.
 *
 * Colour comes from CSS custom properties rather than being computed here, so switching theme
 * re-colours the tiles with no JavaScript and no redraw.
 */
import { esc, money } from "./app.js";

const SWATCHES = 10;

/**
 * Squarified treemap layout (Bruls, Huizing & van Wijk).
 *
 * Lays rows along the shorter edge of the remaining rectangle, extending a row while doing so
 * improves its worst aspect ratio and closing it the moment it stops.
 */
function squarify(items, x, y, w, h) {
  const out = [];
  const total = items.reduce((n, it) => n + it.value, 0);
  if (total <= 0 || w <= 0 || h <= 0) return out;

  // Work in area units so the scale factor never has to be reapplied.
  let scale = (w * h) / total;
  let queue = items.map((it) => ({ ...it, area: it.value * scale }));

  let rx = x;
  let ry = y;
  let rw = w;
  let rh = h;

  const worst = (row, side) => {
    const sum = row.reduce((n, it) => n + it.area, 0);
    if (sum <= 0) return Infinity;
    const max = row.reduce((m, it) => Math.max(m, it.area), 0);
    const min = row.reduce((m, it) => Math.min(m, it.area), Infinity);
    const s2 = sum * sum;
    const side2 = side * side;
    return Math.max((side2 * max) / s2, s2 / (side2 * min));
  };

  while (queue.length) {
    const side = Math.min(rw, rh);
    const row = [queue[0]];
    let rest = queue.slice(1);

    while (rest.length && worst([...row, rest[0]], side) <= worst(row, side)) {
      row.push(rest[0]);
      rest = rest.slice(1);
    }

    const sum = row.reduce((n, it) => n + it.area, 0);
    const thick = sum / side;

    if (rw >= rh) {
      // Row runs down the left edge; the remainder is what is left to its right.
      let cy = ry;
      for (const it of row) {
        const ih = it.area / thick;
        out.push({ ...it, x: rx, y: cy, w: thick, h: ih });
        cy += ih;
      }
      rx += thick;
      rw -= thick;
    } else {
      let cx = rx;
      for (const it of row) {
        const iw = it.area / thick;
        out.push({ ...it, x: cx, y: ry, w: iw, h: thick });
        cx += iw;
      }
      ry += thick;
      rh -= thick;
    }

    queue = rest;
    // Floating point can leave a sliver of negative width on the last pass.
    if (rw < 0.01 || rh < 0.01) break;
  }

  return out;
}

/**
 * Render a treemap into `container`.
 *
 * `rows` is `{ name, cost }` sorted or not; `opts.kind` names what a tile *is* ("Subscription"),
 * which is the difference between a chart that shows five rectangles and one that says what they
 * are. `opts.limit` caps the tile count and rolls the tail into one "others" tile rather than
 * producing slivers too small to label.
 */
export function drawTreemap(container, rows, currency, opts = {}) {
  const kind = opts.kind || "";
  const limit = opts.limit || 12;
  const title = opts.title || "Spend hierarchy";

  const clean = (rows || []).filter((r) => r && r.cost > 0).sort((a, b) => b.cost - a.cost);

  if (!clean.length) {
    container.innerHTML = `<section class="card treemap">
      <div class="tm-head"><h3>${esc(title)}</h3></div>
      <p class="muted empty">No spend to break down for this period.</p>
    </section>`;
    return;
  }

  const items = clean.slice(0, limit).map((r) => ({ name: r.name, value: r.cost, cost: r.cost }));
  const tail = clean.slice(limit);
  if (tail.length) {
    const sum = tail.reduce((n, r) => n + r.cost, 0);
    if (sum > 0) {
      items.push({ name: `${tail.length} smaller`, value: sum, cost: sum, rolled: tail.length });
    }
  }

  const total = clean.reduce((n, r) => n + r.cost, 0);
  const shown = Math.min(clean.length, limit);
  const topShare =
    total > 0 ? (clean.slice(0, 3).reduce((n, r) => n + r.cost, 0) / total) * 100 : 0;

  container.innerHTML = `
    <section class="card treemap">
      <div class="tm-head">
        <h3>${esc(title)}</h3>
        <span class="tm-count">${kind ? esc(kind) + "s" : ""}${
          tail.length ? ` · top ${shown} of ${clean.length}` : ` · ${clean.length}`
        }</span>
      </div>
      <div class="tm-plot"></div>
      <p class="chart-note">Tile area is share of spend over the period.${
        // "The largest three are 100%" is true and useless when there are only three of them.
        clean.length > 3 ? ` The largest three are ${topShare.toFixed(0)}% of the total.` : ""
      }${tail.length ? ` The smallest ${tail.length} are grouped.` : ""}</p>
    </section>`;

  const plot = container.querySelector(".tm-plot");

  /**
   * Lay out against the box we actually have.
   *
   * Squarifying against a fixed notional rectangle and then stretching the result with
   * percentages throws away the whole point of the algorithm: the aspect ratios it worked to
   * keep near 1 get scaled by whatever the real panel's ratio happens to be. Measuring first
   * costs one layout read and keeps the tiles square-ish at any size.
   */
  const layout = () => {
    const w = plot.clientWidth;
    const h = plot.clientHeight;
    // Hidden tab, or first paint before the grid has resolved. The observer will call back.
    if (w < 8 || h < 8) return;

    const laid = squarify(items, 0, 0, w, h);

    plot.innerHTML = laid
      .map((t, i) => {
        const share = total > 0 ? (t.cost / total) * 100 : 0;
        // Below roughly this size a label overflows its tile. Dropping it and letting the
        // tooltip carry the name is better than text spilling across a neighbour.
        const room = t.w > 108 && t.h > 62;
        const tight = t.w > 72 && t.h > 34;
        const cls = `tm-tile tm-c${(i % SWATCHES) + 1}${t.rolled ? " tm-rolled" : ""}`;
        const label = room
          ? `<span class="tm-name">${esc(t.name)}</span>
             <span class="tm-val">${esc(money(t.cost, currency, 0))}</span>
             <span class="tm-sub">${kind ? esc(kind) + " · " : ""}${share.toFixed(1)}%</span>`
          : tight
          ? `<span class="tm-name">${esc(t.name)}</span>
             <span class="tm-val">${esc(money(t.cost, currency, 0))}</span>`
          : "";
        return `<div class="${cls}" tabindex="0"
          style="left:${t.x.toFixed(1)}px;top:${t.y.toFixed(1)}px;
                 width:${t.w.toFixed(1)}px;height:${t.h.toFixed(1)}px"
          title="${esc(t.name)} — ${esc(money(t.cost, currency, 2))} · ${share.toFixed(1)}% of total">
          <span class="tm-inner">${label}</span>
        </div>`;
      })
      .join("");
  };

  layout();

  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      // The dashboard replaces whole subtrees on re-render. Without this the observer would
      // keep a detached element alive and re-lay-out something nobody can see.
      if (!plot.isConnected) {
        ro.disconnect();
        return;
      }
      layout();
    });
    ro.observe(plot);
  }
}

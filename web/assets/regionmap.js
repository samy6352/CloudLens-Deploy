/**
 * Where the money is, geographically.
 *
 * Deliberately *not* a mapping library. Leaflet, Mapbox and the Azure Maps SDK all want either a
 * large bundle or tiles from a CDN, and this app vendors everything — a tile layer would be the
 * heaviest and least reliable thing on the page, to show at most a couple of dozen points.
 *
 * The first version drew an honest graticule and no coastline, on the reasoning that a
 * hand-traced map would be recognisable but wrong, and people read landmasses as fact. That
 * reasoning still holds; the answer was not to trace one. `worldmap.js` carries real Natural
 * Earth geometry (public domain), projected and simplified at build time, so the coastline is
 * accurate rather than suggestive. Measured cost: about 10 KB over the wire after gzip.
 */
import { esc, money } from "./app.js";
import { WORLD, MAP_W, MAP_H, LAT_TOP, LAT_BOTTOM } from "./worldmap.js";

/**
 * Azure regions to latitude and longitude.
 *
 * Azure publishes these, but only through an ARM call that would be a round trip on every page
 * load for data that changes about once a year. Hardcoded here, and any region missing from the
 * table is counted and reported rather than landing at (0,0) in the Atlantic.
 */
const REGIONS = {
  eastus: [37.37, -79.82], eastus2: [36.67, -78.39], eastus3: [32.78, -96.80],
  centralus: [41.59, -93.62], northcentralus: [41.88, -87.62],
  southcentralus: [29.42, -98.49], westcentralus: [40.89, -110.23],
  westus: [37.78, -122.42], westus2: [47.23, -119.85], westus3: [33.45, -112.07],
  canadacentral: [43.65, -79.38], canadaeast: [46.82, -71.21],
  brazilsouth: [-23.55, -46.63], brazilsoutheast: [-22.91, -43.17],
  mexicocentral: [20.59, -100.39],
  northeurope: [53.35, -6.26], westeurope: [52.37, 4.90],
  uksouth: [50.94, -0.80], ukwest: [53.43, -3.08],
  francecentral: [46.37, 2.37], francesouth: [43.82, 2.19],
  germanywestcentral: [50.11, 8.68], germanynorth: [53.07, 8.81],
  switzerlandnorth: [47.45, 8.56], switzerlandwest: [46.20, 6.14],
  norwayeast: [59.91, 10.75], norwaywest: [58.97, 5.73],
  swedencentral: [60.67, 17.14], polandcentral: [52.23, 21.01],
  italynorth: [45.47, 9.18], spaincentral: [40.42, -3.70],
  uaenorth: [25.27, 55.30], uaecentral: [24.47, 54.37],
  qatarcentral: [25.29, 51.53], israelcentral: [31.77, 35.21],
  southafricanorth: [-25.73, 28.22], southafricawest: [-33.91, 18.42],
  centralindia: [18.52, 73.86], southindia: [12.97, 80.16], westindia: [19.09, 72.87],
  jioindiawest: [22.47, 70.06], jioindiacentral: [21.15, 79.09],
  eastasia: [22.27, 114.19], southeastasia: [1.28, 103.83],
  japaneast: [35.68, 139.77], japanwest: [34.69, 135.50],
  koreacentral: [37.57, 126.98], koreasouth: [35.18, 129.08],
  australiaeast: [-33.86, 151.21], australiasoutheast: [-37.81, 144.96],
  australiacentral: [-35.31, 149.12], australiacentral2: [-35.31, 149.12],
  newzealandnorth: [-36.85, 174.76],
  chinanorth: [39.90, 116.41], chinaeast: [31.23, 121.47],
  indonesiacentral: [-6.21, 106.85], malaysiawest: [3.14, 101.69],
};

/**
 * Rows whose "region" is not a place.
 *
 * Cost Management uses these for spend billed to the account rather than to a datacentre —
 * Entra, Defender plans, marketplace fees, bandwidth. That is real money and belongs in the
 * totals, but putting it anywhere on a map would be an invention, so it is counted separately
 * and explained in the caption.
 */
const NON_PLACES = new Set([
  "", "—", "-", "global", "unassigned", "unknown", "n/a", "all regions",
  "intercontinental", "zone 1", "zone 2", "zone 3",
]);

const key = (n) => String(n || "").toLowerCase().replace(/\s+/g, "");
const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

function project(lat, lon) {
  return [
    ((lon + 180) / 360) * MAP_W,
    ((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM)) * MAP_H,
  ];
}

/**
 * Nudge overlapping bubbles apart.
 *
 * Azure clusters its regions tightly. Central India and West India are about 500 km apart, which
 * at this scale is a few pixels, so two circles drawn there sit almost concentrically and read as
 * one. A short relaxation pass separates them enough to be countable. Displacement is a few
 * pixels at most — well below the precision anyone should read off a world map — and the legend
 * underneath carries the exact figures either way.
 */
function separate(points) {
  for (let pass = 0; pass < 60; pass++) {
    let moved = false;
    for (let i = 0; i < points.length; i++) {
      for (let j = i + 1; j < points.length; j++) {
        const a = points[i];
        const b = points[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.hypot(dx, dy) || 0.01;
        const want = a.r + b.r + 4;
        if (dist >= want) continue;
        const push = (want - dist) / 2;
        const ux = (dx / dist) * push;
        const uy = (dy / dist) * push;
        // The larger circle yields less: it is the one the eye uses to locate the cluster.
        const wa = b.r / (a.r + b.r);
        const wb = a.r / (a.r + b.r);
        a.x -= ux * 2 * wa;
        a.y -= uy * 2 * wa;
        b.x += ux * 2 * wb;
        b.y += uy * 2 * wb;
        moved = true;
      }
    }
    if (!moved) break;
  }
  return points;
}

/**
 * Render the map into `container`.
 *
 * `rows` is whatever the executive view already has: `{ name, cost, resources, share_pct }`.
 * Nothing is fetched here — the data is already on the page.
 */
export function drawRegionMap(container, rows, currency) {
  const all = (rows || []).filter((r) => r.cost > 0);

  const plotted = all.map((r) => ({ ...r, at: REGIONS[key(r.name)] })).filter((r) => r.at);
  const offmap = all.filter((r) => !REGIONS[key(r.name)]);
  const isNonPlace = (r) => NON_PLACES.has(String(r.name || "").toLowerCase().trim());
  const nonPlace = offmap.filter(isNonPlace);
  const unmapped = offmap.filter((r) => !isNonPlace(r));

  if (!plotted.length) {
    container.remove();
    return;
  }

  const max = plotted.reduce((m, r) => Math.max(m, r.cost), 0);

  // Area, not radius, scales with cost: a circle drawn with radius proportional to value
  // overstates the big ones by the square of the difference.
  const radius = (cost) => clamp(Math.sqrt(cost / max) * 46, 9, 46);

  const points = separate(
    [...plotted]
      .sort((a, b) => b.cost - a.cost)
      .map((r) => {
        const [x, y] = project(r.at[0], r.at[1]);
        return { row: r, x, y, r: radius(r.cost) };
      })
  );

  const circles = points
    .map(
      (p) => `<g class="rg-pt" tabindex="0" role="listitem"
        aria-label="${esc(p.row.name)}, ${esc(money(p.row.cost, currency, 2))}, ${p.row.resources || 0} resources">
        <title>${esc(p.row.name)} — ${esc(money(p.row.cost, currency, 2))} · ${p.row.resources || 0} resources</title>
        <circle class="rg-halo" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(p.r + 5).toFixed(1)}" />
        <circle class="rg-dot" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${p.r.toFixed(1)}" />
      </g>`
    )
    .join("");

  // The legend carries the names, so the map itself stays unlabelled. Labelling bubbles at this
  // size means either overlapping text or leader lines, and both cost more clarity than they buy
  // when the same names sit directly underneath in reading order.
  const legend = [...plotted]
    .sort((a, b) => b.cost - a.cost)
    .slice(0, 8)
    .map(
      (r) => `<li class="rg-leg-item">
        <span class="rg-leg-name" title="${esc(r.name)}">${esc(r.name)}</span>
        <span class="rg-leg-bar"><i style="width:${((r.cost / max) * 100).toFixed(1)}%"></i></span>
        <span class="rg-leg-val">${esc(money(r.cost, currency, 0))}</span>
      </li>`
    )
    .join("");

  const counts = [`${plotted.length} mapped`];
  if (nonPlace.length) counts.push(`${nonPlace.length} global/unassigned`);
  if (unmapped.length) counts.push(`${unmapped.length} unrecognised`);

  container.innerHTML = `
    <section class="card region-map">
      <div class="rg-head">
        <h3>Spend by Azure region</h3>
        <span class="rg-count">${esc(counts.join(" · "))}</span>
      </div>
      <div class="rg-wrap">
        <svg viewBox="0 0 ${MAP_W} ${MAP_H}" role="list"
             aria-label="Spend by Azure region, plotted geographically"
             preserveAspectRatio="xMidYMid meet">
          <path class="rg-land" d="${WORLD}" />
          ${circles}
        </svg>
      </div>
      <ul class="rg-legend">${legend}</ul>
      <p class="chart-note">Circle area is spend over the period. Nearby regions are nudged apart
        so they stay countable.${
          nonPlace.length
            ? ` ${nonPlace.length} row(s) bill to the account rather than to a datacentre, so they
                are in the totals but not on the map.`
            : ""
        }${
          unmapped.length
            ? ` ${unmapped.length} region(s) have no coordinates in this build and are not plotted.`
            : ""
        }
        <span class="rg-attrib">Geography: Natural Earth (public domain).</span></p>
    </section>`;
}

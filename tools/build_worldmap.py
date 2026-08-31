"""
Turn public-domain Natural Earth geometry into a flat SVG path the app can ship.

Run offline, output vendored. The app must not fetch map tiles at runtime: it loads in about
50 KB total and vendors everything, so a CDN round trip for geography would be both the slowest
and the least reliable thing on the page.

Source: world-atlas countries-110m, derived from Natural Earth, which places its data in the
public domain ("no rights reserved"). No attribution is legally required; we carry one anyway
because saying where a map came from is just honest.

Two encoding choices earn their keep here, both measured rather than assumed:

  * Relative linetos. Absolute coordinates in a 2000-wide space cost four or five characters
    each; the delta between neighbouring coastline points is usually one or two. Same picture,
    roughly half the bytes.

  * Integer coordinates in a 2000x760 space that renders at about 1000 wide, so the rounding
    error is half a pixel -- below what the eye resolves on a coastline, and it removes a
    decimal point plus a digit from every number.

Run with --probe to print the size/tolerance curve instead of writing the file.
"""
import gzip, json, math, os, sys, tempfile, urllib.request

# Paths resolve from this file, not the shell's working directory, so the script works from
# anywhere in the tree.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json"
SRC = os.path.join(tempfile.gettempdir(), "countries-110m.json")

# Drawn at 2x the display size so integer rounding lands at half a pixel.
W, H = 2000.0, 760.0
LAT_TOP, LAT_BOTTOM = 80.0, -56.0


def decode_arcs(topo):
    """Undo TopoJSON's delta encoding and quantisation into absolute lon/lat."""
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    out = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * sx + tx, y * sy + ty))
        out.append(pts)
    return out


def ring_points(arcs, idxs):
    """Stitch arc indices into one ring. A negative index means traverse that arc backwards."""
    pts = []
    for i in idxs:
        a = arcs[~i][::-1] if i < 0 else arcs[i]
        pts.extend(a[1:] if pts else a)
    return pts


def project(lon, lat):
    return (((lon + 180.0) / 360.0) * W, ((LAT_TOP - lat) / (LAT_TOP - LAT_BOTTOM)) * H)


def simplify(pts, tol):
    """Douglas-Peucker in projected pixel space, so the tolerance means what it looks like."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        lo, hi = stack.pop()
        ax, ay = pts[lo]
        bx, by = pts[hi]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, wi = -1.0, -1
        for i in range(lo + 1, hi):
            px, py = pts[i]
            d = (
                math.hypot(px - ax, py - ay)
                if norm == 0
                else abs(dy * px - dx * py + bx * ay - by * ax) / norm
            )
            if d > worst:
                worst, wi = d, i
        if worst > tol and wi > 0:
            keep[wi] = True
            stack.append((lo, wi))
            stack.append((wi, hi))
    return [p for p, k in zip(pts, keep) if k]


def ring_area(pts):
    a = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def encode(pts):
    """Relative-lineto path data, integer coords, drift-free.

    Deltas are taken between *rounded* positions, so error cannot accumulate along a ring the
    way it would if each delta were rounded independently.
    """
    x0, y0 = round(pts[0][0]), round(pts[0][1])
    px, py = x0, y0
    body = []
    for x, y in pts[1:]:
        rx, ry = round(x), round(y)
        dx, dy = rx - px, ry - py
        if dx == 0 and dy == 0:
            continue
        # A leading minus already separates the pair, so the space before it is dead weight.
        body.append(f"{dx}{'' if dy < 0 else ' '}{dy}")
        px, py = rx, ry
    if not body:
        return ""
    # "l" once, then the pairs: SVG repeats the last command implicitly.
    return f"M{x0} {y0}l" + " ".join(body) + "Z"


def split_antimeridian(lonlat):
    """Break a ring wherever it jumps across ±180 longitude.

    Russia's Chukotka and Kiribati straddle the antimeridian, so consecutive points step from
    +179 to -179. Projected, that is a segment spanning the entire width of the map, and it
    renders as a hard horizontal line straight across the world -- which is exactly what the
    first build of this did.

    Proper antimeridian clipping would interpolate the crossing and close each half against the
    map edge. At 110m resolution the pieces involved are a Siberian headland and a few Pacific
    atolls, so splitting the ring into its contiguous runs and letting each close itself is
    within a pixel of that and far less code.
    """
    runs = [[]]
    prev = None
    for lon, lat in lonlat:
        if prev is not None and abs(lon - prev) > 180:
            runs.append([])
        runs[-1].append((lon, lat))
        prev = lon
    return [r for r in runs if len(r) >= 3]


def build(topo, arcs, obj_name, tol, min_area):
    parts = []
    for g in topo["objects"][obj_name]["geometries"]:
        t = g.get("type")
        polys = [g["arcs"]] if t == "Polygon" else g["arcs"] if t == "MultiPolygon" else []
        for poly in polys:
            for ring in poly:
                for run in split_antimeridian(ring_points(arcs, ring)):
                    # Antarctica is a third of the ink for none of the regions. Dropped outright
                    # rather than clipped, which would leave a hard bar across the bottom.
                    if all(p[1] < -55 for p in run):
                        continue
                    # Clamped to the crop itself, so no geometry lands outside the viewBox.
                    # Letting it overhang and relying on the SVG to clip looks identical but
                    # ships coordinates nobody can see and makes "is it in the box" untestable.
                    pts = [
                        project(lon, max(LAT_BOTTOM, min(LAT_TOP, lat))) for lon, lat in run
                    ]
                    pts = simplify(pts, tol)
                    if len(pts) < 3 or ring_area(pts) < min_area:
                        continue
                    d = encode(pts)
                    if d:
                        parts.append(d)
    return "".join(parts)


def main():
    if not os.path.exists(SRC):
        print(f"fetching {SOURCE_URL}")
        urllib.request.urlretrieve(SOURCE_URL, SRC)
    topo = json.load(open(SRC, encoding="utf-8"))
    arcs = decode_arcs(topo)

    if "--probe" in sys.argv:
        # Measure before choosing. Gzip is the number that matters: it is what crosses the wire.
        print(f"{'object':10} {'tol':>5} {'minArea':>8} {'raw':>9} {'gzip':>8}")
        for obj in ("land", "countries"):
            for tol, area in ((0.8, 4), (1.2, 8), (1.8, 14), (2.5, 24), (3.5, 40)):
                d = build(topo, arcs, obj, tol, area)
                gz = len(gzip.compress(d.encode(), 9))
                print(f"{obj:10} {tol:>5} {area:>8} {len(d):>9,} {gz:>8,}")
        return

    # One path, not two. The country rings already contain every coastline, so their union is
    # the landmass -- a separate "land" outline would be duplicate geometry, and measured at
    # 11 KB of it. Keeping them in a single <path> also means one fill operation, so a shared
    # border between neighbours cannot antialias into a faint light seam the way separately
    # filled shapes do.
    world = build(topo, arcs, "countries", tol=WORLD_TOL, min_area=WORLD_AREA)
    out = f"""/**
 * World geometry, projected and simplified once at build time.
 *
 * Generated by tools_build_worldmap.py from world-atlas countries-110m, which derives from
 * Natural Earth (public domain). Do not hand-edit -- regenerate.
 *
 * Equirectangular, cropped to {LAT_BOTTOM:g}..{LAT_TOP:g} latitude for a {W:g}x{H:g} viewBox that renders at
 * about half that. A full -90..90 map spends a third of its height on Antarctica and empty
 * ocean, and Azure builds in neither.
 *
 * Coordinates are integers and linetos are relative: the delta between neighbouring coastline
 * points is one or two characters where an absolute coordinate is four or five.
 */
export const MAP_W = {W:g};
export const MAP_H = {H:g};
export const LAT_TOP = {LAT_TOP:g};
export const LAT_BOTTOM = {LAT_BOTTOM:g};
export const WORLD = "{world}";
"""

    dst = os.path.join(REPO, "web", "assets", "worldmap.js")
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)

    raw = os.path.getsize(dst)
    gz = len(gzip.compress(out.encode(), 9))
    print(f"world  : {len(world):>8,} chars")
    print(f"file   : {raw:>8,} bytes raw   {gz:,} bytes gzipped")


WORLD_TOL, WORLD_AREA = 1.6, 10

main()

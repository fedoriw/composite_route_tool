#!/usr/bin/env python3
"""
Composite Route Map Generator
==============================

Generates a printable PDF showing multiple bike routes overlaid on a single
map, with:
  - Each route drawn in its own color
  - Routes that share roads drawn as parallel offset lines (so shared roads
    are visible as side-by-side colored lines rather than a single hidden
    line)
  - Direction-of-travel arrows along each route
  - Numbered waypoint markers where routes significantly diverge/converge

INPUT
-----
Provide one GPX file per route. Each GPX must contain a single <trk> with
track points (the format RideWithGPS exports). To get a GPX from
RideWithGPS:　open the route -> Export -> GPX (track points only is fine;
"Include POI as waypoints" / "Include cues as waypoints" are not needed by
this script and are ignored).

USAGE
-----
    python3 composite_map.py route1.gpx route2.gpx route3.gpx \\
        --labels "36K" "60K" "100K" \\
        --title "Firecracker Ride - Composite Route Map" \\
        --output composite_map.pdf

If --labels is omitted, each route's <name> from the GPX is used.

CONFIGURATION
-------------
Key tunables are exposed as command-line flags (see --help) and as constants
near the top of main(); see comments for guidance on adjusting them for a
different event / area.
"""

import argparse
import xml.etree.ElementTree as ET
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from scipy.spatial import cKDTree

try:
    import contextily as cx
    HAVE_CONTEXTILY = True
except ImportError:
    HAVE_CONTEXTILY = False

GPX_NS = {'gpx': 'http://www.topografix.com/GPX/1/1'}

METERS_PER_MILE = 1609.34
METERS_PER_KM = 1000.0

# A reasonably distinct, print-friendly color cycle. If you have more than
# this many routes, colors will repeat - consider trimming the route list.
DEFAULT_COLORS = [
    '#1f77b4',  # blue
    '#d62728',  # red
    '#2ca02c',  # green
    '#9467bd',  # purple
    '#ff7f0e',  # orange
    '#17becf',  # cyan
    '#8c564b',  # brown
]


# ---------------------------------------------------------------------------
# GPX parsing
# ---------------------------------------------------------------------------

def parse_gpx_track(path):
    """Return (name, points) where points is an Nx2 array of (lon, lat).

    Only <trk>/<trkseg>/<trkpt> elements are read. Waypoints (<wpt>), routes
    (<rte>), POIs, and cues are ignored - they aren't needed for the
    composite map and it's fine if the GPX includes them.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    trk = root.find('gpx:trk', GPX_NS)
    if trk is None:
        raise ValueError(f"{path}: no <trk> element found - is this a track GPX?")

    name_el = trk.find('gpx:name', GPX_NS)
    name = name_el.text if name_el is not None and name_el.text else path

    pts = []
    for seg in trk.findall('gpx:trkseg', GPX_NS):
        for pt in seg.findall('gpx:trkpt', GPX_NS):
            lat = float(pt.attrib['lat'])
            lon = float(pt.attrib['lon'])
            pts.append((lon, lat))

    if len(pts) < 2:
        raise ValueError(f"{path}: track has fewer than 2 points")

    return name, np.array(pts)


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

class LocalProjection:
    """Simple equirectangular projection centered on a reference latitude.

    Good enough for routes spanning a few tens of km - not suitable for
    very large areas or near the poles.
    """

    def __init__(self, ref_lat):
        self.m_per_deg_lat = 111000.0
        self.m_per_deg_lon = 111000.0 * np.cos(np.radians(ref_lat))

    def to_m(self, pts_lonlat):
        out = np.empty_like(pts_lonlat, dtype=float)
        out[:, 0] = pts_lonlat[:, 0] * self.m_per_deg_lon
        out[:, 1] = pts_lonlat[:, 1] * self.m_per_deg_lat
        return out

    def to_lonlat(self, pts_m):
        out = np.empty_like(pts_m, dtype=float)
        out[:, 0] = pts_m[:, 0] / self.m_per_deg_lon
        out[:, 1] = pts_m[:, 1] / self.m_per_deg_lat
        return out


def cumulative_dist_m(pts_m):
    """Cumulative arc length (meters) along a polyline given in meters."""
    diffs = np.diff(pts_m, axis=0)
    d = np.sqrt((diffs[:, 0]) ** 2 + (diffs[:, 1]) ** 2)
    return np.concatenate([[0], np.cumsum(d)])


def route_distance_label(pts_lonlat, proj):
    """Return a 'XX.X mi / YY.Y km' label for a route's total distance,
    computed from its GPX track geometry."""
    pts_m = proj.to_m(pts_lonlat)
    cum = cumulative_dist_m(pts_m)
    total_m = cum[-1]
    miles = total_m / METERS_PER_MILE
    km = total_m / METERS_PER_KM
    return f"{miles:.1f} mi / {km:.1f} km"


# ---------------------------------------------------------------------------
# Parallel offset rendering
# ---------------------------------------------------------------------------

def offset_route(pts_lonlat, proj, offset_m, side, smooth_window_m=80):
    """Shift a route sideways by offset_m * side (side is +1/-1/0/etc).

    The shift direction at each point is the perpendicular to a *smoothed*
    local direction (computed by looking smooth_window_m/2 ahead and behind
    along the route, in arc-length terms). Using a smoothed direction avoids
    "spike" artifacts at sharp switchbacks that a naive point-to-point normal
    would produce.

    Returns (offset_pts_lonlat, cumulative_dist_m, offset_pts_m).
    """
    pts_m = proj.to_m(pts_lonlat)
    n = len(pts_m)
    cum = cumulative_dist_m(pts_m)

    normals = np.zeros_like(pts_m)
    for i in range(n):
        target_back = max(cum[i] - smooth_window_m / 2, cum[0])
        target_fwd = min(cum[i] + smooth_window_m / 2, cum[-1])
        p_back = np.array([
            np.interp(target_back, cum, pts_m[:, 0]),
            np.interp(target_back, cum, pts_m[:, 1]),
        ])
        p_fwd = np.array([
            np.interp(target_fwd, cum, pts_m[:, 0]),
            np.interp(target_fwd, cum, pts_m[:, 1]),
        ])
        d = p_fwd - p_back
        norm = np.array([-d[1], d[0]])
        length = np.linalg.norm(norm)
        if length > 0:
            norm = norm / length
        normals[i] = norm

    offset_pts_m = pts_m + normals * offset_m * side
    return proj.to_lonlat(offset_pts_m), cum, offset_pts_m


def assign_offset_sides(n_routes):
    """Return a side multiplier for each route, spread symmetrically.

    2 routes -> [-1, 1]
    3 routes -> [-1, 0, 1]
    4 routes -> [-1.5, -0.5, 0.5, 1.5]
    etc. Each unit corresponds to one OFFSET_M step, so adjacent routes are
    OFFSET_M apart and the whole set is centered on the "true" line.
    """
    if n_routes == 1:
        return [0]
    start = -(n_routes - 1) / 2.0
    return [start + i for i in range(n_routes)]


# ---------------------------------------------------------------------------
# Direction arrows
# ---------------------------------------------------------------------------

def place_arrows_m(pts_m, cum, spacing_m, lookahead_m=30):
    """Return list of (x, y, dx, dy) arrow anchors+directions, in meters,
    evenly spaced by arc length along the route (skipping the very start)."""
    total = cum[-1]
    n_arrows = max(1, int(total / spacing_m))
    arrows = []
    for k in range(1, n_arrows):
        s = k * spacing_m
        if s >= total:
            break
        x = np.interp(s, cum, pts_m[:, 0])
        y = np.interp(s, cum, pts_m[:, 1])
        s2 = min(s + lookahead_m, total)
        x2 = np.interp(s2, cum, pts_m[:, 0])
        y2 = np.interp(s2, cum, pts_m[:, 1])
        dx, dy = x2 - x, y2 - y
        norm = np.hypot(dx, dy)
        if norm > 0:
            dx, dy = dx / norm, dy / norm
        arrows.append((x, y, dx, dy))
    return arrows


# ---------------------------------------------------------------------------
# Waypoint (divergence/convergence) detection
# ---------------------------------------------------------------------------

def find_waypoints(routes_lonlat, proj, tolerance_m, min_unique_m, cluster_dist_m):
    """Find points where a route's path runs by itself (not within
    tolerance_m of ANY other route) for at least min_unique_m, and return
    one waypoint marker at the start and end of each such "unique" stretch.

    Waypoints that fall within cluster_dist_m of each other (e.g. because
    two routes diverge from the pack at the same junction) are merged into a
    single marker.

    Returns a list of (lon, lat) waypoint locations, in a stable order
    (roughly: in the order they're first encountered).
    """
    keys = list(routes_lonlat.keys())
    routes_m = {k: proj.to_m(v) for k, v in routes_lonlat.items()}
    trees = {k: cKDTree(v) for k, v in routes_m.items()}

    candidates = []  # (lon, lat)
    for a in keys:
        others = [o for o in keys if o != a]
        if not others:
            continue
        flags = []
        for o in others:
            dists, _ = trees[o].query(routes_m[a], k=1)
            flags.append(dists <= tolerance_m)
        near_any = np.any(flags, axis=0)
        unique_flag = ~near_any

        cum = cumulative_dist_m(routes_m[a])
        n = len(unique_flag)
        i = 0
        while i < n:
            j = i
            while j < n and unique_flag[j] == unique_flag[i]:
                j += 1
            if unique_flag[i]:
                run_len = cum[min(j, n - 1)] - cum[i]
                if run_len >= min_unique_m:
                    if i > 0:
                        candidates.append((routes_lonlat[a][i, 0], routes_lonlat[a][i, 1]))
                    if j < n:
                        candidates.append((routes_lonlat[a][j - 1, 0], routes_lonlat[a][j - 1, 1]))
            i = j

    if not candidates:
        return []

    # Cluster nearby candidates into single waypoints
    cand_m = proj.to_m(np.array(candidates))
    used = [False] * len(candidates)
    clustered = []
    for i in range(len(candidates)):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        # Chain-link: keep expanding until no new members found, so a
        # waypoint that's close to ANY existing cluster member merges in
        # (not just close to the original seed point)
        changed = True
        while changed:
            changed = False
            for j in range(len(candidates)):
                if used[j]:
                    continue
                for g in group:
                    if np.linalg.norm(cand_m[g] - cand_m[j]) <= cluster_dist_m:
                        group.append(j)
                        used[j] = True
                        changed = True
                        break
        lons = [candidates[k][0] for k in group]
        lats = [candidates[k][1] for k in group]
        clustered.append((np.mean(lons), np.mean(lats)))

    return clustered


# ---------------------------------------------------------------------------
# Start/finish detection
# ---------------------------------------------------------------------------

def find_start_finish(routes_lonlat, proj, cluster_dist_m=100.0):
    """Return (lon, lat) of the shared start/finish location, or None.

    Looks at the start point of every route. If they're all within
    cluster_dist_m of each other, returns their centroid. Otherwise returns
    None (routes don't share a common start - e.g. point-to-point routes
    with different starts).
    """
    starts = np.array([pts[0] for pts in routes_lonlat.values()])
    if len(starts) == 1:
        return tuple(starts[0])

    starts_m = proj.to_m(starts)
    centroid_m = starts_m.mean(axis=0)
    dists = np.linalg.norm(starts_m - centroid_m, axis=1)
    if np.all(dists <= cluster_dist_m):
        centroid = proj.to_lonlat(centroid_m[np.newaxis, :])[0]
        return tuple(centroid)
    return None


# ---------------------------------------------------------------------------
# Cue sheet cross-referencing
# ---------------------------------------------------------------------------

def load_cue_sheet(path):
    """Load a cue sheet JSON file. Expected format:

        {
          "route": "60K",
          "entries": [
            {"num": 1, "dist": 0.0, "note": "Start of route"},
            {"num": 9, "dist": 13.7, "note": "Rest Stop #2 Sky Mart",
             "rest_stop": true, "rest_stop_label": "Rest Stop #2: Sky Mart"},
            ...
          ]
        }

    "dist" is the cumulative distance in MILES at that cue, matching the
    units RideWithGPS cue sheets normally use. Entries with "rest_stop":
    true are used to place rest-stop markers on the map.
    """
    import json
    with open(path) as f:
        return json.load(f)


def route_cum_miles(pts_lonlat, proj):
    """Cumulative distance in miles at each point of a route."""
    pts_m = proj.to_m(pts_lonlat)
    cum_m = cumulative_dist_m(pts_m)
    return cum_m / METERS_PER_MILE


def nearest_cue_note(cue_sheet, mile, max_diff_miles=1.5):
    """Return the note text of the cue sheet entry closest to `mile`, or
    None if the nearest entry is more than max_diff_miles away (i.e. this
    route doesn't really have a cue near that point)."""
    entries = cue_sheet['entries']
    diffs = [abs(e['dist'] - mile) for e in entries]
    idx = int(np.argmin(diffs))
    if diffs[idx] > max_diff_miles:
        return None
    return entries[idx]['note']


def annotate_waypoints_with_cues(waypoints, routes_lonlat, proj, cue_sheets,
                                  match_tolerance_m=80.0, max_cue_diff_miles=1.5):
    """For each waypoint (lon, lat), find which route(s) pass near it and
    look up the nearest cue-sheet note for each. Returns a list of dicts:

        {'lon':, 'lat':, 'notes': {route_label: (note_text, mile, km), ...}}

    cue_sheets: dict of route_label -> cue sheet dict (from load_cue_sheet).
    Routes without a cue sheet, or that don't pass within
    match_tolerance_m of the waypoint, are omitted from 'notes'.
    """
    annotated = []
    for (lon, lat) in waypoints:
        pt_m = proj.to_m(np.array([[lon, lat]]))[0]
        notes = {}
        for label, pts in routes_lonlat.items():
            if label not in cue_sheets:
                continue
            pts_m = proj.to_m(pts)
            tree = cKDTree(pts_m)
            d, idx = tree.query(pt_m, k=1)
            if d > match_tolerance_m:
                continue
            cum_miles = route_cum_miles(pts, proj)
            mile = cum_miles[idx]
            km = mile * METERS_PER_MILE / METERS_PER_KM
            note = nearest_cue_note(cue_sheets[label], mile, max_cue_diff_miles)
            if note:
                notes[label] = (note, mile, km)
        annotated.append({'lon': lon, 'lat': lat, 'notes': notes})
    return annotated


def find_rest_stops(routes_lonlat, proj, cue_sheets, cluster_dist_m=250.0):
    """Locate rest stops from cue sheets by interpolating their mile marker
    onto each route's GPX geometry, then clustering nearby rest stops
    (e.g. the same physical rest stop appearing on multiple routes' cue
    sheets) into single markers.

    Returns a list of dicts: {'lon':, 'lat':, 'label': str,
    'routes': [route_label, ...]}
    """
    candidates = []  # (lon, lat, label, route_label)
    for label, pts in routes_lonlat.items():
        if label not in cue_sheets:
            continue
        cum_miles = route_cum_miles(pts, proj)
        for entry in cue_sheets[label]['entries']:
            if not entry.get('rest_stop'):
                continue
            mile = entry['dist']
            lon = np.interp(mile, cum_miles, pts[:, 0])
            lat = np.interp(mile, cum_miles, pts[:, 1])
            rs_label = entry.get('rest_stop_label', 'Rest Stop')
            candidates.append((lon, lat, rs_label, label))

    if not candidates:
        return []

    cand_m = proj.to_m(np.array([[c[0], c[1]] for c in candidates]))
    used = [False] * len(candidates)
    clustered = []
    for i in range(len(candidates)):
        if used[i]:
            continue
        group = [i]
        used[i] = True
        # Chain-link: keep expanding until no new members found
        changed = True
        while changed:
            changed = False
            for j in range(len(candidates)):
                if used[j]:
                    continue
                for g in group:
                    if np.linalg.norm(cand_m[g] - cand_m[j]) <= cluster_dist_m:
                        group.append(j)
                        used[j] = True
                        changed = True
                        break
        lons = [candidates[k][0] for k in group]
        lats = [candidates[k][1] for k in group]
        labels = [candidates[k][2] for k in group]
        label = max(labels, key=len)
        route_labels = [candidates[k][3] for k in group]
        clustered.append({
            'lon': float(np.mean(lons)),
            'lat': float(np.mean(lats)),
            'label': label,
            'routes': route_labels,
        })
    return clustered


def draw_checkered_flag(ax, lon, lat, proj, size_m=300, n=4, zorder=8):
    """Draw a small checkered-flag marker centered at (lon, lat).

    size_m: width/height of the checkerboard in meters (pole extends below
    it). n: number of squares per side.
    """
    import matplotlib.patches as mpatches

    center_m = proj.to_m(np.array([[lon, lat]]))[0]
    cell = size_m / n

    # Flag pole: from below the checkerboard up through its bottom edge
    pole_bottom_m = center_m + np.array([0, -size_m * 0.7])
    pole_top_m = center_m + np.array([0, size_m * 0.5])
    p0 = proj.to_lonlat(pole_bottom_m[np.newaxis, :])[0]
    p1 = proj.to_lonlat(pole_top_m[np.newaxis, :])[0]
    ax.plot([p0[0], p1[0]], [p0[1], p1[1]], color='black', lw=1.5, zorder=zorder,
            solid_capstyle='butt')

    # Checkerboard squares, anchored with bottom-left corner at center_m
    for row in range(n):
        for col in range(n):
            color = 'black' if (row + col) % 2 == 0 else 'white'
            corner_m = center_m + np.array([col * cell, row * cell - size_m / 2])
            # build the 4 corners of this cell in lon/lat
            corners_m = np.array([
                corner_m,
                corner_m + [cell, 0],
                corner_m + [cell, cell],
                corner_m + [0, cell],
            ])
            corners_ll = proj.to_lonlat(corners_m)
            poly = mpatches.Polygon(corners_ll, closed=True, facecolor=color,
                                     edgecolor='black', linewidth=0.3, zorder=zorder + 1)
            ax.add_patch(poly)


def _draw_routes_and_markers(ax, routes, colors, proj, side_map,
                              offset_m, arrow_spacing_m, arrow_len_m,
                              waypoints, start_finish, rest_stops=None):
    """Draw routes, direction arrows, waypoint markers, rest-stop markers,
    and the start/finish flag onto an existing axes. Returns (lon_all,
    lat_all) arrays covering all drawn route geometry (for setting axis
    bounds)."""
    keys = list(routes.keys())
    all_lons, all_lats_list = [], []

    for key in keys:
        pts = routes[key]
        color = colors[key]
        offset_pts, cum, offset_pts_m = offset_route(pts, proj, offset_m, side_map[key])

        dist_label = route_distance_label(pts, proj)
        ax.plot(offset_pts[:, 0], offset_pts[:, 1], color=color, linewidth=4,
                label=f"{key} ({dist_label})", zorder=3)

        for (x, y, dx, dy) in place_arrows_m(offset_pts_m, cum, arrow_spacing_m):
            p0 = proj.to_lonlat(np.array([[x, y]]))[0]
            p1 = proj.to_lonlat(np.array([[x + dx * arrow_len_m, y + dy * arrow_len_m]]))[0]
            ax.annotate('', xy=p1, xytext=p0,
                         arrowprops=dict(arrowstyle='-|>', color=color, lw=2.5,
                                          mutation_scale=30),
                         zorder=5)

        all_lons.append(offset_pts[:, 0])
        all_lats_list.append(offset_pts[:, 1])

    # Nudge apart any waypoints whose markers would visually overlap
    # (close together relative to the marker size, even if genuinely
    # several hundred meters apart in real terms at this map's scale).
    wp_plot_positions = list(waypoints)
    for i in range(len(wp_plot_positions)):
        for j in range(i):
            pi = proj.to_m(np.array([[wp_plot_positions[i][0], wp_plot_positions[i][1]]]))[0]
            pj = proj.to_m(np.array([[wp_plot_positions[j][0], wp_plot_positions[j][1]]]))[0]
            d_m = np.linalg.norm(pi - pj)
            if d_m < 500:
                # push i away from j along their connecting direction
                direction = pi - pj
                norm = np.linalg.norm(direction)
                if norm < 1e-6:
                    direction = np.array([1.0, 0.0])
                else:
                    direction = direction / norm
                push_m = 350
                new_pi = pi + direction * push_m
                new_ll = proj.to_lonlat(new_pi[np.newaxis, :])[0]
                wp_plot_positions[i] = (new_ll[0], new_ll[1])

    for idx, (lon, lat) in enumerate(wp_plot_positions, start=1):
        ax.plot(lon, lat, 'o', markersize=32, markerfacecolor='white',
                markeredgecolor='black', markeredgewidth=1.5, zorder=6)
        ax.text(lon, lat, f"W{idx}", ha='center', va='center', fontsize=11,
                fontweight='bold', zorder=7)

    if rest_stops:
        # Sort by the rest stop number embedded in the label (e.g. "#1", "#2")
        import re
        def _rs_sort_key(rs):
            m = re.search(r'#(\d+)', rs['label'])
            return int(m.group(1)) if m else 99
        rest_stops_sorted = sorted(rest_stops, key=_rs_sort_key)

        for rs_idx, rs in enumerate(rest_stops_sorted, start=1):
            rs_lon, rs_lat = rs['lon'], rs['lat']
            for (wlon, wlat) in waypoints:
                d_m = np.linalg.norm(proj.to_m(np.array([[rs_lon, rs_lat]]))[0] -
                                      proj.to_m(np.array([[wlon, wlat]]))[0])
                if d_m < 250:
                    offset_m_xy = 400
                    offset_ll = proj.to_lonlat(np.array([[offset_m_xy, offset_m_xy]]))[0] - \
                                proj.to_lonlat(np.array([[0, 0]]))[0]
                    rs_lon += offset_ll[0]
                    rs_lat += offset_ll[1]
                    break
            ax.plot(rs_lon, rs_lat, marker='D', markersize=24,
                    markerfacecolor='#fff7c2', markeredgecolor='black',
                    markeredgewidth=1.5, zorder=6)
            ax.text(rs_lon, rs_lat, f"R{rs_idx}", ha='center', va='center',
                    fontsize=10, fontweight='bold', zorder=7)
        # Legend handle for rest stops
        ax.plot([], [], marker='D', markersize=12, markerfacecolor='#fff7c2',
                markeredgecolor='black', linestyle='None', label='Rest Stop')

    if start_finish is not None:
        sf_lon, sf_lat = start_finish
        flag_size_m = offset_m * 6
        draw_checkered_flag(ax, sf_lon, sf_lat, proj, size_m=flag_size_m, zorder=8)
        # Legend handle with a checkered hatch to resemble the flag
        import matplotlib.patches as mpatches
        sf_patch = mpatches.Patch(facecolor='white', edgecolor='black',
                                   hatch='++++', label='Start/Finish')
        existing_handles, existing_labels = ax.get_legend_handles_labels()
        ax._sf_legend_patch = sf_patch

    return np.concatenate(all_lons), np.concatenate(all_lats_list)


def _add_logo(ax, logo_path, size=0.07):
    """Place a logo image in the top-left corner of the given axes.

    logo_path: path to a PNG or SVG file. SVG is rasterized via cairosvg
    if available, otherwise falls back to the PNG. size is the zoom factor
    for the OffsetImage (smaller = smaller logo).
    """
    if not logo_path:
        return
    import os
    from matplotlib.offsetbox import OffsetImage, AnnotationBbox

    path = logo_path
    if path.lower().endswith('.svg'):
        try:
            import cairosvg, io
            png_bytes = cairosvg.svg2png(url=path, output_width=300)
            from PIL import Image
            img = np.array(Image.open(io.BytesIO(png_bytes)).convert('RGBA'))
            path = None
        except Exception:
            png_sibling = os.path.splitext(logo_path)[0] + '.png'
            if os.path.exists(png_sibling):
                path = png_sibling
            else:
                return
    if path:
        try:
            from PIL import Image
            img = np.array(Image.open(path).convert('RGBA'))
        except Exception:
            return

    imagebox = OffsetImage(img, zoom=size)
    imagebox.image.axes = ax
    # Place just outside top-left of the axes so it doesn't overlap the table
    ab = AnnotationBbox(imagebox, (0.0, 1.0),
                         xycoords='axes fraction',
                         box_alignment=(0, 1),
                         frameon=False,
                         pad=0.1)
    ax.add_artist(ab)


def _build_info_table_rows(waypoint_annotations, rest_stops, colors):
    """Build row data for the waypoint/rest-stop info table.

    Returns (rows, route_col_for_row) where rows is a list of
    [waypoint_label, route_label, note] and route_col_for_row is a parallel
    list giving the route label for each row (or None), used to color that
    cell to match the route's line color in the legend.
    """
    rows = []
    row_routes = []

    for i, wp in enumerate(waypoint_annotations, start=1):
        wp_label = f"WP{i}"
        if wp['notes']:
            for j, (route_label, val) in enumerate(wp['notes'].items()):
                if isinstance(val, tuple):
                    note, mile, km = val
                    note_text = f"{note}  ({mile:.1f} mi / {km:.1f} km)"
                else:
                    note_text = val  # fallback if no cue sheet (plain string)
                rows.append([wp_label if j == 0 else "", route_label, note_text])
                row_routes.append(route_label)
        else:
            rows.append([wp_label, "", f"({wp['lat']:.5f}, {wp['lon']:.5f})"])
            row_routes.append(None)

    import re
    def _rs_num(rs):
        m = re.search(r'#(\d+)', rs['label'])
        return int(m.group(1)) if m else 99

    for rs in sorted(rest_stops, key=_rs_num):
        rs_num = _rs_num(rs)
        rs_label = f"R{rs_num}" if rs_num != 99 else "RS"
        routes_str = ", ".join(sorted(set(rs['routes'])))
        rows.append([rs_label, routes_str, rs['label']])
        row_routes.append(None)

    return rows, row_routes


def _draw_info_table(ax, rows, row_routes, colors, fontsize=13):
    """Draw the waypoint/rest-stop info table onto an axes (which should
    have axis turned off)."""
    import re
    table = ax.table(cellText=rows,
                      colLabels=["WP", "Route", "Cross Streets / Notes"],
                      loc='upper center', cellLoc='left',
                      colWidths=[0.06, 0.14, 0.59])
    table.auto_set_font_size(False)
    table.set_fontsize(fontsize)
    table.scale(1, 1.9)

    for (r, c), cell in table.get_celld().items():
        cell.set_edgecolor('#cccccc')
        if r == 0:
            cell.set_text_props(fontweight='bold')
            cell.set_facecolor('#f0f0f0')
        elif c == 1:
            route_label = row_routes[r - 1]
            if route_label and route_label in colors:
                cell.set_text_props(color=colors[route_label], fontweight='bold')
            else:
                # Rest stop route column — bold for consistent K rendering
                cell.set_text_props(fontweight='bold')
        if r > 0 and re.match(r'^R\d+$', rows[r - 1][0]):
            for cc in range(3):
                table[(r, cc)].set_facecolor('#fffbe6')

    return table


def render_composite(routes, colors, title, output_path,
                      offset_m=50.0,
                      arrow_spacing_m=4000.0,
                      arrow_len_m=400.0,
                      waypoint_tolerance_m=40.0,
                      waypoint_min_unique_m=300.0,
                      waypoint_cluster_m=500.0,
                      figsize=(16, 16),
                      dpi=150,
                      basemap=True,
                      basemap_source=None,
                      start_finish_cluster_m=100.0,
                      cue_sheets=None,
                      rest_stop_cluster_m=200.0,
                      logo_path=None):
    """routes: dict of label -> Nx2 array of (lon, lat).
    colors: dict of label -> hex color string.

    basemap: if True and the `contextily` package is installed (and tiles
    are reachable), draws a street-map background behind the routes. If
    contextily isn't installed, or the tile fetch fails (e.g. no internet
    connection), falls back silently to a plain white background.

    cue_sheets: optional dict of route_label -> cue sheet dict (from
    load_cue_sheet). If provided, waypoint cross-streets are looked up from
    the cue sheets and rest-stop markers are added to the map.
    """
    keys = list(routes.keys())
    cue_sheets = cue_sheets or {}

    # Reference latitude for projection: mean latitude across all routes
    all_lats = np.concatenate([routes[k][:, 1] for k in keys])
    proj = LocalProjection(ref_lat=all_lats.mean())

    sides = assign_offset_sides(len(keys))
    side_map = dict(zip(keys, sides))

    waypoints = find_waypoints(routes, proj, waypoint_tolerance_m,
                                waypoint_min_unique_m, waypoint_cluster_m)
    start_finish = find_start_finish(routes, proj, start_finish_cluster_m)
    rest_stops = find_rest_stops(routes, proj, cue_sheets, rest_stop_cluster_m) if cue_sheets else []
    waypoint_annotations = (
        annotate_waypoints_with_cues(waypoints, routes, proj, cue_sheets)
        if cue_sheets else
        [{'lon': lon, 'lat': lat, 'notes': {}} for (lon, lat) in waypoints]
    )

    table_rows, table_row_routes = _build_info_table_rows(waypoint_annotations, rest_stops, colors)

    if table_rows:
        n_table_rows = len(table_rows) + 1  # +1 for header
        # Each row gets ~0.35 inches; header adds 0.5 base
        table_h = 0.5 + 0.35 * n_table_rows
        map_h = figsize[1]
        fig = plt.figure(figsize=(figsize[0], map_h + table_h))
        gs = gridspec.GridSpec(2, 1, height_ratios=[table_h, map_h], hspace=0.05)
        ax_table = fig.add_subplot(gs[0])
        ax_table.axis('off')
        _draw_info_table(ax_table, table_rows, table_row_routes, colors)
        ax = fig.add_subplot(gs[1])
    else:
        fig, ax = plt.subplots(figsize=figsize)

    lon_all, lat_all = _draw_routes_and_markers(
        ax, routes, colors, proj, side_map,
        offset_m, arrow_spacing_m, arrow_len_m,
        waypoints, start_finish, rest_stops)

    # Bounds with a small margin
    lon_pad = (lon_all.max() - lon_all.min()) * 0.03
    lat_pad = (lat_all.max() - lat_all.min()) * 0.03
    ax.set_xlim(lon_all.min() - lon_pad, lon_all.max() + lon_pad)
    ax.set_ylim(lat_all.min() - lat_pad, lat_all.max() + lat_pad)

    ax.set_aspect('equal')

    # Basemap
    basemap_added = False
    if basemap and HAVE_CONTEXTILY:
        try:
            source = basemap_source or cx.providers.OpenStreetMap.Mapnik
            cx.add_basemap(ax, crs='EPSG:4326', source=source, zorder=0,
                            attribution_size=6)
            basemap_added = True
        except Exception as e:
            print(f"Warning: basemap could not be added ({e}). "
                  f"Continuing without a background map.")
    elif basemap and not HAVE_CONTEXTILY:
        print("Note: 'contextily' is not installed. Install it with:\n"
              "    pip install contextily --break-system-packages\n"
              "to enable a street-map background.")

    ax.set_xticks([])
    ax.set_yticks([])
    if not basemap_added:
        for spine in ax.spines.values():
            spine.set_visible(False)

    # Legend on the map
    handles, lbls = ax.get_legend_handles_labels()
    if hasattr(ax, '_sf_legend_patch'):
        handles.append(ax._sf_legend_patch)
        lbls.append('Start/Finish')
    ax.legend(handles, lbls, loc='lower left', fontsize=16, markerscale=1.5, framealpha=0.9)

    # Title and logo — placed in the table axis (or as figure suptitle if
    # no table). Using the table axis keeps them visually above the map.
    if table_rows:
        ax_table.set_title(title, fontsize=20, fontweight='bold', pad=8, loc='center')
        _add_logo(ax_table, logo_path)
    else:
        ax.set_title(title, fontsize=20, fontweight='bold')
        _add_logo(ax, logo_path)

    plt.tight_layout()
    plt.savefig(output_path, dpi=dpi, bbox_inches='tight')
    plt.close(fig)

    return waypoint_annotations, start_finish, rest_stops


def render_overlay_png(routes, colors, output_path,
                        offset_m=50.0,
                        arrow_spacing_m=4000.0,
                        arrow_len_m=400.0,
                        waypoint_tolerance_m=40.0,
                        waypoint_min_unique_m=300.0,
                        waypoint_cluster_m=500.0,
                        start_finish_cluster_m=100.0,
                        dpi=150,
                        bbox_pad_frac=0.03,
                        cue_sheets=None,
                        rest_stop_cluster_m=200.0):
    """Render routes + markers ONLY (no title, legend, axes, or background)
    as a transparent PNG, sized to exactly cover the routes' lon/lat bounding
    box (plus bbox_pad_frac padding on each side).

    This is meant to be layered over a separately-obtained map image (e.g. a
    screenshot of the same area from Google Maps) in an image editor. Because
    matplotlib doesn't know anything about how that other image was
    generated, getting the two to align requires matching:
      1. The aspect ratio (this function preserves the lon/lat aspect ratio
         using an equirectangular approximation, same as the main map - this
         is also how Google Maps renders at city scale, so it should be
         close).
      2. The geographic bounding box - printed to the console and returned,
         so you can pan/zoom the other map to roughly that box, then scale
         the overlay image to fit.

    Returns the bounding box as a dict: {'lon_min', 'lon_max', 'lat_min', 'lat_max'}.
    """
    keys = list(routes.keys())
    cue_sheets = cue_sheets or {}
    all_lats = np.concatenate([routes[k][:, 1] for k in keys])
    proj = LocalProjection(ref_lat=all_lats.mean())

    sides = assign_offset_sides(len(keys))
    side_map = dict(zip(keys, sides))

    waypoints = find_waypoints(routes, proj, waypoint_tolerance_m,
                                waypoint_min_unique_m, waypoint_cluster_m)
    start_finish = find_start_finish(routes, proj, start_finish_cluster_m)
    rest_stops = find_rest_stops(routes, proj, cue_sheets, rest_stop_cluster_m) if cue_sheets else []
    waypoint_annotations = (
        annotate_waypoints_with_cues(waypoints, routes, proj, cue_sheets)
        if cue_sheets else
        [{'lon': lon, 'lat': lat, 'notes': {}} for (lon, lat) in waypoints]
    )

    # First pass: figure out the bounding box (without drawing legend/title)
    fig, ax = plt.subplots()
    lon_all, lat_all = _draw_routes_and_markers(
        ax, routes, colors, proj, side_map,
        offset_m, arrow_spacing_m, arrow_len_m,
        waypoints, start_finish, rest_stops)
    plt.close(fig)

    lon_pad = (lon_all.max() - lon_all.min()) * bbox_pad_frac
    lat_pad = (lat_all.max() - lat_all.min()) * bbox_pad_frac
    lon_min, lon_max = lon_all.min() - lon_pad, lon_all.max() + lon_pad
    lat_min, lat_max = lat_all.min() - lat_pad, lat_all.max() + lat_pad

    # Figure size proportional to the geographic bounding box, so the output
    # PNG's aspect ratio matches a same-area map screenshot.
    lat_mid = (lat_min + lat_max) / 2
    width_m = (lon_max - lon_min) * proj.m_per_deg_lon
    height_m = (lat_max - lat_min) * proj.m_per_deg_lat
    aspect = width_m / height_m

    base_size = 10
    if aspect >= 1:
        figsize = (base_size * aspect, base_size)
    else:
        figsize = (base_size, base_size / aspect)

    fig, ax = plt.subplots(figsize=figsize)
    fig.patch.set_alpha(0.0)
    ax.patch.set_alpha(0.0)

    _draw_routes_and_markers(
        ax, routes, colors, proj, side_map,
        offset_m, arrow_spacing_m, arrow_len_m,
        waypoints, start_finish, rest_stops)

    ax.set_xlim(lon_min, lon_max)
    ax.set_ylim(lat_min, lat_max)
    ax.set_aspect('equal')
    ax.set_xticks([])
    ax.set_yticks([])
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.margins(0)
    fig.subplots_adjust(left=0, right=1, top=1, bottom=0)

    plt.savefig(output_path, dpi=dpi, transparent=True,
                bbox_inches=None, pad_inches=0)
    plt.close(fig)

    return {'lon_min': lon_min, 'lon_max': lon_max,
            'lat_min': lat_min, 'lat_max': lat_max}, waypoint_annotations, start_finish, rest_stops


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('gpx_files', nargs='+', help='GPX track files, one per route')
    parser.add_argument('--labels', nargs='+', default=None,
                         help='Label for each route (default: use GPX track name)')
    parser.add_argument('--title', default='Composite Route Map',
                         help='Map title')
    parser.add_argument('--output', default='composite_map.pdf',
                         help='Output PDF path')
    parser.add_argument('--offset-m', type=float, default=50.0,
                         help='Sideways offset between parallel routes, in meters '
                              '(default: 50). Increase for a less cluttered look '
                              'on a large-area map; decrease for a tight cluster '
                              'of short routes.')
    parser.add_argument('--arrow-spacing-m', type=float, default=4000.0,
                         help='Distance between direction arrows along each '
                              'route, in meters (default: 4000)')
    parser.add_argument('--arrow-len-m', type=float, default=400.0,
                         help='Length of each direction arrow, in meters (default: 400)')
    parser.add_argument('--waypoint-tolerance-m', type=float, default=40.0,
                         help='How close (meters) a point on one route must be '
                              'to another route to be considered "the same road" '
                              'for waypoint detection (default: 40)')
    parser.add_argument('--waypoint-min-unique-m', type=float, default=300.0,
                         help='Minimum length (meters) of a route running alone '
                              '(not near any other route) before it counts as a '
                              'divergence worth marking with a waypoint (default: 300)')
    parser.add_argument('--waypoint-cluster-m', type=float, default=500.0,
                         help='Waypoints closer than this distance (meters) '
                              'are merged into one marker - acts as a minimum '
                              'visible separation for map legibility. Two '
                              'boundary points of the same junction landing '
                              'close together are consolidated. (default: 500)')
    parser.add_argument('--dpi', type=int, default=150,
                         help='Output resolution (default: 150; use 300 for '
                              'high-quality print)')
    parser.add_argument('--no-basemap', action='store_true',
                         help='Disable the street-map background even if '
                              'the "contextily" package is installed')
    parser.add_argument('--start-finish-cluster-m', type=float, default=100.0,
                         help='If all routes start within this distance '
                              '(meters) of each other, mark that point as '
                              'Start/Finish (default: 100)')
    parser.add_argument('--overlay-png', default=None,
                         help='Also save a transparent PNG (routes/markers '
                              'only, no title/legend/background) to this '
                              'path, sized to the routes\' geographic '
                              'bounding box. Useful for layering over a '
                              'Google Maps screenshot of the same area in an '
                              'image editor. The bounding box is printed to '
                              'the console.')
    parser.add_argument('--cue-sheets', nargs='+', default=None,
                         help='Cue sheet JSON files (one per route, same '
                              'order as gpx_files). If provided: waypoint '
                              'cross-streets are looked up, and rest-stop '
                              'markers are added to the map.')
    parser.add_argument('--logo', default=None,
                         help='Path to a logo image (PNG or SVG) to place '
                              'in the top-left corner of the map. SVG is '
                              'rasterized automatically if cairosvg is '
                              'installed; otherwise provide a PNG.')
    args = parser.parse_args()

    if args.labels and len(args.labels) != len(args.gpx_files):
        parser.error('--labels must have the same number of entries as gpx_files')

    if args.cue_sheets and len(args.cue_sheets) != len(args.gpx_files):
        parser.error('--cue-sheets must have the same number of entries as gpx_files')

    routes = {}
    labels = []
    for i, path in enumerate(args.gpx_files):
        name, pts = parse_gpx_track(path)
        label = args.labels[i] if args.labels else name
        # avoid duplicate labels
        base_label = label
        n = 1
        while label in routes:
            n += 1
            label = f"{base_label} ({n})"
        routes[label] = pts
        labels.append(label)
        print(f"Loaded {path}: '{label}' ({len(pts)} points)")

    cue_sheets = {}
    if args.cue_sheets:
        for label, cue_path in zip(labels, args.cue_sheets):
            cue_sheets[label] = load_cue_sheet(cue_path)
            print(f"Loaded cue sheet {cue_path} for '{label}'")

    colors = {label: DEFAULT_COLORS[i % len(DEFAULT_COLORS)] for i, label in enumerate(labels)}

    waypoint_annotations, start_finish, rest_stops = render_composite(
        routes, colors, args.title, args.output,
        offset_m=args.offset_m,
        arrow_spacing_m=args.arrow_spacing_m,
        arrow_len_m=args.arrow_len_m,
        waypoint_tolerance_m=args.waypoint_tolerance_m,
        waypoint_min_unique_m=args.waypoint_min_unique_m,
        waypoint_cluster_m=args.waypoint_cluster_m,
        dpi=args.dpi,
        basemap=not args.no_basemap,
        start_finish_cluster_m=args.start_finish_cluster_m,
        cue_sheets=cue_sheets,
        logo_path=args.logo,
    )

    print(f"\nSaved composite map to {args.output}")

    print("\nRoute distances:")
    for label, pts in routes.items():
        all_lats = routes[label][:, 1]
        proj = LocalProjection(ref_lat=all_lats.mean())
        print(f"  {label}: {route_distance_label(pts, proj)}")

    if start_finish is not None:
        sf_lon, sf_lat = start_finish
        print(f"\nStart/Finish: lat={sf_lat:.5f}, lon={sf_lon:.5f}  "
              f"(https://www.google.com/maps?q={sf_lat:.6f},{sf_lon:.6f})")
    else:
        print("\nNo common Start/Finish detected (routes don't all start "
              "within --start-finish-cluster-m of each other)")

    if waypoint_annotations:
        print(f"\n{len(waypoint_annotations)} waypoint(s) marked:")
        for idx, wp in enumerate(waypoint_annotations, start=1):
            lon, lat = wp['lon'], wp['lat']
            print(f"  WP{idx}: lat={lat:.5f}, lon={lon:.5f}  "
                  f"(https://www.google.com/maps?q={lat:.6f},{lon:.6f})")
            for route_label, val in wp['notes'].items():
                if isinstance(val, tuple):
                    note, mile, km = val
                    print(f"      {route_label}: {note}  ({mile:.1f} mi / {km:.1f} km)")
                else:
                    print(f"      {route_label}: {val}")
    else:
        print("\nNo significant divergence points found "
              "(routes may largely overlap, or try lowering --waypoint-min-unique-m)")

    if rest_stops:
        print(f"\n{len(rest_stops)} rest stop(s) marked:")
        for rs in rest_stops:
            routes_str = ", ".join(sorted(set(rs['routes'])))
            print(f"  {rs['label']} (on {routes_str}): "
                  f"lat={rs['lat']:.5f}, lon={rs['lon']:.5f}  "
                  f"(https://www.google.com/maps?q={rs['lat']:.6f},{rs['lon']:.6f})")

    if args.overlay_png:
        bbox, _, _, _ = render_overlay_png(
            routes, colors, args.overlay_png,
            offset_m=args.offset_m,
            arrow_spacing_m=args.arrow_spacing_m,
            arrow_len_m=args.arrow_len_m,
            waypoint_tolerance_m=args.waypoint_tolerance_m,
            waypoint_min_unique_m=args.waypoint_min_unique_m,
            waypoint_cluster_m=args.waypoint_cluster_m,
            start_finish_cluster_m=args.start_finish_cluster_m,
            dpi=args.dpi,
            cue_sheets=cue_sheets,
        )
        print(f"\nSaved transparent overlay PNG to {args.overlay_png}")
        print("Geographic bounding box covered by the overlay:")
        print(f"  SW corner: {bbox['lat_min']:.6f}, {bbox['lon_min']:.6f}")
        print(f"  NE corner: {bbox['lat_max']:.6f}, {bbox['lon_max']:.6f}")
        print("To align with Google Maps: pan/zoom Google Maps so these two "
              "corners are at the edges of the visible map, screenshot it, "
              "then place the overlay PNG on top at the same size.")


if __name__ == '__main__':
    main()

# Composite Route Map

Generates a printable PDF map showing multiple bike event routes overlaid on
a single composite map - useful for ham radio / aid-station volunteers who
need a single-page reference showing all routes for an event.

## What it does

- Plots each route in its own color, with a large, easy-to-read legend
  showing each route's distance in both miles and kilometers (computed from
  the GPX track, not hardcoded)
- Marks the shared Start/Finish location with a checkered-flag icon (if all
  routes start/end within a small distance of each other)
- Where routes share the same roads, draws them as parallel offset lines so
  every route stays visible (no single route hidden underneath another)
- Adds direction-of-travel arrows along each route
- Drops numbered "WP1", "WP2", ... markers at the spots where routes
  significantly diverge or reconverge (e.g. "this is where the 100K splits
  off from the 60K route")
- If cue sheets are provided, looks up the cross-street name at each
  waypoint and adds rest-stop markers to the map (see "Cue sheets" below)
- Optionally draws an OpenStreetMap background behind the routes (requires
  the `contextily` package and internet access; falls back to a plain
  background otherwise)
- Can also export a transparent PNG of just the routes/markers, sized to
  the routes' geographic bounding box - for layering over a Google Maps
  screenshot of the same area in an image editor

This is intentionally simple: it doesn't try to detect *exact* road-by-road
overlaps. Routes are drawn as recorded; humans looking at the printed map can
tell at a glance where routes run together.

## Requirements

- Python 3
- `numpy`, `scipy`, `matplotlib`, `Pillow`

### Setup (Mac/Homebrew)

Mac's system Python is protected, so use a virtual environment:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

After the first setup, just activate before each session:
```bash
source venv/bin/activate
```

Your prompt will show `(venv)` when it's active.

### Optional: street-map background

To draw the routes on top of an OpenStreetMap background, also install
`contextily`:

```bash
pip install contextily --break-system-packages
```

This requires internet access at the time you generate the map (it
downloads map tiles). If `contextily` isn't installed, or the tiles can't be
reached, the script automatically falls back to a plain white background -
no error, just a note printed to the console. Use `--no-basemap` to disable
the background map even if `contextily` is installed.

## Getting GPX files from RideWithGPS

1. Open the route on ridewithgps.com
2. Click **Export** -> **GPX**
3. Track points only is fine - "Include POI as waypoints" and
   "Include cues as waypoints" are ignored by this script, so you don't need
   to uncheck them if you already have files with those options.

## Usage

```bash
python3 composite_map.py route1.gpx route2.gpx route3.gpx \
    --labels "36K" "60K" "100K" \
    --title "Firecracker Ride - Composite Route Map" \
    --output composite_map.pdf
```

If `--labels` is omitted, each route's name (from the GPX `<name>` tag) is
used instead.

### Example (using the included sample data)

```bash
python3 composite_map.py \
    gpx_data/2026_36K_Firecracker_Ride.gpx \
    gpx_data/60K_2026_Firecracker_Ride.gpx \
    gpx_data/2026_100K_Firecracker_Ride.gpx \
    --labels "36K" "60K" "100K" \
    --title "Firecracker Ride - Composite Route Map" \
    --output composite_map.pdf
```

### Transparent overlay for Google Maps

To get a version you can layer on top of a Google Maps screenshot, add
`--overlay-png`:

```bash
python3 composite_map.py route1.gpx route2.gpx route3.gpx \
    --labels "36K" "60K" "100K" \
    --output composite_map.pdf \
    --overlay-png overlay.png
```

This produces a transparent PNG containing only the routes, arrows,
waypoint markers, and start/finish flag - no title, legend, or background -
sized so its width:height ratio matches the routes' geographic bounding box.
The script prints that bounding box (SW/NE corners), e.g.:

```
Geographic bounding box covered by the overlay:
  SW corner: 35.508906, -79.049351
  NE corner: 35.742574, -78.835713
```

To use it: open Google Maps, pan/zoom so roughly those two corners are at
the edges of the visible map, take a screenshot, then place `overlay.png` on
top of it in an image editor (Preview, PowerPoint, GIMP, etc.) and stretch it
to fill the screenshot. Because both images use the same aspect ratio, this
should line up closely - small adjustments may be needed since Google Maps'
exact viewport depends on your browser window size and zoom level.

## Cue sheets (cross-streets and rest stops)

If you have a cue sheet (turn-by-turn directions with cumulative mileage)
for each route, the script can use it to:

- Label each waypoint with the cross-street(s) where routes diverge/converge
- Add rest-stop markers (yellow diamond) to the map

Cue sheets must be JSON files, one per route, in this format:

```json
{
  "route": "60K",
  "title": "60K 2025 Firecracker Ride Orange",
  "total_miles": 36.1,
  "entries": [
    {"num": 1, "dist": 0.0, "note": "Start of route"},
    {"num": 2, "dist": 0.6, "note": "R onto Green Oaks Pkwy"},
    {"num": 9, "dist": 13.7, "note": "Rest Stop #2 Sky Mart",
     "rest_stop": true, "rest_stop_label": "Rest Stop #2: Sky Mart"},
    ...
  ]
}
```

- `dist` is the cumulative distance **in miles** at that cue (matching
  typical RideWithGPS cue sheet exports).
- Entries with `"rest_stop": true` get a marker on the map, labeled with
  `rest_stop_label`. If the same physical rest stop appears in multiple
  routes' cue sheets (common - routes often share rest stops), they're
  automatically merged into a single marker.

See `cue_data/36K.json`, `cue_data/60K.json`, `cue_data/100K.json` for
complete examples (transcribed from the 2025 Firecracker Ride cue sheets).

### Converting a cue sheet PDF automatically

If your cue sheet is a PDF (e.g. exported from RideWithGPS) in the standard
Num / Dist / Prev / Type / Note / Next table format, `pdf_to_cue_sheet.py`
can convert it to the JSON format above automatically:

```bash
python3 pdf_to_cue_sheet.py "60K Cue Sheet.pdf" --route 60K -o cue_data/60K.json
```

This also detects rest stops (any cue containing "Rest Stop") and builds a
descriptive label from the lines immediately following it in the PDF (e.g.
merging "Rest Stop #2" + "Sky Mart" into "Rest Stop #2: Sky Mart"), while
filtering out amenity lines like "Food Water Restrooms".

**Always review the output JSON before using it** - the script prints a
reminder, and will tell you how many rest stops it found and what labels it
gave them. Automated extraction can occasionally miss a location name (for
example, if a cue sheet's rest stop entry has no location text at all, just
a note like "Only rest stop on this route") - in that case, edit the
`rest_stop_label` field by hand afterward.

If your cue sheet PDF has a different table layout and the converter
produces nothing or garbled output, run with `--debug` to see the raw
extracted text and parsed entries:

```bash
python3 pdf_to_cue_sheet.py "60K Cue Sheet.pdf" --debug
```

To use cue sheets, pass `--cue-sheets` with one JSON file per route, in the
same order as the GPX files:

```bash
python3 composite_map.py \
    gpx_data/2026_36K_Firecracker_Ride.gpx \
    gpx_data/60K_2026_Firecracker_Ride.gpx \
    gpx_data/2026_100K_Firecracker_Ride.gpx \
    --labels "36K" "60K" "100K" \
    --title "Firecracker Ride - Composite Route Map" \
    --output composite_map.pdf \
    --cue-sheets cue_data/36K.json cue_data/60K.json cue_data/100K.json
```

The cross-street lookup works by finding the point on each route nearest to
a waypoint, converting that point's position to a cumulative mile, and
finding the cue sheet entry with the closest matching mileage. This is
naturally a little approximate (GPS track length vs. cue sheet mileage can
differ by a percent or so over a long route), but in practice lands on the
correct street name.



All of these have sensible defaults but can be adjusted via command-line
flags - run `python3 composite_map.py --help` for the full list.

| Flag | Default | What it does |
|---|---|---|
| `--offset-m` | 50 | Sideways distance between parallel routes on shared roads. Increase for a less cluttered look on a large map; decrease for tightly-clustered short routes. |
| `--arrow-spacing-m` | 4000 | Distance between direction arrows along each route. |
| `--arrow-len-m` | 400 | Length of each arrow. |
| `--waypoint-tolerance-m` | 40 | How close a point on one route must be to another to be considered "the same road" for waypoint detection. |
| `--waypoint-min-unique-m` | 300 | Minimum length a route must run alone before a divergence point gets marked. |
| `--waypoint-cluster-m` | 100 | Nearby waypoint candidates within this distance get merged into one marker. |
| `--start-finish-cluster-m` | 100 | If all routes start within this distance of each other, mark that point as Start/Finish. |
| `--cue-sheets` | none | One cue sheet JSON per route (same order as the GPX files) - see "Cue sheets" above. |
| `--no-basemap` | off | Disable the OpenStreetMap background even if `contextily` is installed. |
| `--dpi` | 150 | Output resolution (use 300 for higher-quality printing). |

## Output

The PDF includes the composite map and a legend (with each route's distance
in miles and km). If waypoints or rest stops were found, a summary line is
printed at the bottom of the page (cross-streets if cue sheets were
provided, otherwise just coordinates).

The console output additionally lists each route's total distance, the
Start/Finish location, every waypoint (with cross-street names if cue sheets
were provided), and every rest stop - each with a clickable Google Maps
link, useful for cross-referencing or plugging into a separate mapping tool.

## Limitations / ideas for follow-up

- The waypoint detection is a simple heuristic (nearest-neighbor distance +
  minimum run length + spatial clustering). For routes with lots of small
  GPS jitter or many short shared/unshared alternations, you may need to
  adjust `--waypoint-tolerance-m` / `--waypoint-min-unique-m`.
- The Start/Finish marker only appears if all routes start within
  `--start-finish-cluster-m` of each other. For events where routes start
  at different locations, no marker is drawn (the script still runs fine).
- Currently supports any number of routes, but colors repeat after 7 routes
  and the parallel-offset spacing can get visually busy with many
  similar-length routes on the same roads.

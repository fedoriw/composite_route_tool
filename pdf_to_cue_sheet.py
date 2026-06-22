#!/usr/bin/env python3
"""
Cue Sheet PDF -> JSON Converter
=================================

Converts a RideWithGPS-style cue sheet PDF (Num / Dist / Prev / Type / Note
/ Next columns) into the JSON format used by composite_map.py's
--cue-sheets option.

This is built for the common RideWithGPS PDF cue sheet layout. It may need
adjustment for cue sheets from other sources with a different table
structure - if it produces garbled output, check the --debug output first
(it prints the raw extracted lines) before assuming the parser needs a fix.

USAGE
-----
    python3 pdf_to_cue_sheet.py cue_sheet.pdf -o cue_data/60K.json --route 60K

    # Inspect raw extracted text without parsing, to debug a new PDF format:
    python3 pdf_to_cue_sheet.py cue_sheet.pdf --debug

REST STOP DETECTION
--------------------
Any cue whose note contains "Rest Stop" is flagged as a rest stop
candidate. Continuation lines immediately following (location name, amenity
list like "Food Water Restrooms") are merged into a descriptive label using
this heuristic:
  - A continuation line that itself contains "Rest Stop" or amenity words
    (Food/Water/Restroom/Restrooms/etc) is treated as an amenity line and
    skipped for labeling purposes.
  - Any other continuation line is treated as the rest stop's location name
    and appended to the label, e.g. "Rest Stop #3: Olive Chapel Baptist
    Church".

This heuristic works for the standard RideWithGPS layout but cue sheets
vary - ALWAYS REVIEW THE OUTPUT JSON before using it, especially the
rest_stop_label fields, and adjust by hand if a location name was merged
incorrectly or missed.
"""

import argparse
import json
import re
import subprocess
import sys

AMENITY_WORDS = {'food', 'water', 'restroom', 'restrooms', 'sag', 'medical',
                  'mechanic', 'first', 'aid', 'ice', 'fruit', 'restock'}

# Arrow/direction glyphs used by the "web export" RideWithGPS cue sheet format
DIRECTION_ARROWS = '←→↑↓↗↘↙↖'


def extract_layout_text(pdf_path):
    """Run pdftotext -layout and return the raw text."""
    result = subprocess.run(
        ['pdftotext', '-layout', pdf_path, '-'],
        capture_output=True, text=True, check=True
    )
    return result.stdout


# Matches lines like:
#   1.    0.0     0.0          Start of route                                              0.6
#  10.   18.2     1.3          R onto Piney Grove-Wilbon Rd                                0.4
# Num and Dist/Prev/Next are numeric; Note is free text in between.
CUE_LINE_RE = re.compile(
    r'^\s*(\d+)\.\s+([\d.]+)\s+([\d.]+)\s+(.*?)\s+([\d.]+)\s*$'
)

# Continuation lines: no leading "N." but indented text, e.g.
#                              Only rest stop on the 36K route.
#                              Food Water Restrooms
CONTINUATION_RE = re.compile(r'^\s{15,}(\S.*?)\s*$')

# --- Format 2: RideWithGPS "web export" cue sheet ---
# Columns: Leg(optional, blank on first row) Dir(arrow) Type Notes... Total
# e.g.:
#   3.2 ←              Left              Turn left onto Tody Goodwin Rd                  5.5
#       ←              Left              Turn left onto Shearon Harris Rd                0.3
WEB_CUE_LINE_RE = re.compile(
    r'^\s*([\d.]*)\s*([' + DIRECTION_ARROWS + r'])\s+(\S+)\s+(.*?)\s+([\d.]+)\s*$'
)
# A bare continuation line (wrapped long note, no leg/dir/type/total columns)
WEB_CONTINUATION_RE = re.compile(
    r'^\s{4,}(?!.*[' + DIRECTION_ARROWS + r'])(\S.*?)\s*$'
)


def detect_format(raw_text):
    """Return 'numbered' (Num/Dist/Prev/Type/Note/Next) or 'web' (Leg/Dir/
    Type/Notes/Total) based on which pattern matches more lines."""
    numbered_hits = sum(1 for l in raw_text.split('\n') if CUE_LINE_RE.match(l))
    web_hits = sum(1 for l in raw_text.split('\n') if WEB_CUE_LINE_RE.match(l))
    return 'web' if web_hits > numbered_hits else 'numbered'


def parse_web_cue_lines(raw_text):
    """Parse the 'web export' cue sheet format (Leg/Dir/Type/Notes/Total).

    Distance ("Total") here is already cumulative, same meaning as "Dist"
    in the numbered format. Returns the same entry shape as
    parse_cue_lines() for compatibility: {num, dist, note, continuation_lines}.

    Long notes (typically rest-stop descriptions) wrap across the line(s)
    BEFORE the data row itself, with the data row's own Notes field left
    blank in that case, e.g.:

        Rest Stop #1 - Refuel Exxon (formerly known as
    1.3 ↑ Water                                                10.1
        Wilsonville)

    Here "Wilsonville)" is itself a continuation line that precedes the
    NEXT data row, even though visually it reads like a trailing fragment
    of the rest stop above. Since a bare continuation line's own content
    can't reliably distinguish "belongs to the previous row" from "belongs
    to the next row", continuation lines are always treated as belonging to
    whatever data row comes next, accumulating in order until that row is
    reached.
    """
    lines = raw_text.split('\n')
    entries = []
    pending_before = []  # text fragments seen before the next data row
    num = 0

    for line in lines:
        if not line.strip():
            continue
        stripped = line.strip()

        if stripped.startswith('Leg') and 'Total' in stripped:
            continue
        if 'ridewithgps.com' in stripped.lower() or 'Cue sheet for' in stripped:
            continue
        if re.match(r'^\d+/\d+$', stripped):
            continue
        if stripped.endswith('miles') and not any(a in stripped for a in DIRECTION_ARROWS):
            continue
        if re.match(r'^\d+/\d+/\d+,', stripped):
            continue

        m = WEB_CUE_LINE_RE.match(line)
        if m:
            num += 1
            dist = float(m.group(5))
            note = m.group(4).strip()

            if pending_before:
                prefix = ' '.join(pending_before)
                note = f"{prefix} {note}".strip() if note else prefix
                pending_before = []

            entries.append({'num': num, 'dist': dist, 'note': note, 'continuation_lines': []})
            continue

        cm = WEB_CONTINUATION_RE.match(line)
        if cm:
            text = cm.group(1).strip()
            # If the most recently completed entry's note has an unbalanced
            # opening parenthesis, this line closes it out (trailing
            # continuation) rather than starting the next row's note.
            if entries and entries[-1]['note'].count('(') > entries[-1]['note'].count(')'):
                entries[-1]['note'] = f"{entries[-1]['note']} {text}".strip()
            else:
                pending_before.append(text)
            continue
        # else: unrecognized line - ignore

    return entries


def parse_cue_lines(raw_text):
    """Parse the raw pdftotext -layout output into a list of cue entries.

    Returns a list of dicts: {num, dist, note, continuation_lines: [...]}
    """
    lines = raw_text.split('\n')
    entries = []
    current = None

    for line in lines:
        if not line.strip():
            continue

        # Skip repeated page headers / footers
        stripped = line.strip()
        if stripped.startswith('Num') and 'Dist' in stripped:
            continue
        if re.match(r'^[\d.]+ miles\.', stripped):
            continue
        if stripped.startswith('For Emergency') or stripped.startswith('For SAG'):
            continue
        # Skip the title line (first non-table line, doesn't match cue or
        # continuation pattern and doesn't look like table data)
        m = CUE_LINE_RE.match(line)
        if m:
            if current:
                entries.append(current)
            num = int(m.group(1))
            dist = float(m.group(2))
            note = m.group(4).strip()
            current = {'num': num, 'dist': dist, 'note': note, 'continuation_lines': []}
            continue

        cm = CONTINUATION_RE.match(line)
        if cm and current:
            current['continuation_lines'].append(cm.group(1).strip())
            continue
        # Otherwise: title line, blank-ish, or unrecognized - ignore

    if current:
        entries.append(current)

    return entries


def is_amenity_line(text):
    words = set(re.findall(r'[a-zA-Z]+', text.lower()))
    return bool(words & AMENITY_WORDS)


def build_cue_sheet(entries, route_label, title=None):
    """Convert parsed entries into the cue_data JSON structure, detecting
    rest stops and building descriptive labels from continuation lines."""
    out_entries = []
    rest_stop_counter = 0

    for e in entries:
        note = e['note'].replace('\u200b', '').replace('\u00a0', ' ')
        note = re.sub(r'\s+', ' ', note).strip()
        is_rest_stop = 'rest stop' in note.lower() and 'ahead' not in note.lower()

        entry = {'num': e['num'], 'dist': e['dist'], 'note': note}

        if is_rest_stop:
            # Try to extract "#N" from the note for numbering; fall back to
            # an incrementing counter.
            m = re.search(r'#(\d+)', note)
            if m:
                rs_num = m.group(1)
            else:
                rest_stop_counter += 1
                rs_num = str(rest_stop_counter)

            location_parts = []
            for cl in e['continuation_lines']:
                if not is_amenity_line(cl):
                    location_parts.append(cl)

            if location_parts:
                label = f"Rest Stop #{rs_num}: {' '.join(location_parts)}"
            elif ':' in note or any(c.isalpha() for c in note.split('#')[-1]):
                # note itself may already contain a location name after the
                # "Rest Stop #N" prefix, e.g. "Rest Stop #2 Sky Mart" or
                # "Rest Stop #1 - Refuel Exxon (formerly Wilsonville)"
                after = re.sub(r'^.*?#\d+\s*[-:]?\s*', '', note).strip()
                label = f"Rest Stop #{rs_num}: {after}" if after else f"Rest Stop #{rs_num}"
            else:
                label = f"Rest Stop #{rs_num}"

            entry['rest_stop'] = True
            entry['rest_stop_label'] = label

        out_entries.append(entry)

    total_miles = out_entries[-1]['dist'] if out_entries else 0.0

    return {
        'route': route_label,
        'title': title or route_label,
        'total_miles': total_miles,
        'entries': out_entries,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('pdf_path', help='Cue sheet PDF file')
    parser.add_argument('-o', '--output', default=None,
                         help='Output JSON path (default: print to stdout)')
    parser.add_argument('--route', default=None,
                         help='Route label, e.g. "60K" (default: derived from filename)')
    parser.add_argument('--title', default=None,
                         help='Title for the cue sheet (default: same as --route)')
    parser.add_argument('--debug', action='store_true',
                         help='Print raw extracted text and parsed entries '
                              'without building the final JSON (use this to '
                              'troubleshoot a cue sheet PDF with a different layout)')
    args = parser.parse_args()

    raw_text = extract_layout_text(args.pdf_path)
    fmt = detect_format(raw_text)
    parse_fn = parse_web_cue_lines if fmt == 'web' else parse_cue_lines

    if args.debug:
        print(f"=== DETECTED FORMAT: {fmt} ===")
        print("=== RAW EXTRACTED TEXT ===")
        print(raw_text)
        print("\n=== PARSED ENTRIES ===")
        entries = parse_fn(raw_text)
        for e in entries:
            print(e)
        return

    entries = parse_fn(raw_text)
    if not entries:
        print("No cue entries found. Try --debug to inspect the raw "
              "extracted text and check whether this PDF's layout matches "
              "the expected format.", file=sys.stderr)
        sys.exit(1)

    route_label = args.route
    if not route_label:
        import os
        base = os.path.splitext(os.path.basename(args.pdf_path))[0]
        m = re.search(r'(\d+)\s*[kKmM]\b', base)
        route_label = (m.group(1) + base[m.end()-1].upper()) if m else base

    cue_sheet = build_cue_sheet(entries, route_label, args.title)
    print(f"(parsed using '{fmt}' format detector)")

    output_json = json.dumps(cue_sheet, indent=2)

    if args.output:
        with open(args.output, 'w') as f:
            f.write(output_json + '\n')
        print(f"Wrote {len(cue_sheet['entries'])} entries to {args.output}")
        rest_stops = [e for e in cue_sheet['entries'] if e.get('rest_stop')]
        if rest_stops:
            print(f"Detected {len(rest_stops)} rest stop(s):")
            for rs in rest_stops:
                print(f"  {rs['rest_stop_label']}  (mile {rs['dist']})")
        print("\nIMPORTANT: review the output JSON, especially rest_stop_label "
              "fields, before using it - automated extraction can miss or "
              "misplace location names.")
    else:
        print(output_json)


if __name__ == '__main__':
    main()

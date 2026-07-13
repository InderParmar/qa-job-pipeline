"""
Resolves a numeric LinkedIn geoId for every location used across your config files, and
writes it into each search query as an explicit "geoId" field. This makes location
matching deterministic instead of relying on LinkedIn's free-text guessing -- which can
silently return 0 results or the wrong region for smaller/ambiguous city names (e.g.
"Cambridge" could mean Cambridge, ON or Cambridge, UK; "Charlottetown" might not resolve
cleanly to a free-text match at all).

IMPORTANT -- read before trusting this blindly:
This uses LinkedIn's undocumented public typeahead endpoint (the same one the LinkedIn
jobs search page itself calls when you type into the location box). It is NOT an
official API, its response shape isn't guaranteed, and it wasn't possible to verify this
script against a live response while building it. The parsing is regex-based specifically
so it tolerates schema drift rather than crashing outright, but you should:
  1. Run with --dry-run first and read the printed results.
  2. Spot-check 2-3 resolved geoIds against LinkedIn's own site: manually search that
     location on linkedin.com/jobs, and confirm the geoId in the resulting URL's
     ?geoId=... parameter matches what this script resolved.
  3. Only then re-run without --dry-run to actually write into your config files.

Usage:
    python resolve_geoids.py --dry-run           # resolve and print only, write nothing
    python resolve_geoids.py                     # resolve and write geoId into config files
    python resolve_geoids.py --config-dir config --cache geoid_cache.json
"""

import argparse
import glob
import json
import os
import re
import time

import requests

TYPEAHEAD_URL = "https://www.linkedin.com/jobs-guest/api/typeaheadHits"

# A realistic desktop User-Agent -- LinkedIn's guest endpoints are more likely to reject
# an obviously non-browser request (e.g. python-requests' default UA string).
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
}

# Matches both urn:li:geo:12345 and urn:li:fs_geo:12345 -- LinkedIn has used both forms
# in different parts of its API over time.
GEO_URN_RE = re.compile(r'urn:li:(?:fs_)?geo:(\d+)')
DISPLAY_NAME_RE = re.compile(r'"(?:displayName|text)"\s*:\s*"([^"]+)"')


def discover_config_files(config_dir='config', include_root=True):
    files = []
    if include_root and os.path.exists('config.json'):
        files.append('config.json')
    files.extend(sorted(glob.glob(os.path.join(config_dir, '*.json'))))
    return files


def collect_locations(config_files):
    """Returns {location_string: [config_paths_using_it]} for every query that doesn't
    already have a geoId set (so re-running this script is always safe/idempotent --
    it never overwrites a geoId you've already resolved or set by hand).
    """
    locations = {}
    for path in config_files:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception as e:
            print(f"Skipping {path} (couldn't parse: {e})")
            continue
        for q in data.get('search_queries', []):
            loc = q.get('location')
            if loc and not q.get('geoId'):
                locations.setdefault(loc, []).append(path)
    return locations


def resolve_geoid(location, retries=3, delay=2):
    """Returns (geoId, matched_display_name) or (None, None) if nothing was found."""
    for attempt in range(retries):
        try:
            resp = requests.get(
                TYPEAHEAD_URL,
                params={
                    'origin': 'jserp',
                    'typeaheadType': 'GEO',
                    'query': location,
                },
                headers=HEADERS,
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"  non-200 ({resp.status_code}) resolving {location!r}, retrying...")
                time.sleep(delay)
                continue
            text = resp.text
            geo_ids = GEO_URN_RE.findall(text)
            names = DISPLAY_NAME_RE.findall(text)
            if not geo_ids:
                return None, None
            # The first hit is LinkedIn's own top match for the query text.
            return geo_ids[0], (names[0] if names else None)
        except requests.exceptions.RequestException as e:
            print(f"  error resolving {location!r}: {e}, retrying...")
            time.sleep(delay)
    return None, None


def apply_geoids(config_files, cache, dry_run):
    for path in config_files:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        changed = False
        for q in data.get('search_queries', []):
            loc = q.get('location')
            if loc in cache and cache[loc].get('geoId') and not q.get('geoId'):
                q['geoId'] = cache[loc]['geoId']
                changed = True
        if changed:
            if dry_run:
                print(f"[dry-run] would update {path}")
            else:
                with open(path, 'w') as f:
                    json.dump(data, f, indent=2)
                    f.write('\n')
                print(f"Updated {path}")


def main():
    parser = argparse.ArgumentParser(description="Resolve LinkedIn geoIds for every config location.")
    parser.add_argument('--config-dir', default='config')
    parser.add_argument('--dry-run', action='store_true', help="Resolve and print only, write nothing")
    parser.add_argument('--cache', default='geoid_cache.json', help="Where resolved location->geoId pairs are saved for reuse")
    args = parser.parse_args()

    config_files = discover_config_files(args.config_dir)
    locations = collect_locations(config_files)
    print(f"Found {len(locations)} unique location(s) without a geoId, across {len(config_files)} config file(s).\n")

    cache = {}
    if os.path.exists(args.cache):
        with open(args.cache) as f:
            cache = json.load(f)

    for loc in sorted(locations):
        if loc in cache:
            print(f"(cached) {loc!r} -> geoId={cache[loc]['geoId']} ({cache[loc]['matched_name']})")
            continue
        geo_id, name = resolve_geoid(loc)
        time.sleep(1)  # be polite between lookups, this isn't the main scraper's traffic
        if geo_id:
            print(f"{loc!r} -> geoId={geo_id} ({name})")
            cache[loc] = {'geoId': geo_id, 'matched_name': name}
        else:
            print(f"{loc!r} -> NOT FOUND -- will keep using free-text location for this one")
            cache[loc] = {'geoId': None, 'matched_name': None}

    with open(args.cache, 'w') as f:
        json.dump(cache, f, indent=2)

    if args.dry_run:
        print("\nDry run -- no config files were modified. Spot-check the geoIds above against "
              "LinkedIn's own site, then re-run without --dry-run to apply them.")
        return

    apply_geoids(config_files, cache, dry_run=False)
    print("\nDone. Review the diffs in your config files before your next scrape run.")


if __name__ == "__main__":
    main()

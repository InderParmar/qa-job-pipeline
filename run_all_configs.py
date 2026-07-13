"""
Runs the scraper (main.py) against every config file automatically, so you don't have
to invoke `python main.py config/config_xxx.json` by hand for each city.

Usage:
    python run_all_configs.py

    # Custom pause between configs (seconds), default 45:
    python run_all_configs.py --pause 60

    # Only scan a different folder for config files:
    python run_all_configs.py --config-dir config

    # Skip the root config.json and only run files inside config/:
    python run_all_configs.py --no-root-config

What it does:
    1. Loads every *.json file in the root directory (config.json) and the config/
       folder, validating each is well-formed JSON before running it.
    2. Any file that fails to parse (e.g. a broken/example file) is skipped with a
       warning instead of stopping the whole run.
    3. Runs main.main(config_path) for each valid config, one at a time.
    4. Pauses between configs (default 45s) so the run doesn't look like a burst of
       automated requests to LinkedIn.
    5. Prints a summary at the end: which configs succeeded, which failed, and why.
"""

import argparse
import glob
import json
import os
import sys
import time

# main.py must be importable from the same directory this script lives in.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from main import main as run_scraper


def discover_config_files(config_dir, include_root_config):
    candidates = []
    if include_root_config and os.path.exists('config.json'):
        candidates.append('config.json')
    if os.path.isdir(config_dir):
        candidates.extend(sorted(glob.glob(os.path.join(config_dir, '*.json'))))
    return candidates


def validate_config(path):
    # Returns (is_valid, error_message). Catches malformed JSON (e.g. two objects
    # pasted together) and missing required keys before we ever try to run main().
    required_keys = [
        'search_queries', 'db_path', 'jobs_tablename', 'filtered_jobs_tablename',
        'pages_to_scrape', 'rounds', 'days_to_scrape', 'timespan',
    ]
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return False, f"invalid JSON ({e})"
    except Exception as e:
        return False, f"could not read file ({e})"

    missing = [k for k in required_keys if k not in data]
    if missing:
        return False, f"missing required keys: {', '.join(missing)}"
    return True, None


def run_all(config_dir='config', pause=45, include_root_config=True):
    """Runs the scraper against every valid config file found. Returns the list of
    (path, status, detail) results so callers (like pipeline.py) can inspect what
    happened without parsing stdout.
    """
    config_files = discover_config_files(config_dir, include_root_config)
    if not config_files:
        print(f"No config files found (checked config.json and {config_dir}/*.json). Nothing to run.")
        return []

    print(f"Found {len(config_files)} config file(s) to check:")
    for path in config_files:
        print(f"  - {path}")
    print()

    results = []  # list of (path, status, detail)
    for i, path in enumerate(config_files):
        is_valid, error = validate_config(path)
        if not is_valid:
            print(f"SKIPPING {path}: {error}")
            results.append((path, "skipped", error))
            continue

        print(f"\n{'='*60}\nRunning scraper with config: {path}\n{'='*60}")
        try:
            run_scraper(path)
            results.append((path, "success", None))
        except Exception as e:
            print(f"ERROR while running {path}: {e}")
            results.append((path, "failed", str(e)))

        is_last = (i == len(config_files) - 1)
        if not is_last and pause > 0:
            print(f"\nPausing {pause}s before the next config...")
            time.sleep(pause)

    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    for path, status, detail in results:
        line = f"{status.upper():8} {path}"
        if detail:
            line += f"  ({detail})"
        print(line)

    succeeded = sum(1 for _, status, _ in results if status == "success")
    failed = sum(1 for _, status, _ in results if status == "failed")
    skipped = sum(1 for _, status, _ in results if status == "skipped")
    print(f"\n{succeeded} succeeded, {failed} failed, {skipped} skipped, out of {len(results)} total.")
    return results


def main_runner():
    parser = argparse.ArgumentParser(description="Run the LinkedIn scraper against every config file.")
    parser.add_argument('--config-dir', default='config', help="Folder to scan for config *.json files (default: config)")
    parser.add_argument('--pause', type=float, default=45, help="Seconds to pause between configs (default: 45)")
    parser.add_argument('--no-root-config', dest='include_root_config', action='store_false',
                         help="Skip the root config.json and only run files inside --config-dir")
    args = parser.parse_args()

    run_all(config_dir=args.config_dir, pause=args.pause, include_root_config=args.include_root_config)


if __name__ == "__main__":
    main_runner()

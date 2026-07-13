"""
Runs the full automated pipeline in one shot: scrape every config -> AI-screen every
new job for suitability -> email a notification for anything that newly qualifies.

This is the single entry point the GitHub Actions workflow (or a local cron job) calls
on a schedule.

Env vars used (all read by the modules this script calls):
    OPENAI_API_KEY    required, for ai_screening.py
    OPENAI_MODEL      optional, default gpt-4o-mini
    SUITABILITY_THRESHOLD  optional, default 65
    SMTP_USER / SMTP_PASS  required, for notify.py
    SMTP_HOST / SMTP_PORT  optional
    NOTIFY_EMAIL_TO       optional, defaults to SMTP_USER
    PIPELINE_CONFIG_PAUSE  optional, seconds between each scraper config (default 20)
"""

import os
import sys
import time

import run_all_configs
import ai_screening
import notify


def run_pipeline():
    start = time.perf_counter()

    print(f"\n{'#'*60}\nSTEP 1/3: Scraping all configs\n{'#'*60}")
    pause = float(os.environ.get('PIPELINE_CONFIG_PAUSE', '20'))
    scrape_results = run_all_configs.run_all(config_dir='config', pause=pause, include_root_config=True)

    print(f"\n{'#'*60}\nSTEP 2/3: AI-screening new jobs for suitability\n{'#'*60}")
    try:
        qualifying_jobs = ai_screening.screen_all_databases(data_dir='data')
    except Exception as e:
        print(f"ERROR during AI screening: {e}")
        qualifying_jobs = []

    print(f"\n{'#'*60}\nSTEP 3/3: Notifying about {len(qualifying_jobs)} qualifying job(s)\n{'#'*60}")
    try:
        notify.send_qualifying_jobs_email(qualifying_jobs)
    except Exception as e:
        print(f"ERROR sending notification email: {e}")

    elapsed = time.perf_counter() - start
    print(f"\nPipeline finished in {elapsed:.1f}s. "
          f"{sum(1 for _, s, _ in scrape_results if s == 'success')} config(s) scraped successfully, "
          f"{len(qualifying_jobs)} new qualifying job(s) found.")


if __name__ == "__main__":
    run_pipeline()

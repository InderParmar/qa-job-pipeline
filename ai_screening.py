"""
Screens scraped jobs against a candidate profile using an LLM, scoring each job's
suitability and writing the result back into the same row in the jobs table.

This is deliberately separate from main.py (scraping) so it can be re-run independently,
re-scored with a different model, or skipped entirely without touching the scraper.

Usage (screens every un-screened job across every .db file in ./data):
    python ai_screening.py

Usage (screen a single db):
    python ai_screening.py --db data/calgary.db
"""

import argparse
import glob
import json
import os
import sqlite3
import sys
import time

from openai import OpenAI

# ---------------------------------------------------------------------------
# Candidate profile. Edit this block (or point PROFILE_PATH at a text file via
# the CANDIDATE_PROFILE_PATH env var) whenever the resume/targeting rules change --
# everything below is fed to the model as-is on every scoring call.
# ---------------------------------------------------------------------------

DEFAULT_PROFILE = """
WHO I AM
Role: QA Automation Engineer (fresh grad, actively job hunting)
Location: Toronto, ON, Canada. Open to remote within Canada.
Education: Bachelor of Software Engineering, Seneca Polytechnic (Jan 2022 - Dec 2025)

SKILLS
QA & Automation: Selenium WebDriver, Playwright, Python, pytest, Java, BDD/Gherkin (pytest-bdd),
Page Object Model, Data-Driven Testing, Regression Testing, Performance Testing, Cross-Browser Testing
API & Databases: Postman, REST API Testing, Python requests, Schema Validation, SQL (Oracle, PostgreSQL),
RESTful APIs, JSON/XML Validation
Tools & Delivery: Git, GitHub Actions, GitHub Pages, Locust, Docker, CI/CD Pipelines, Agile/Scrum,
JIRA, Zephyr, pytest-html, Root Cause Analysis

EXPERIENCE (~1.5 years total, 2 co-ops)
1. Quality Assurance Specialist - Seneca Polytechnic (May 2024 - Aug 2024): Selenium/Java automation,
   80% regression reduction, Oracle SQL validation, Agile/Scrum, JIRA + Zephyr.
2. Quality Assurance Engineer, Web Application - ImmigrateX/CredWise (Sep 2024 - Dec 2024): Functional,
   regression, negative testing; backend validation; defect/root cause analysis.
3. IT Support Specialist - Seneca Polytechnic (Sep 2024 - Dec 2025): Root cause analysis, SLA adherence.

FLAGSHIP PROJECT (AutoShield): 5-layer banking QA platform - Playwright UI (POM), REST API suite,
BDD/Gherkin (pytest-bdd), Locust performance testing, GitHub Actions CI/CD with a live published
HTML dashboard. Also: ShopSafe (Selenium e-commerce suite) and a REST API test suite for Reqres.in.

HONEST SKILL LEVEL
- Strong: Python, pytest, Selenium, Playwright, POM, DDT, BDD/Gherkin, REST API testing,
  GitHub Actions CI/CD, Locust, Docker
- Moderate: Java (used for Selenium only, not full-stack dev), SQL (Oracle/PostgreSQL)
- Basic/awareness only: Spring Boot, Jenkins, RPA tools (BluePrism/UiPath)
- NOT a fit for: senior roles (5+ years experience required), roles requiring Robot Framework,
  mobile automation (Appium), or any role outside Canada that isn't fully remote

JOB TARGETING
Looking for: Junior or Intermediate QA Automation Engineer / SDET roles, 0-3 years experience.
Type: full-time preferred, open to contract. Canadian work authorization, no sponsorship needed.

Best-fit signals in a job posting:
- Selenium OR Playwright explicitly listed
- Python OR Java for automation
- 0-3 years experience required
- GitHub Actions / CI/CD mentioned
- REST API testing mentioned
- BDD/Gherkin a plus

Disqualifying signals:
- 5+ years experience required
- Robot Framework required
- Mobile-only automation (Appium) as the core focus
- Location outside Canada and not fully remote
""".strip()


def load_profile():
    custom_path = os.environ.get('CANDIDATE_PROFILE_PATH')
    if custom_path and os.path.exists(custom_path):
        with open(custom_path) as f:
            return f.read().strip()
    return DEFAULT_PROFILE


SUITABILITY_THRESHOLD = int(os.environ.get('SUITABILITY_THRESHOLD') or '65')

SYSTEM_PROMPT = """You are screening job postings for a specific candidate against their profile.
Score how suitable each posting is for this exact candidate, honestly -- do not be generous.
A candidate profile and a job posting will be provided. Respond with ONLY a JSON object,
no markdown fences, no commentary, matching exactly this shape:

{
  "suitability_score": <integer 0-100>,
  "matched_signals": [<short strings, things from the job that match the candidate>],
  "gaps": [<short strings, things the job needs that the candidate lacks or is weak on>],
  "reasoning": "<2-3 sentence honest explanation of the score>"
}

Scoring guidance:
- 0-30: Clearly not a fit (wrong seniority, wrong stack, disqualifying signal present)
- 31-64: Possible but weak fit, meaningful gaps
- 65-84: Solid fit, matches most of what the candidate is targeting
- 85-100: Excellent fit, matches nearly everything the candidate is looking for
"""


def _client():
    api_key = os.environ.get('OPENAI_API_KEY')
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Set it as an environment variable "
            "(or a repo secret if running via GitHub Actions)."
        )
    return OpenAI(api_key=api_key)


def _model():
    return os.environ.get('OPENAI_MODEL') or 'gpt-4o-mini'


def screen_job(client, model, profile, title, company, location, job_description, retries=2):
    """Calls the model once (with one retry on a malformed response) and returns a dict:
    {suitability_score, matched_signals, gaps, reasoning} or None if scoring failed entirely.
    """
    user_prompt = (
        f"CANDIDATE PROFILE:\n{profile}\n\n"
        f"JOB POSTING:\n"
        f"Title: {title}\n"
        f"Company: {company}\n"
        f"Location: {location}\n"
        f"Description:\n{job_description or '(no description available)'}\n"
    )

    last_error = None
    for attempt in range(retries + 1):
        try:
            completion = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0,
            )
            raw = completion.choices[0].message.content.strip()
            # Strip accidental markdown code fences if the model adds them anyway.
            if raw.startswith('```'):
                raw = raw.strip('`')
                if raw.lower().startswith('json'):
                    raw = raw[4:].strip()
            result = json.loads(raw)
            score = int(result.get('suitability_score', 0))
            score = max(0, min(100, score))
            return {
                'suitability_score': score,
                'matched_signals': result.get('matched_signals', []),
                'gaps': result.get('gaps', []),
                'reasoning': result.get('reasoning', ''),
            }
        except Exception as e:
            last_error = e
            print(f"  scoring attempt {attempt + 1} failed: {e}")
            time.sleep(1)

    print(f"  giving up on this job after {retries + 1} attempts: {last_error}")
    return None


def ensure_ai_columns(conn):
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(jobs)")
    existing = [col[1] for col in cursor.fetchall()]

    additions = {
        'ai_score': 'INTEGER',
        'ai_matched_signals': 'TEXT',
        'ai_gaps': 'TEXT',
        'ai_reasoning': 'TEXT',
        'ai_screened': 'INTEGER DEFAULT 0',
        'notified': 'INTEGER DEFAULT 0',
    }
    for column, col_type in additions.items():
        if column not in existing:
            cursor.execute(f"ALTER TABLE jobs ADD COLUMN {column} {col_type}")
            print(f"  added column {column} to jobs table")
    conn.commit()


def screen_database(db_path, client, model, profile, threshold=SUITABILITY_THRESHOLD):
    """Screens every un-screened, non-hidden job in one .db file.
    Returns the list of jobs that newly qualify (score >= threshold) so the caller can
    notify on them. Each dict includes db_path + row id so a notifier can build a stable
    reference even though sqlite ids aren't unique across separate database files.
    """
    conn = sqlite3.connect(db_path)
    ensure_ai_columns(conn)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, title, company, location, job_url, job_description, date "
        "FROM jobs WHERE (ai_screened IS NULL OR ai_screened = 0) AND hidden = 0"
    )
    rows = cursor.fetchall()

    newly_qualifying = []
    print(f"{db_path}: {len(rows)} job(s) to screen")
    for row_id, title, company, location, job_url, job_description, posted_date in rows:
        print(f"  screening: {title} @ {company}")
        result = screen_job(client, model, profile, title, company, location, job_description)
        if result is None:
            # Leave ai_screened at 0 so a future run retries it, rather than silently
            # marking a failed call as "screened" and losing the job forever.
            continue

        cursor.execute(
            "UPDATE jobs SET ai_score = ?, ai_matched_signals = ?, ai_gaps = ?, "
            "ai_reasoning = ?, ai_screened = 1 WHERE id = ?",
            (
                result['suitability_score'],
                json.dumps(result['matched_signals']),
                json.dumps(result['gaps']),
                result['reasoning'],
                row_id,
            ),
        )
        conn.commit()

        if result['suitability_score'] >= threshold:
            newly_qualifying.append({
                'db_path': db_path,
                'row_id': row_id,
                'title': title,
                'company': company,
                'location': location,
                'job_url': job_url,
                'posted_date': posted_date,
                'score': result['suitability_score'],
                'matched_signals': result['matched_signals'],
                'gaps': result['gaps'],
                'reasoning': result['reasoning'],
            })

    conn.close()
    return newly_qualifying


def screen_all_databases(data_dir='data', threshold=SUITABILITY_THRESHOLD):
    client = _client()
    model = _model()
    profile = load_profile()

    all_qualifying = []
    for db_path in sorted(glob.glob(os.path.join(data_dir, '*.db'))):
        try:
            qualifying = screen_database(db_path, client, model, profile, threshold)
            all_qualifying.extend(qualifying)
        except Exception as e:
            print(f"ERROR screening {db_path}: {e}")
    return all_qualifying


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI-screen scraped jobs for suitability.")
    parser.add_argument('--db', help="Screen a single .db file instead of everything in --data-dir")
    parser.add_argument('--data-dir', default='data', help="Folder containing .db files (default: data)")
    parser.add_argument('--threshold', type=int, default=SUITABILITY_THRESHOLD,
                         help=f"Minimum score (0-100) to count as qualifying (default: {SUITABILITY_THRESHOLD})")
    args = parser.parse_args()

    if args.db:
        client = _client()
        model = _model()
        profile = load_profile()
        qualifying = screen_database(args.db, client, model, profile, args.threshold)
    else:
        qualifying = screen_all_databases(args.data_dir, args.threshold)

    print(f"\n{len(qualifying)} job(s) newly qualify at {args.threshold}%+:")
    for job in qualifying:
        print(f"  [{job['score']}%] {job['title']} @ {job['company']} ({job['db_path']})")
        
"""
Builds a static, self-contained HTML dashboard (docs/index.html) from every .db file in
data/, for GitHub Pages. Runs as the last step of the automated pipeline, after scraping
and screening -- so the published page always reflects the latest run.

GitHub Pages only serves static files, so this dashboard is VIEW-ONLY: there is no way
for it to write "applied" back into the database (that would need a live backend, which
Pages doesn't provide). The single source of truth for applied/hidden/rejected status is
still app.py running locally. The flow to get a local change onto the public dashboard:

    1. Run app.py locally, mark a job Applied/Rejected/Interview/Hidden as usual.
    2. git add data/*.db && git commit && git push   (pushes your change to the repo)
    3. On the NEXT scheduled pipeline run (or trigger one manually from the Actions tab),
       this script re-reads the updated .db files and regenerates docs/index.html with
       your change reflected -- the job moves from "New" into "Applied" on the public page.

There's no way around step 2 being manual without adding a live backend (see the earlier
conversation about GitHub Pages' static-only limitation) -- this script only takes care
of turning whatever's in the databases into a nice page, it doesn't change who owns
writing to them.

Usage:
    python generate_dashboard.py                  # reads data/*.db, writes docs/index.html
    python generate_dashboard.py --data-dir data --out docs/index.html
"""

import argparse
import glob
import json
import os
import sqlite3
from datetime import datetime, timezone


def load_all_jobs(data_dir):
    jobs = []
    base_columns = ['title', 'company', 'location', 'date', 'job_url', 'applied',
                     'interview', 'rejected', 'date_loaded']
    ai_columns = ['ai_score', 'ai_matched_signals', 'ai_gaps', 'ai_reasoning', 'ai_screened']

    for db_path in sorted(glob.glob(os.path.join(data_dir, '*.db'))):
        source = os.path.splitext(os.path.basename(db_path))[0]
        conn = sqlite3.connect(db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(jobs)")
            existing_columns = {c[1] for c in cursor.fetchall()}
            if 'title' not in existing_columns:
                print(f"Skipping {db_path}: no jobs table")
                continue

            # Only select ai_* columns that actually exist yet -- a db that hasn't been
            # through ai_screening.py's migration (e.g. right after a fresh scrape, before
            # the screening step of the pipeline has run) simply shows those jobs unscored
            # instead of being dropped from the dashboard entirely.
            select_columns = [c for c in base_columns if c in existing_columns]
            present_ai_columns = [c for c in ai_columns if c in existing_columns]
            all_columns = select_columns + present_ai_columns

            cursor.execute(f"SELECT {', '.join(all_columns)} FROM jobs WHERE hidden = 0")
            for row in cursor.fetchall():
                job = dict(zip(all_columns, row))
                for col in ai_columns:
                    job.setdefault(col, None)
                job['source'] = source
                for field in ('ai_matched_signals', 'ai_gaps'):
                    try:
                        job[field] = json.loads(job[field] or '[]')
                    except (json.JSONDecodeError, TypeError):
                        job[field] = []
                jobs.append(job)
        except sqlite3.OperationalError as e:
            print(f"Skipping {db_path}: {e}")
        finally:
            conn.close()
    return jobs


def build_stats(jobs):
    applied = [j for j in jobs if j['applied']]
    screened = [j for j in jobs if j['ai_screened']]
    qualifying = [j for j in screened if (j['ai_score'] or 0) >= 65]
    avg_score = round(sum(j['ai_score'] or 0 for j in screened) / len(screened)) if screened else None
    return {
        'total': len(jobs),
        'screened': len(screened),
        'qualifying': len(qualifying),
        'applied': len(applied),
        'avg_score': avg_score,
    }


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>QA job pipeline</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0D1117;
    --panel: #161B22;
    --panel-hover: #1C2129;
    --border: #30363D;
    --text: #E6EDF3;
    --text-dim: #8B949E;
    --text-faint: #565D66;
    --pass: #3FB950;
    --pass-dim: #1B4721;
    --warn: #D29922;
    --warn-dim: #4D3B0F;
    --fail: #F85149;
    --fail-dim: #4C1614;
    --applied: #58A6FF;
    --applied-dim: #16324D;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'IBM Plex Sans', -apple-system, sans-serif;
    line-height: 1.5;
  }
  .mono { font-family: 'IBM Plex Mono', monospace; }
  header {
    border-bottom: 1px solid var(--border);
    padding: 24px 32px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 12px;
  }
  header h1 {
    font-size: 18px;
    font-weight: 600;
    margin: 0;
    letter-spacing: 0.02em;
  }
  header .build-time {
    font-size: 12px;
    color: var(--text-faint);
  }
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 1px;
    background: var(--border);
    border-bottom: 1px solid var(--border);
  }
  .stat {
    background: var(--panel);
    padding: 18px 20px;
  }
  .stat .value {
    font-family: 'IBM Plex Mono', monospace;
    font-size: 26px;
    font-weight: 600;
  }
  .stat .label {
    font-size: 12px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-top: 4px;
  }
  .stat.qualifying .value { color: var(--pass); }
  .stat.applied .value { color: var(--applied); }
  .controls {
    padding: 16px 32px;
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
    border-bottom: 1px solid var(--border);
  }
  .tabs { display: flex; gap: 6px; }
  .tab {
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text-dim);
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 13px;
    cursor: pointer;
    font-family: inherit;
  }
  .tab.active { color: var(--text); border-color: var(--text-dim); }
  .spacer { flex: 1; }
  select, input[type="search"] {
    background: var(--panel);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 7px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-family: inherit;
  }
  input[type="search"] { min-width: 200px; }
  main { padding: 20px 32px 60px; max-width: 980px; margin: 0 auto; }
  .job {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 16px 18px;
    margin-bottom: 10px;
    display: flex;
    gap: 16px;
    align-items: flex-start;
  }
  .job:hover { background: var(--panel-hover); }
  .badge {
    font-family: 'IBM Plex Mono', monospace;
    font-weight: 600;
    font-size: 13px;
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 48px;
    text-align: center;
    flex-shrink: 0;
  }
  .badge.pass { background: var(--pass-dim); color: var(--pass); }
  .badge.warn { background: var(--warn-dim); color: var(--warn); }
  .badge.fail { background: var(--fail-dim); color: var(--fail); }
  .badge.unscored { background: var(--border); color: var(--text-faint); }
  .job-body { flex: 1; min-width: 0; }
  .job-title-row { display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
  .job-title { font-size: 15px; font-weight: 500; }
  .job-title a { color: var(--text); text-decoration: none; }
  .job-title a:hover { color: var(--applied); text-decoration: underline; }
  .job-meta {
    font-size: 12px;
    color: var(--text-dim);
    margin-top: 3px;
    font-family: 'IBM Plex Mono', monospace;
  }
  .job-meta .source { color: var(--text-faint); }
  .checks { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
  .check {
    font-size: 11px;
    padding: 3px 8px;
    border-radius: 4px;
    font-family: 'IBM Plex Mono', monospace;
  }
  .check.matched { background: var(--pass-dim); color: var(--pass); }
  .check.gap { background: var(--fail-dim); color: var(--fail); }
  .reasoning {
    margin-top: 8px;
    font-size: 13px;
    color: var(--text-dim);
    font-style: italic;
  }
  .applied-pill {
    font-size: 11px;
    background: var(--applied-dim);
    color: var(--applied);
    padding: 3px 8px;
    border-radius: 4px;
    font-family: 'IBM Plex Mono', monospace;
    align-self: flex-start;
    flex-shrink: 0;
  }
  .empty {
    text-align: center;
    color: var(--text-faint);
    padding: 60px 20px;
    font-size: 14px;
  }
  @media (max-width: 640px) {
    header, .controls, main { padding-left: 16px; padding-right: 16px; }
    .job { flex-direction: column; }
  }
</style>
</head>
<body>
<header>
  <h1>QA job pipeline</h1>
  <div class="build-time mono">last run __BUILD_TIME__</div>
</header>

<div class="stats">
  <div class="stat"><div class="value mono">__STAT_TOTAL__</div><div class="label">tracked</div></div>
  <div class="stat"><div class="value mono">__STAT_SCREENED__</div><div class="label">screened</div></div>
  <div class="stat qualifying"><div class="value mono">__STAT_QUALIFYING__</div><div class="label">65%+ match</div></div>
  <div class="stat applied"><div class="value mono">__STAT_APPLIED__</div><div class="label">applied</div></div>
  <div class="stat"><div class="value mono">__STAT_AVG__</div><div class="label">avg score</div></div>
</div>

<div class="controls">
  <div class="tabs">
    <button class="tab active" data-tab="new">New</button>
    <button class="tab" data-tab="applied">Applied</button>
    <button class="tab" data-tab="all">All</button>
  </div>
  <div class="spacer"></div>
  <input type="search" id="search" placeholder="Search title or company">
  <select id="sort">
    <option value="date">Sort: newest posted</option>
    <option value="score">Sort: highest score</option>
    <option value="scraped">Sort: recently scraped</option>
  </select>
  <select id="minscore">
    <option value="0">All scores</option>
    <option value="65" selected>65%+ only</option>
    <option value="85">85%+ only</option>
  </select>
</div>

<main id="job-list"></main>

<script id="jobs-data" type="application/json">__JOBS_JSON__</script>
<script>
const jobs = JSON.parse(document.getElementById('jobs-data').textContent);
let activeTab = 'new';

function scoreClass(score) {
  if (score === null || score === undefined) return 'unscored';
  if (score >= 85) return 'pass';
  if (score >= 65) return 'warn';
  return 'fail';
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function render() {
  const search = document.getElementById('search').value.toLowerCase();
  const sortBy = document.getElementById('sort').value;
  const minScore = parseInt(document.getElementById('minscore').value, 10);

  let filtered = jobs.filter(j => {
    if (activeTab === 'new' && j.applied) return false;
    if (activeTab === 'applied' && !j.applied) return false;
    if ((j.ai_score || 0) < minScore && !j.applied) return false;
    if (search && !((j.title || '').toLowerCase().includes(search) || (j.company || '').toLowerCase().includes(search))) return false;
    return true;
  });

  filtered.sort((a, b) => {
    if (sortBy === 'score') return (b.ai_score || 0) - (a.ai_score || 0);
    if (sortBy === 'scraped') return (b.date_loaded || '').localeCompare(a.date_loaded || '');
    return (b.date || '').localeCompare(a.date || '');
  });

  const list = document.getElementById('job-list');
  if (filtered.length === 0) {
    list.innerHTML = '<div class="empty">Nothing here yet. Check back after the next scheduled run.</div>';
    return;
  }

  list.innerHTML = filtered.map(j => {
    const cls = scoreClass(j.ai_score);
    const scoreLabel = (j.ai_score === null || j.ai_score === undefined) ? '--' : j.ai_score + '%';
    const matched = (j.ai_matched_signals || []).map(s => `<span class="check matched">${escapeHtml(s)}</span>`).join('');
    const gaps = (j.ai_gaps || []).map(s => `<span class="check gap">${escapeHtml(s)}</span>`).join('');
    const appliedPill = j.applied ? '<div class="applied-pill">APPLIED</div>' : '';
    return `
      <div class="job">
        <div class="badge ${cls}">${scoreLabel}</div>
        <div class="job-body">
          <div class="job-title-row">
            <div class="job-title"><a href="${j.job_url}" target="_blank" rel="noopener">${escapeHtml(j.title)}</a></div>
            ${appliedPill}
          </div>
          <div class="job-meta">${escapeHtml(j.company)} &middot; ${escapeHtml(j.location)} &middot; posted ${escapeHtml(j.date || 'unknown')} &middot; <span class="source">${escapeHtml(j.source)}</span></div>
          ${(matched || gaps) ? `<div class="checks">${matched}${gaps}</div>` : ''}
          ${j.ai_reasoning ? `<div class="reasoning">${escapeHtml(j.ai_reasoning)}</div>` : ''}
        </div>
      </div>
    `;
  }).join('');
}

document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    activeTab = tab.dataset.tab;
    render();
  });
});
document.getElementById('search').addEventListener('input', render);
document.getElementById('sort').addEventListener('change', render);
document.getElementById('minscore').addEventListener('change', render);

render();
</script>
</body>
</html>
"""


def render_dashboard(jobs, stats):
    build_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    html = TEMPLATE
    html = html.replace('__BUILD_TIME__', build_time)
    html = html.replace('__STAT_TOTAL__', str(stats['total']))
    html = html.replace('__STAT_SCREENED__', str(stats['screened']))
    html = html.replace('__STAT_QUALIFYING__', str(stats['qualifying']))
    html = html.replace('__STAT_APPLIED__', str(stats['applied']))
    html = html.replace('__STAT_AVG__', f"{stats['avg_score']}%" if stats['avg_score'] is not None else '--')
    html = html.replace('__JOBS_JSON__', json.dumps(jobs))
    return html


def main():
    parser = argparse.ArgumentParser(description="Generate the static job dashboard for GitHub Pages.")
    parser.add_argument('--data-dir', default='data')
    parser.add_argument('--out', default='docs/index.html')
    args = parser.parse_args()

    jobs = load_all_jobs(args.data_dir)
    stats = build_stats(jobs)
    html = render_dashboard(jobs, stats)

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w') as f:
        f.write(html)

    print(f"Wrote {args.out}: {stats['total']} jobs tracked, {stats['qualifying']} qualifying, {stats['applied']} applied")


if __name__ == "__main__":
    main()

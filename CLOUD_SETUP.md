# Cloud Automation Setup (GitHub Actions)

This adds a scheduled pipeline that runs every 4 hours: scrape all configs -> AI-screen
every new job against your profile -> email you when something scores 65%+.

## Files involved
- `ai_screening.py` - scores each job 0-100 against your profile using the OpenAI API,
  writes the score/reasoning back into the job's row (adds `ai_score`, `ai_matched_signals`,
  `ai_gaps`, `ai_reasoning`, `ai_screened`, `notified` columns to the `jobs` table automatically).
- `notify.py` - emails you a summary of newly-qualifying jobs, then marks them `notified=1`
  so you never get the same job twice.
- `pipeline.py` - runs scrape -> screen -> notify in sequence. This is the single entry
  point the scheduled job calls.
- `generate_dashboard.py` - builds the static `docs/index.html` GitHub Pages dashboard from
  every `.db` file in `data/`, run automatically as the last pipeline step.
- `resolve_geoids.py` - one-off/occasional tool that looks up a numeric LinkedIn geoId for
  each config location, for more reliable location matching than free-text search. Not part
  of the scheduled pipeline -- run it manually when you add new cities.
- `.github/workflows/scrape-screen-notify.yml` - the GitHub Actions schedule (every 4 hours)
  that runs `pipeline.py` in the cloud and commits the updated `.db` files + dashboard back
  to the repo.

## One-time setup

### 1. Push this project to a GitHub repo
**Use a private repo.** The `data/*.db` files will contain scraped job postings and your
applied/rejected/interview history -- not secret, but not something to make public either.

### 2. Add repository secrets
Go to your repo -> **Settings -> Secrets and variables -> Actions -> New repository secret**,
and add:

| Secret name | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | yes | Your OpenAI API key |
| `SMTP_USER` | yes | The email address that will *send* the notification |
| `SMTP_PASS` | yes | An **app password**, not your normal email password (see below) |
| `OPENAI_MODEL` | no | Defaults to `gpt-4o-mini` if not set |
| `SUITABILITY_THRESHOLD` | no | Defaults to `65` |
| `SMTP_HOST` | no | Defaults to `smtp.gmail.com` |
| `SMTP_PORT` | no | Defaults to `587` |
| `NOTIFY_EMAIL_TO` | no | Defaults to `SMTP_USER` (i.e. you email yourself) |

**Getting a Gmail app password** (if using Gmail to send): Google Account -> Security ->
2-Step Verification (must be on) -> App passwords -> generate one for "Mail". Use that
16-character password as `SMTP_PASS`, not your real Gmail password.

### 3. Allow the workflow to push back to the repo
Repo -> **Settings -> Actions -> General -> Workflow permissions** -> select
**"Read and write permissions"** -> Save. Without this, the workflow can scrape and email
fine, but can't save the updated `.db` files back to the repo, so every run would look like
the first run again (re-notifying about the same jobs).

### 4. Enable the GitHub Pages dashboard
Repo -> **Settings -> Pages** -> Source: **Deploy from a branch** -> Branch: **main**, folder **/docs** -> Save.
Your dashboard will be live at `https://<your-username>.github.io/<repo-name>/` within a
minute or two. Every pipeline run regenerates `docs/index.html` from the latest scraped +
screened data and commits it back, so the page always reflects the last run. It's
**view-only** -- GitHub Pages can't run a backend, so marking a job "Applied" still has to
happen locally in `app.py`; see "Keeping the dashboard's Applied status in sync" below.

### 5. That's it
The workflow runs automatically every 4 hours (`0 */4 * * *`, UTC). You can also trigger it
manually anytime from the repo's **Actions** tab -> "Scrape, Screen & Notify" -> **Run workflow**.

## Keeping the dashboard's Applied status in sync

GitHub Pages is static -- there's no way for a button on the public dashboard to write back
into the database. The database is only ever changed by `app.py` running locally. To get a
change you make locally onto the public dashboard:

1. Run `app.py` locally, mark a job Applied/Rejected/Interview/Hidden as usual.
2. `git add data/*.db && git commit -m "update job status" && git push`
3. Wait for the next scheduled run (within 4 hours) or trigger one manually from the
   Actions tab. It regenerates `docs/index.html` from your updated database, and the job
   moves from the "New" tab to the "Applied" tab on the public page.

There's no way around step 2 being manual without a live backend behind the dashboard --
that's the tradeoff of the free, zero-infrastructure setup.

## Important caveats (read before relying on this)

1. **LinkedIn may block/rate-limit GitHub's IPs more than your home IP.** GitHub-hosted
   runners use shared Azure datacenter IP ranges, which LinkedIn is more likely to flag than
   a residential IP. If runs start consistently returning 0 jobs where they used to return
   plenty, check the Actions logs for repeated "Non-200 status" warnings from `main.py` --
   that's this issue, not a bug in the code. A paid residential/rotating proxy service would
   reduce this risk if it becomes a real problem, at the cost of a subscription.

2. **The database is stored by committing it to git on every run.** This is the simplest
   way to persist state on a free, zero-maintenance setup, but it means your repo's git
   history grows by a small amount every 4 hours indefinitely. This is fine for a long
   while, but if it ever becomes a problem, the fix is to migrate `data/*.db` to a small
   hosted database (e.g. a free-tier Postgres on Supabase or Railway) instead of SQLite-in-git
   -- a bigger change, not needed to get started.

3. **Screening costs a small amount per job.** Each new job costs one OpenAI API call
   (roughly a few hundred tokens with `gpt-4o-mini`, well under a cent per job). With ~13
   configs scraping every 4 hours, expect this to add up to some real but modest OpenAI
   usage over a month -- worth keeping an eye on your OpenAI usage dashboard the first
   week to get a feel for the actual cost.

4. **The Flask app's manual resume/cover-letter buttons still need your resume PDF locally**
   -- that part isn't part of the cloud pipeline and still only works when you run `app.py`
   on your own machine with `resume_path` pointing at a real file.

## Running the pipeline manually (without GitHub Actions)
Useful for testing before you rely on the schedule:

```bash
export OPENAI_API_KEY="sk-..."
export SMTP_USER="you@gmail.com"
export SMTP_PASS="your-16-char-app-password"
python pipeline.py
```

Or run just one stage at a time:
```bash
python run_all_configs.py          # scrape only
python ai_screening.py             # screen only (reads OPENAI_API_KEY from env)
python -c "import notify, ai_screening; notify.send_qualifying_jobs_email(ai_screening.screen_all_databases())"
```

"""
Sends an email listing newly-qualifying jobs (from ai_screening.py) and marks them as
notified in their source database, so the same job never gets emailed twice.

Configuration is entirely via environment variables (so this works the same locally
and in a GitHub Actions secret-based run):
    SMTP_HOST      default: smtp.gmail.com
    SMTP_PORT      default: 587
    SMTP_USER      the sending email address (required)
    SMTP_PASS      the sending account's app password (required) -- for Gmail this must
                   be a 16-character App Password, not your normal login password
    NOTIFY_EMAIL_TO  who receives the notification (defaults to SMTP_USER, i.e. you email yourself)
"""

import os
import smtplib
import sqlite3
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def _smtp_config():
    user = os.environ.get('SMTP_USER')
    password = os.environ.get('SMTP_PASS')
    if not user or not password:
        raise RuntimeError("SMTP_USER and SMTP_PASS must be set to send notification emails.")
    return {
        'host': os.environ.get('SMTP_HOST', 'smtp.gmail.com'),
        'port': int(os.environ.get('SMTP_PORT', '587')),
        'user': user,
        'password': password,
        'to': os.environ.get('NOTIFY_EMAIL_TO', user),
    }


def _build_email(qualifying_jobs):
    jobs_sorted = sorted(qualifying_jobs, key=lambda j: j['score'], reverse=True)

    lines_html = []
    lines_text = []
    for job in jobs_sorted:
        matched = ", ".join(job.get('matched_signals') or []) or "-"
        gaps = ", ".join(job.get('gaps') or []) or "-"
        posted = job.get('posted_date') or "date unknown"
        lines_html.append(f"""
            <div style="margin-bottom:18px;padding:12px;border:1px solid #ddd;border-radius:6px;">
                <div style="font-size:16px;font-weight:bold;">{job['title']} — {job['score']}% match</div>
                <div style="color:#555;">{job['company']} · {job['location']} · posted {posted}</div>
                <div style="margin-top:6px;"><b>Matched:</b> {matched}</div>
                <div><b>Gaps:</b> {gaps}</div>
                <div style="margin-top:6px;font-style:italic;color:#555;">{job.get('reasoning', '')}</div>
                <div style="margin-top:8px;"><a href="{job['job_url']}">View posting →</a></div>
            </div>
        """)
        lines_text.append(
            f"[{job['score']}%] {job['title']} @ {job['company']} ({job['location']}) · posted {posted}\n"
            f"  Matched: {matched}\n  Gaps: {gaps}\n  {job.get('reasoning', '')}\n  {job['job_url']}\n"
        )

    html = f"""
    <html><body>
        <h2>{len(jobs_sorted)} new job(s) match your profile</h2>
        {''.join(lines_html)}
    </body></html>
    """
    text = f"{len(jobs_sorted)} new job(s) match your profile:\n\n" + "\n".join(lines_text)
    return html, text


def send_qualifying_jobs_email(qualifying_jobs):
    if not qualifying_jobs:
        print("No qualifying jobs to notify about -- skipping email.")
        return False

    config = _smtp_config()
    html, text = _build_email(qualifying_jobs)

    msg = MIMEMultipart('alternative')
    msg['Subject'] = f"{len(qualifying_jobs)} new QA job match(es) found"
    msg['From'] = config['user']
    msg['To'] = config['to']
    msg.attach(MIMEText(text, 'plain'))
    msg.attach(MIMEText(html, 'html'))

    with smtplib.SMTP(config['host'], config['port']) as server:
        server.starttls()
        server.login(config['user'], config['password'])
        server.sendmail(config['user'], [config['to']], msg.as_string())

    print(f"Sent notification email for {len(qualifying_jobs)} job(s) to {config['to']}")
    _mark_notified(qualifying_jobs)
    return True


def _mark_notified(qualifying_jobs):
    # Group by source db so we open each file once, not once per job.
    by_db = {}
    for job in qualifying_jobs:
        by_db.setdefault(job['db_path'], []).append(job['row_id'])

    for db_path, row_ids in by_db.items():
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.executemany("UPDATE jobs SET notified = 1 WHERE id = ?", [(rid,) for rid in row_ids])
        conn.commit()
        conn.close()

from flask import Flask, render_template, jsonify
import pandas as pd
import sqlite3
import json
import glob
import os
from openai import OpenAI
from pdfminer.high_level import extract_text
from flask_cors import CORS

def load_config(file_name):
    # Load the config file
    with open(file_name) as f:
        return json.load(f)

config = load_config('config.json')
app = Flask(__name__)
CORS(app)
app.config['TEMPLATES_AUTO_RELOAD'] = True

# Folder containing every scraper database (my_database.db, calgary.db, ottawa.db, etc).
# The web UI combines ALL .db files found here into a single job list, instead of only
# reading whatever single db_path happens to be in config.json.
DATA_DIR = 'data'

# Composite job ids look like "<db-name-without-extension>::<row-id>", e.g. "calgary::42".
# This lets every route (view/hide/apply/etc) know which specific database a job lives in,
# since row ids are only unique *within* a single db, not across all of them combined.
ID_SEPARATOR = '::'

def get_db_paths():
    # Every .db file in the data folder, discovered fresh on each call so newly created
    # databases (e.g. after running main.py with a new city config) show up automatically
    # without restarting the app.
    return sorted(glob.glob(os.path.join(DATA_DIR, '*.db')))

def db_label(db_path):
    # Human-friendly source label, e.g. "data/calgary.db" -> "calgary"
    return os.path.splitext(os.path.basename(db_path))[0]

def make_composite_id(db_path, row_id):
    return f"{db_label(db_path)}{ID_SEPARATOR}{row_id}"

def resolve_composite_id(job_id):
    # Turns "calgary::42" back into ('data/calgary.db', 42). Raises ValueError if the
    # source database in the id no longer exists on disk or the id isn't well-formed.
    if ID_SEPARATOR not in job_id:
        raise ValueError(f"Malformed job id (missing separator): {job_id}")
    label, row_id = job_id.rsplit(ID_SEPARATOR, 1)
    db_path = os.path.join(DATA_DIR, f"{label}.db")
    if not os.path.exists(db_path):
        raise ValueError(f"No database found for job id: {job_id}")
    return db_path, int(row_id)

def read_pdf(file_path):
    try:
        text = extract_text(file_path)
        return text
    except FileNotFoundError:
        print(f"Error: The file '{file_path}' was not found.")
        return None
    except Exception as e:
        print(f"An error occurred while reading the PDF: {e}")
        return None

@app.route('/')
def home():
    jobs = read_jobs_from_db()
    return render_template('jobs.html', jobs=jobs)

@app.route('/get_all_jobs')
def get_all_jobs():
    all_jobs = []
    for db_path in get_db_paths():
        conn = sqlite3.connect(db_path)
        try:
            df = pd.read_sql_query("SELECT * FROM jobs", conn)
        except Exception as e:
            print(f"Skipping {db_path}: {e}")
            continue
        finally:
            conn.close()
        df['id'] = df['id'].apply(lambda row_id: make_composite_id(db_path, row_id))
        df['source'] = db_label(db_path)
        all_jobs.append(df)

    if not all_jobs:
        return jsonify([])

    combined = pd.concat(all_jobs, ignore_index=True)
    combined = combined.sort_values(by='date_loaded', ascending=False, na_position='last')
    combined.reset_index(drop=True, inplace=True)
    return jsonify(combined.to_dict('records'))

@app.route('/job_details/<job_id>')
def job_details(job_id):
    try:
        db_path, row_id = resolve_composite_id(job_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs WHERE id = ?", (row_id,))
    job_tuple = cursor.fetchone()
    column_names = [column[0] for column in cursor.description]
    conn.close()
    if job_tuple is not None:
        job = dict(zip(column_names, job_tuple))
        job['id'] = job_id  # expose the composite id, not the raw per-db row id
        job['source'] = db_label(db_path)
        return jsonify(job)
    else:
        return jsonify({"error": "Job not found"}), 404

@app.route('/hide_job/<job_id>', methods=['POST'])
def hide_job(job_id):
    return _update_job_flag(job_id, "hidden")

@app.route('/mark_applied/<job_id>', methods=['POST'])
def mark_applied(job_id):
    return _update_job_flag(job_id, "applied")

@app.route('/mark_interview/<job_id>', methods=['POST'])
def mark_interview(job_id):
    return _update_job_flag(job_id, "interview")

@app.route('/mark_rejected/<job_id>', methods=['POST'])
def mark_rejected(job_id):
    return _update_job_flag(job_id, "rejected")

def _update_job_flag(job_id, column_name):
    try:
        db_path, row_id = resolve_composite_id(job_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    query = f"UPDATE jobs SET {column_name} = 1 WHERE id = ?"
    print(f'Executing query: {query} with job_id: {job_id} (db: {db_path}, row: {row_id})')
    cursor.execute(query, (row_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": f"Job marked as {column_name}"}), 200

@app.route('/get_cover_letter/<job_id>')
def get_cover_letter(job_id):
    try:
        db_path, row_id = resolve_composite_id(job_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT cover_letter FROM jobs WHERE id = ?", (row_id,))
    cover_letter = cursor.fetchone()
    conn.close()
    if cover_letter is not None:
        return jsonify({"cover_letter": cover_letter[0]})
    else:
        return jsonify({"error": "Cover letter not found"}), 404

@app.route('/get_resume/<job_id>', methods=['POST'])
def get_resume(job_id):
    print("Resume clicked!")
    try:
        db_path, row_id = resolve_composite_id(job_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT job_description, title, company FROM jobs WHERE id = ?", (row_id,))
    job_tuple = cursor.fetchone()
    if job_tuple is not None:
        column_names = [column[0] for column in cursor.description]
        job = dict(zip(column_names, job_tuple))
    else:
        conn.close()
        return jsonify({"error": "Job not found"}), 404

    resume = read_pdf(config["resume_path"])
    if resume is None:
        conn.close()
        return jsonify({"error": "Resume not found or couldn't be read."}), 400

    if not config["OpenAI_API_KEY"]:
        print("Error: OpenAI API key is empty.")
        conn.close()
        return jsonify({"error": "OpenAI API key is empty."}), 400

    openai_client = OpenAI(api_key=config["OpenAI_API_KEY"])
    consideration = ""
    user_prompt = ("You are a career coach with a client that is applying for a job as a "
                   + job['title'] + " at " + job['company']
                   + ". They have a resume that you need to review and suggest how to tailor it for the job. "
                   "Approach this task in the following steps: \n 1. Highlight three to five most important responsibilities for this role based on the job description. "
                   "\n2. Based on these most important responsibilities from the job description, please tailor the resume for this role. Do not make information up. "
                   "Respond with the final resume only. \n\n Here is the job description: "
                   + job['job_description'] + "\n\n Here is the resume: " + resume)
    if consideration:
        user_prompt += "\nConsider incorporating that " + consideration

    try:
        completion = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
        response = completion.choices[0].message.content
    except Exception as e:
        print(f"Error connecting to OpenAI: {e}")
        conn.close()
        return jsonify({"error": f"Error connecting to OpenAI: {e}"}), 500

    query = "UPDATE jobs SET resume = ? WHERE id = ?"
    print(f'Executing query: {query} with job_id: {job_id} and resume: {response}')
    cursor.execute(query, (response, row_id))
    conn.commit()
    conn.close()
    return jsonify({"resume": response}), 200

@app.route('/get_CoverLetter/<job_id>', methods=['POST'])
def get_CoverLetter(job_id):
    print("CoverLetter clicked!")
    try:
        db_path, row_id = resolve_composite_id(job_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    openai_client = OpenAI(api_key=config["OpenAI_API_KEY"])
    consideration = ""

    def get_chat_gpt(prompt):
        try:
            completion = openai_client.chat.completions.create(
                model=config["OpenAI_Model"],
                messages=[
                    {"role": "user", "content": prompt},
                ],
            )
            return completion.choices[0].message.content
        except Exception as e:
            print(f"Error connecting to OpenAI: {e}")
            return None

    cursor.execute("SELECT job_description, title, company FROM jobs WHERE id = ?", (row_id,))
    job_tuple = cursor.fetchone()
    if job_tuple is not None:
        column_names = [column[0] for column in cursor.description]
        job = dict(zip(column_names, job_tuple))
    else:
        conn.close()
        return jsonify({"error": "Job not found"}), 404

    resume = read_pdf(config["resume_path"])

    if resume is None:
        print("Error: Resume not found or couldn't be read.")
        conn.close()
        return jsonify({"error": "Resume not found or couldn't be read."}), 400

    if not config["OpenAI_API_KEY"]:
        print("Error: OpenAI API key is empty.")
        conn.close()
        return jsonify({"error": "OpenAI API key is empty."}), 400

    user_prompt = ("You are a career coach with over 15 years of experience helping job seekers land their dream jobs in tech. You are helping a candidate to write a cover letter for the below role. Approach this task in three steps. Step 1. Identify main challenges someone in this position would face day to day. Step 2. Write an attention grabbing hook for your cover letter that highlights your experience and qualifications in a way that shows you empathize and can successfully take on challenges of the role. Consider incorporating specific examples of how you tackled these challenges in your past work, and explore creative ways to express your enthusiasm for the opportunity. Put emphasis on how the candidate can contribute to company as opposed to just listing accomplishments. Keep your hook within 100 words or less. Step 3. Finish writing the cover letter based on the resume and keep it within 250 words. Respond with final cover letter only. \n job description: " + job['job_description'] + "\n company: " + job['company'] + "\n title: " + job['title'] + "\n resume: " + resume)
    if consideration:
        user_prompt += "\nConsider incorporating that " + consideration

    response = get_chat_gpt(user_prompt)
    if response is None:
        conn.close()
        return jsonify({"error": "Failed to get a response from OpenAI."}), 500

    user_prompt2 = ("You are young but experienced career coach helping job seekers land their dream jobs in tech. I need your help crafting a cover letter. Here is a job description: " + job['job_description'] + "\nhere is my resume: " + resume + "\nHere's the cover letter I got so far: " + response + "\nI need you to help me improve it. Let's approach this in following steps. \nStep 1. Please set the formality scale as follows: 1 is conversational English, my initial Cover letter draft is 10. Step 2. Identify three to five ways this cover letter can be improved, and elaborate on each way with at least one thoughtful sentence. Step 4. Suggest an improved cover letter based on these suggestions with the Formality Score set to 7. Avoid subjective qualifiers such as drastic, transformational, etc. Keep the final cover letter within 250 words. Please respond with the final cover letter only.")
    if user_prompt2:
        response = get_chat_gpt(user_prompt2)
        if response is None:
            conn.close()
            return jsonify({"error": "Failed to get a response from OpenAI."}), 500

    query = "UPDATE jobs SET cover_letter = ? WHERE id = ?"
    print(f'Executing query: {query} with job_id: {job_id} and cover letter: {response}')
    cursor.execute(query, (response, row_id))
    conn.commit()
    conn.close()
    return jsonify({"cover_letter": response}), 200

def read_jobs_from_db():
    # Pull non-hidden jobs from every .db file in the data folder and merge them into
    # one list, tagging each with a composite id (so hide/apply/etc routes know which
    # database to write back to) and a 'source' label (which city/config produced it).
    all_dfs = []
    for db_path in get_db_paths():
        conn = sqlite3.connect(db_path)
        try:
            df = pd.read_sql_query("SELECT * FROM jobs WHERE hidden = 0", conn)
        except Exception as e:
            print(f"Skipping {db_path}: {e}")
            continue
        finally:
            conn.close()
        if df.empty:
            continue
        df['id'] = df['id'].apply(lambda row_id: make_composite_id(db_path, row_id))
        df['source'] = db_label(db_path)
        all_dfs.append(df)

    if not all_dfs:
        return []

    combined = pd.concat(all_dfs, ignore_index=True)
    # Newest-loaded jobs first, across all cities combined.
    combined = combined.sort_values(by='date_loaded', ascending=False, na_position='last')
    return combined.to_dict('records')

def verify_db_schema():
    # Run the schema check against every database in the data folder, not just one,
    # so older per-city .db files that predate the cover_letter/resume columns still work.
    for db_path in get_db_paths():
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        cursor.execute("PRAGMA table_info(jobs)")
        table_info = cursor.fetchall()
        existing_columns = [column[1] for column in table_info]

        if "cover_letter" not in existing_columns:
            cursor.execute("ALTER TABLE jobs ADD COLUMN cover_letter TEXT")
            print(f"Added cover_letter column to jobs table in {db_path}")

        if "resume" not in existing_columns:
            cursor.execute("ALTER TABLE jobs ADD COLUMN resume TEXT")
            print(f"Added resume column to jobs table in {db_path}")

        conn.commit()
        conn.close()

if __name__ == "__main__":
    verify_db_schema()  # Verify the DB schema before running the app
    app.run(debug=True, port=5001)

import requests
import json
import sqlite3
import sys
from sqlite3 import Error
from bs4 import BeautifulSoup
import time as tm
from datetime import datetime, timedelta, time
import pandas as pd
from urllib.parse import quote
from langdetect import detect
from langdetect.lang_detect_exception import LangDetectException


def load_config(file_name):
    # Load the config file
    with open(file_name) as f:
        return json.load(f)

def get_with_retry(url, config, retries=3, delay=1):
    # Get the URL with retries and delay
    for i in range(retries):
        try:
            if len(config['proxies']) > 0:
                r = requests.get(url, headers=config['headers'], proxies=config['proxies'], timeout=5)
            else:
                r = requests.get(url, headers=config['headers'], timeout=5)
            # A non-200 status (e.g. 999, 429, CAPTCHA redirect) is a sign we're being
            # blocked/rate-limited by LinkedIn. Don't silently treat it as "no jobs" --
            # retry it like a timeout instead of returning a soup of junk HTML.
            if r.status_code != 200:
                print(f"Non-200 status ({r.status_code}) for URL: {url}, retrying in {delay}s...")
                tm.sleep(delay)
                continue
            return BeautifulSoup(r.content, 'html.parser')
        except requests.exceptions.Timeout:
            print(f"Timeout occurred for URL: {url}, retrying in {delay}s...")
            tm.sleep(delay)
        except Exception as e:
            print(f"An error occurred while retrieving the URL: {url}, error: {e}")
    print(f"WARNING: Giving up on URL after {retries} attempts (likely blocked/rate-limited): {url}")
    return None

def transform(soup):
    # Parsing the job card info (title, company, location, date, job_url) from the beautiful soup object
    joblist = []
    try:
        divs = soup.find_all('div', class_='base-search-card__info')
    except:
        print("Empty page, no jobs found")
        return joblist
    for item in divs:
        title = item.find('h3').text.strip()
        company = item.find('a', class_='hidden-nested-link')
        location = item.find('span', class_='job-search-card__location')
        parent_div = item.parent
        entity_urn = parent_div['data-entity-urn']
        job_posting_id = entity_urn.split(':')[-1]
        job_url = 'https://www.linkedin.com/jobs/view/'+job_posting_id+'/'

        date_tag_new = item.find('time', class_ = 'job-search-card__listdate--new')
        date_tag = item.find('time', class_='job-search-card__listdate')
        date = date_tag['datetime'] if date_tag else date_tag_new['datetime'] if date_tag_new else ''
        job_description = ''
        job = {
            'title': title,
            'company': company.text.strip().replace('\n', ' ') if company else '',
            'location': location.text.strip() if location else '',
            'date': date,
            'job_url': job_url,
            'job_description': job_description,
            'applied': 0,
            'hidden': 0,
            'interview': 0,
            'rejected': 0
        }
        joblist.append(job)
    return joblist

def transform_job(soup):
    div = soup.find('div', class_='description__text description__text--rich')
    if div:
        # Remove unwanted elements
        for element in div.find_all(['span', 'a']):
            element.decompose()

        # Replace bullet points
        for ul in div.find_all('ul'):
            for li in ul.find_all('li'):
                li.insert(0, '-')

        text = div.get_text(separator='\n').strip()
        text = text.replace('\n\n', '')
        text = text.replace('::marker', '-')
        text = text.replace('-\n', '- ')
        text = text.replace('Show less', '').replace('Show more', '')
        return text
    else:
        return "Could not find Job Description"

def safe_detect(text):
    try:
        return detect(text)
    except LangDetectException:
        return 'en'

def remove_irrelevant_jobs(joblist, config):
    #Filter out jobs based on description, title, and language. Set up in config.json.
    new_joblist = [job for job in joblist if not any(word.lower() in job['job_description'].lower() for word in config['desc_words'])]   
    new_joblist = [job for job in new_joblist if not any(word.lower() in job['title'].lower() for word in config['title_exclude'])] if len(config['title_exclude']) > 0 else new_joblist
    new_joblist = [job for job in new_joblist if any(word.lower() in job['title'].lower() for word in config['title_include'])] if len(config['title_include']) > 0 else new_joblist
    new_joblist = [job for job in new_joblist if safe_detect(job['job_description']) in config['languages']] if len(config['languages']) > 0 else new_joblist
    new_joblist = [job for job in new_joblist if not any(word.lower() in job['company'].lower() for word in config['company_exclude'])] if len(config['company_exclude']) > 0 else new_joblist

    return new_joblist

def remove_duplicates(joblist, config):
    # Remove duplicate jobs in the joblist. Duplicate is defined as having the same job_url.
    # (Previously this deduped on title+company, which collapsed two genuinely different
    # postings from the same company with the same title -- e.g. two separate "QA Automation
    # Engineer" reqs -- into one, silently dropping the second listing.)
    seen_urls = set()
    deduped = []
    for job in joblist:
        if job['job_url'] not in seen_urls:
            seen_urls.add(job['job_url'])
            deduped.append(job)
    return deduped

def convert_date_format(date_string):
    """
    Converts a date string to a date object. 
    
    Args:
        date_string (str): The date in string format.

    Returns:
        date: The converted date object, or None if conversion failed.
    """
    date_format = "%Y-%m-%d"
    try:
        job_date = datetime.strptime(date_string, date_format).date()
        return job_date
    except ValueError:
        print(f"Error: The date for job {date_string} - is not in the correct format.")
        return None

def create_connection(config):
    # Create a database connection to a SQLite database
    conn = None
    path = config['db_path']
    try:
        conn = sqlite3.connect(path) # creates a SQL database in the 'data' directory
        #print(sqlite3.version)
    except Error as e:
        print(e)

    return conn

def create_table(conn, df, table_name):
    ''''
    # Create a new table with the data from the dataframe
    df.to_sql(table_name, conn, if_exists='replace', index=False)
    print (f"Created the {table_name} table and added {len(df)} records")
    '''
    # Create a new table with the data from the DataFrame
    # Prepare data types mapping from pandas to SQLite
    type_mapping = {
    'int64': 'INTEGER',
    'float64': 'REAL',
    'datetime64[ns]': 'TIMESTAMP',
    'object': 'TEXT',
    'bool': 'INTEGER',
    'str': 'TEXT',
    'string': 'TEXT'
    }
    
    # Prepare a string with column names and their types
    columns_with_types = ', '.join(
        f'"{column}" {type_mapping[str(df.dtypes[column])]}'
        for column in df.columns
    )
    
    # Prepare SQL query to create a new table
    create_table_sql = f"""
        CREATE TABLE IF NOT EXISTS "{table_name}" (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            {columns_with_types}
        );
    """
    
    # Execute SQL query
    cursor = conn.cursor()
    cursor.execute(create_table_sql)
    
    # Commit the transaction
    conn.commit()

    # Insert DataFrame records one by one
    insert_sql = f"""
        INSERT INTO "{table_name}" ({', '.join(f'"{column}"' for column in df.columns)})
        VALUES ({', '.join(['?' for _ in df.columns])})
    """
    for record in df.to_dict(orient='records'):
        cursor.execute(insert_sql, list(record.values()))
    
    # Commit the transaction
    conn.commit()

    print(f"Created the {table_name} table and added {len(df)} records")

def update_table(conn, df, table_name):
    # Update the existing table with new records.
    df_existing = pd.read_sql(f'select * from {table_name}', conn)

    # Create a dataframe with unique records in df that are not in df_existing
    df_new_records = pd.concat([df, df_existing, df_existing]).drop_duplicates(['title', 'company', 'date'], keep=False)

    # If there are new records, append them to the existing table
    if len(df_new_records) > 0:
        df_new_records.to_sql(table_name, conn, if_exists='append', index=False)
        print (f"Added {len(df_new_records)} new records to the {table_name} table")
    else:
        print (f"No new records to add to the {table_name} table")

def table_exists(conn, table_name):
    # Check if the table already exists in the database
    cur = conn.cursor()
    cur.execute(f"SELECT count(name) FROM sqlite_master WHERE type='table' AND name='{table_name}'")
    if cur.fetchone()[0]==1 :
        return True
    return False

def job_exists(df, job):
    # Check if the job already exists in the dataframe
    if df.empty:
        return False
    #return ((df['title'] == job['title']) & (df['company'] == job['company']) & (df['date'] == job['date'])).any()
    #The job exists if there's already a job in the database that has the same URL
    return ((df['job_url'] == job['job_url']).any() | (((df['title'] == job['title']) & (df['company'] == job['company']) & (df['date'] == job['date'])).any()))

def get_jobcards(config):
    #Function to get the job cards from the search results page
    all_jobs = []
    request_delay = config.get('request_delay_seconds', 1)
    max_pages = config['pages_to_scrape']  # safety ceiling, not a fixed count -- see loop below
    for k in range(0, config['rounds']):
        for query in config['search_queries']:
            keywords = quote(query['keywords']) # URL encode the keywords
            location = quote(query['location']) # URL encode the location
            query_job_count = 0
            page = 0
            hit_ceiling = False
            while page < max_pages:
                # sortBy=DD sorts by date posted, newest first. Without this LinkedIn
                # defaults to a "most relevant" ordering, which can bury a brand-new
                # posting many pages deep -- past pages_to_scrape, it's never fetched at all,
                # not filtered out, just never requested. Sorting by date means anything we
                # truncate due to the page ceiling is guaranteed to be the OLDER results, not
                # the newest ones -- exactly the tradeoff we want if there's ever more volume
                # than the ceiling allows.
                # If resolve_geoids.py has resolved a numeric geoId for this location, use it --
                # it's a deterministic location match instead of LinkedIn's free-text guessing.
                # Falls back to blank (previous behavior) for any query that hasn't been resolved.
                geo_id = query.get('geoId', '')
                url = f"https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords={keywords}&location={location}&f_WT={query['f_WT']}&geoId={geo_id}&f_TPR={config['timespan']}&sortBy=DD&start={25*page}"
                soup = get_with_retry(url, config)
                jobs = transform(soup)
                page_count = len(jobs)
                query_job_count += page_count
                all_jobs = all_jobs + jobs
                print(f"Finished scraping page {page + 1} ({page_count} jobs): ", url)
                page += 1
                tm.sleep(request_delay)
                # A page with fewer than 25 jobs means we've reached the true end of
                # LinkedIn's results for this query -- stop early instead of wasting
                # requests on pages that don't exist (this is what actually fixes
                # low-volume queries like small cities that previously always scraped
                # the full pages_to_scrape count regardless of how few results existed).
                if page_count < 25:
                    break
                if page >= max_pages:
                    hit_ceiling = True
            # Only warn about truncation if we actually ran out of ceiling while pages
            # were still coming back full -- not just "this query had few results".
            if hit_ceiling:
                print(f"WARNING: '{query['keywords']}' in '{query['location']}' still returned a full page at the pages_to_scrape ceiling ({max_pages}) -- some older postings were likely never fetched. Consider raising pages_to_scrape for this config.")
    print ("Total job cards scraped: ", len(all_jobs))
    all_jobs = remove_duplicates(all_jobs, config)
    print ("Total job cards after removing duplicates: ", len(all_jobs))
    all_jobs = remove_irrelevant_jobs(all_jobs, config)
    print ("Total job cards after removing irrelevant jobs: ", len(all_jobs))
    return all_jobs

def find_new_jobs(all_jobs, conn, config):
    # From all_jobs, find the jobs that are not already in the database. Function checks both the jobs and filtered_jobs tables.
    jobs_tablename = config['jobs_tablename']
    filtered_jobs_tablename = config['filtered_jobs_tablename']
    jobs_db = pd.DataFrame()
    filtered_jobs_db = pd.DataFrame()    
    if conn is not None:
        if table_exists(conn, jobs_tablename):
            query = f"SELECT * FROM {jobs_tablename}"
            jobs_db = pd.read_sql_query(query, conn)
        if table_exists(conn, filtered_jobs_tablename):
            query = f"SELECT * FROM {filtered_jobs_tablename}"
            filtered_jobs_db = pd.read_sql_query(query, conn)

    new_joblist = [job for job in all_jobs if not job_exists(jobs_db, job) and not job_exists(filtered_jobs_db, job)]
    return new_joblist

def main(config_file):
    start_time = tm.perf_counter()
    job_list = []

    config = load_config(config_file)
    jobs_tablename = config['jobs_tablename'] # name of the table to store the "approved" jobs
    filtered_jobs_tablename = config['filtered_jobs_tablename'] # name of the table to store the jobs that have been filtered out based on description keywords (so that in future they are not scraped again)
    #Scrape search results page and get job cards. This step might take a while based on the number of pages and search queries.
    all_jobs = get_jobcards(config)
    conn = create_connection(config)
    #filtering out jobs that are already in the database
    all_jobs = find_new_jobs(all_jobs, conn, config)
    print ("Total new jobs found after comparing to the database: ", len(all_jobs))

    if len(all_jobs) > 0:

        for job in all_jobs:
            job_date = convert_date_format(job['date'])
            job_date = datetime.combine(job_date, time())
            #if job is older than a week, skip it
            if job_date < datetime.now() - timedelta(days=config['days_to_scrape']):
                continue
            print('Found new job: ', job['title'], 'at ', job['company'], job['job_url'])
            desc_soup = get_with_retry(job['job_url'], config)
            job['job_description'] = transform_job(desc_soup)
            language = safe_detect(job['job_description'])
            if language not in config['languages']:
                print('Job description language not supported: ', language)
                #continue
            job_list.append(job)
        #Final check - removing jobs based on job description keywords words from the config file
        jobs_to_add = remove_irrelevant_jobs(job_list, config)
        print ("Total jobs to add: ", len(jobs_to_add))
        #Create a list for jobs removed based on job description keywords - they will be added to the filtered_jobs table
        filtered_list = [job for job in job_list if job not in jobs_to_add]
        df = pd.DataFrame(jobs_to_add)
        df_filtered = pd.DataFrame(filtered_list)
        df['date_loaded'] = datetime.now()
        df_filtered['date_loaded'] = datetime.now()
        df['date_loaded'] = df['date_loaded'].astype(str)
        df_filtered['date_loaded'] = df_filtered['date_loaded'].astype(str)        
        
        if conn is not None:
            #Update or Create the database table for the job list
            if table_exists(conn, jobs_tablename):
                update_table(conn, df, jobs_tablename)
            else:
                create_table(conn, df, jobs_tablename)
                
            #Update or Create the database table for the filtered out jobs
            if table_exists(conn, filtered_jobs_tablename):
                update_table(conn, df_filtered, filtered_jobs_tablename)
            else:
                create_table(conn, df_filtered, filtered_jobs_tablename)
        else:
            print("Error! cannot create the database connection.")
        
        df.to_csv('linkedin_jobs.csv', index=False, encoding='utf-8')
        df_filtered.to_csv('linkedin_jobs_filtered.csv', index=False, encoding='utf-8')
    else:
        print("No jobs found")
    
    end_time = tm.perf_counter()
    print(f"Scraping finished in {end_time - start_time:.2f} seconds")


if __name__ == "__main__":
    config_file = 'config.json'  # default config file
    if len(sys.argv) == 2:
        config_file = sys.argv[1]
        
    main(config_file)
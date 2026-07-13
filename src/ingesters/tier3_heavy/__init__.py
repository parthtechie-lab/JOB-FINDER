"""
Tier 3: JS-Heavy Sites + LinkedIn IMAP Parser
- Playwright (stealth) for JS-heavy sites — launched via asyncio.subprocess
  to prevent memory leaks (each run is a fresh process)
- LinkedIn Zero-Legal-Risk: reads YOUR OWN Gmail inbox via IMAP
  Scrapes your personal email, NOT LinkedIn's servers
- Runs in isolated Celery heavy_queue (concurrency=1)
"""
import asyncio
import hashlib
import imaplib
import email
import re
import json
import tempfile
import os
from datetime import datetime
from email.header import decode_header
from typing import Optional

from src.config import get_settings
from src.exceptions import IngestionError
from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job, Source
from src.processors.dedup_engine import is_duplicate, mark_processed
from src.processors.ai_router import batch_process_jobs
from src.processors.scorer import calculate_score

log = get_logger("tier3_heavy")
settings = get_settings()


def _canonical_hash(title: str, company: str, location: str) -> str:
    raw = f"{title.lower()}|{company.lower()}|{location.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── LinkedIn IMAP (Legal) ────────────────────────────────────────────────────
def fetch_linkedin_from_imap(max_emails: int = 50) -> list[dict]:
    """
    Reads unseen LinkedIn job-alert emails from Gmail via IMAP.
    This is 100% legal — we're reading our own email.
    Gmail filter: label:jobs-alerts from:(jobalerts-noreply@linkedin.com)
    """
    if not settings.linkedin_email or not settings.linkedin_imap_password:
        log.warning("LinkedIn IMAP credentials not set, skipping")
        return []

    raw_jobs = []
    try:
        mail = imaplib.IMAP4_SSL(settings.linkedin_imap_server)
        mail.login(settings.linkedin_email, settings.linkedin_imap_password)
        mail.select("INBOX")

        # Search for unseen LinkedIn job alert emails
        _, data = mail.search(None, '(UNSEEN FROM "jobalerts-noreply@linkedin.com")')
        email_ids = data[0].split()
        log.info("LinkedIn IMAP emails found", count=len(email_ids))

        for eid in email_ids[-max_emails:]:  # process at most max_emails
            _, msg_data = mail.fetch(eid, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            # Extract HTML body
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
            else:
                body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")

            if not body:
                continue

            # Extract job links from LinkedIn email HTML
            job_links = re.findall(
                r'href="(https://www\.linkedin\.com/jobs/view/[^"]+)"', body
            )
            job_titles = re.findall(r'<h3[^>]*>([^<]+)</h3>', body)
            companies = re.findall(r'<h4[^>]*>([^<]+)</h4>', body)

            for i, link in enumerate(job_links[:10]):  # max 10 jobs per email
                title = job_titles[i] if i < len(job_titles) else ""
                company = companies[i] if i < len(companies) else ""
                ext_id = f"li_{hashlib.md5(link.encode()).hexdigest()[:16]}"
                raw_jobs.append({
                    "external_id": ext_id,
                    "title": title.strip(),
                    "company": company.strip(),
                    "location": "",
                    "url": link,
                    "raw_text": f"{title} {company} {body[:2000]}",
                    "source_name": "linkedin_imap",
                    "source_tier": 3,
                })

            # Mark email as read after processing
            mail.store(eid, "+FLAGS", "\\Seen")

        mail.logout()
        log.info("LinkedIn IMAP processed", jobs_extracted=len(raw_jobs))
    except Exception as e:
        log.error("LinkedIn IMAP error", error=str(e))
        raise IngestionError("LinkedIn IMAP failed", extra_context={"error": str(e)})

    return raw_jobs


# ─── Playwright JS Scraper (subprocess isolation) ─────────────────────────────
PLAYWRIGHT_SCRIPT = '''
import asyncio, json, sys
from playwright.async_api import async_playwright

async def scrape(url, selector_title, selector_company, selector_location, selector_items):
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await ctx.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        await asyncio.sleep(2)  # wait for JS to render
        items = await page.query_selector_all(selector_items)
        for item in items[:30]:
            try:
                title = await (await item.query_selector(selector_title)).inner_text() if selector_title else ""
                company = await (await item.query_selector(selector_company)).inner_text() if selector_company else ""
                location = await (await item.query_selector(selector_location)).inner_text() if selector_location else ""
                results.append({"title": title, "company": company, "location": location, "url": url})
            except Exception:
                pass
        await browser.close()
    print(json.dumps(results))

asyncio.run(scrape(
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
))
'''

# Sites to scrape with Playwright (add more here)
PLAYWRIGHT_TARGETS = [
    # {
    #     "name": "mysmartprice_jobs",
    #     "url": "https://www.mysmartprice.com/careers",
    #     "selector_items": ".job-card",
    #     "selector_title": ".job-title",
    #     "selector_company": ".company-name",
    #     "selector_location": ".location",
    # },
]


async def scrape_with_playwright(target: dict) -> list[dict]:
    """Run Playwright in a subprocess to prevent memory leaks."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(PLAYWRIGHT_SCRIPT)
        script_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "python", script_path,
            target["url"],
            target.get("selector_title", ""),
            target.get("selector_company", ""),
            target.get("selector_location", ""),
            target.get("selector_items", ".job"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        if proc.returncode != 0:
            err = stderr.decode()
            log.error("Playwright failed", target=target["name"], stderr=err)
            raise IngestionError("Playwright process failed", extra_context={"target": target["name"], "stderr": err})

        data = json.loads(stdout.decode())
        results = []
        for item in data:
            ext_id = f"pw_{hashlib.md5((item['url'] + item['title']).encode()).hexdigest()[:16]}"
            results.append({
                "external_id": ext_id,
                "title": item.get("title", ""),
                "company": item.get("company", ""),
                "location": item.get("location", ""),
                "url": item.get("url", ""),
                "raw_text": f"{item.get('title', '')} {item.get('company', '')} {item.get('location', '')}",
                "source_name": target["name"],
                "source_tier": 3,
            })
        log.info("Playwright scraped", target=target["name"], count=len(results))
        return results
    except asyncio.TimeoutError:
        log.error("Playwright timeout", target=target["name"])
        raise IngestionError("Playwright timeout", extra_context={"target": target["name"]})
    except Exception as e:
        log.error("Playwright error", target=target["name"], error=str(e))
        raise IngestionError("Playwright error", extra_context={"target": target["name"], "error": str(e)})
    finally:
        os.unlink(script_path)


# ─── Main pipeline ────────────────────────────────────────────────────────────
async def run_tier3_ingestion():
    raw_jobs: list[dict] = []

    # LinkedIn IMAP (sync, run in executor)
    loop = asyncio.get_event_loop()
    imap_jobs = await loop.run_in_executor(None, fetch_linkedin_from_imap)
    raw_jobs.extend(imap_jobs)

    # Playwright targets
    for target in PLAYWRIGHT_TARGETS:
        jobs = await scrape_with_playwright(target)
        raw_jobs.extend(jobs)

    log.info("Tier3 raw jobs", count=len(raw_jobs))

    to_process = [
        j for j in raw_jobs
        if not is_duplicate(j["external_id"], 3, j["title"], j["company"], j["location"])
    ]

    if not to_process:
        log.info("Tier3: no new jobs after dedup")
        return

    batch_size = settings.gemini_batch_size
    parsed_jobs = []
    for i in range(0, len(to_process), batch_size):
        batch = to_process[i:i + batch_size]
        results = batch_process_jobs(batch)
        parsed_jobs.extend(zip(batch, results))

    saved = 0
    with get_db_session() as session:
        for raw, parsed in parsed_jobs:
            title = parsed.title or raw["title"]
            company = parsed.company or raw["company"]
            location = parsed.location or raw["location"]
            canonical = _canonical_hash(title, company, location)

            if session.query(Job).filter_by(canonical_hash=canonical).first():
                continue

            job_obj = Job(
                external_id=raw["external_id"],
                canonical_hash=canonical,
                company=company, title=title,
                description_raw=raw.get("raw_text", "")[:5000],
                salary_low=parsed.salary_low, salary_high=parsed.salary_high,
                currency=parsed.currency, tech_stack=parsed.tech_stack,
                remote_policy=parsed.remote_policy, location=location,
                url=raw["url"], source_tier=3, source_name=raw["source_name"],
            )
            job_obj.score = calculate_score(job_obj)
            job_obj.is_dream_company = (job_obj.score == 100)
            session.add(job_obj)
            mark_processed(raw["external_id"], 3, title, company, location)
            saved += 1

    log.info("Tier3 ingestion complete", saved=saved)


if __name__ == "__main__":
    asyncio.run(run_tier3_ingestion())

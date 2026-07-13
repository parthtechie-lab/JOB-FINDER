"""
Tier 2: RSS / Sitemap Static Ingester
- feedparser for RSS feeds (Indeed, StackOverflow, etc.)
- lxml for /job-sitemap.xml parsing
- ETag / Last-Modified caching to avoid re-downloading unchanged feeds
- HEAD request fallback before full download
"""
import asyncio
import hashlib
import re
from datetime import datetime
from typing import Optional

import feedparser
import httpx
import redis
from lxml import etree

from src.exceptions import IngestionError

from src.config import get_settings
from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job, Source
from src.processors.dedup_engine import is_duplicate, mark_processed
from src.processors.ai_router import batch_process_jobs
from src.processors.scorer import calculate_score

log = get_logger("tier2_static")
settings = get_settings()
_redis = redis.from_url(settings.redis_url, decode_responses=True)

# ─── Static RSS sources ───────────────────────────────────────────────────────
RSS_FEEDS = [
    {"name": "indeed_remote_rust", "url": "https://www.indeed.com/rss?q=rust+remote&sort=date&limit=50"},
    {"name": "indeed_remote_golang", "url": "https://www.indeed.com/rss?q=golang+remote&sort=date&limit=50"},
    {"name": "stackoverflow_jobs", "url": "https://stackoverflow.com/jobs/feed"},
    {"name": "remotive_software", "url": "https://remotive.com/remote-jobs/feed/category/software-dev"},
    {"name": "weworkremotely_prog", "url": "https://weworkremotely.com/categories/remote-programming-jobs.rss"},
    {"name": "remoteco", "url": "https://remote.co/remote-jobs/developer/feed/"},
]

SITEMAPS = [
    # Add company-specific job sitemaps here
    # {"name": "notion_jobs", "url": "https://www.notion.so/careers/job-sitemap.xml"},
]


def _canonical_hash(title: str, company: str, location: str) -> str:
    raw = f"{title.lower()}|{company.lower()}|{location.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _etag_key(url: str) -> str:
    return f"rss:etag:{hashlib.md5(url.encode()).hexdigest()}"

def _lastmod_key(url: str) -> str:
    return f"rss:lastmod:{hashlib.md5(url.encode()).hexdigest()}"


# ─── RSS Fetcher ──────────────────────────────────────────────────────────────
async def fetch_rss(client: httpx.AsyncClient, feed_cfg: dict) -> list[dict]:
    url = feed_cfg["url"]
    name = feed_cfg["name"]

    etag_key = _etag_key(url)
    lastmod_key = _lastmod_key(url)
    cached_etag = _redis.get(etag_key)
    cached_lastmod = _redis.get(lastmod_key)

    headers = {}
    if cached_etag:
        headers["If-None-Match"] = cached_etag
    if cached_lastmod:
        headers["If-Modified-Since"] = cached_lastmod

    try:
        resp = await client.get(url, headers=headers, timeout=30, follow_redirects=True)
        if resp.status_code == 304:
            log.info("RSS feed unchanged (304)", name=name)
            return []

        resp.raise_for_status()

        # Cache ETag/Last-Modified
        if "ETag" in resp.headers:
            _redis.setex(etag_key, 86400, resp.headers["ETag"])
        if "Last-Modified" in resp.headers:
            _redis.setex(lastmod_key, 86400, resp.headers["Last-Modified"])

        feed = feedparser.parse(resp.text)
        entries = feed.get("entries", [])
        log.info("RSS feed fetched", name=name, count=len(entries))
        return [_rss_entry_to_raw(e, name) for e in entries]

    except Exception as e:
        log.error("RSS fetch error", name=name, url=url, error=str(e))
        raise IngestionError("Failed to fetch RSS feed", extra_context={"name": name, "url": url, "error": str(e)})


def _rss_entry_to_raw(entry: dict, source_name: str) -> dict:
    title = entry.get("title", "")
    # Try to extract company from title (e.g., "Senior Rust Engineer at Stripe")
    company = ""
    if " at " in title:
        parts = title.rsplit(" at ", 1)
        title = parts[0].strip()
        company = parts[1].strip()

    location = entry.get("location", "") or ""
    summary = entry.get("summary", "") or ""
    content = entry.get("content", [{}])[0].get("value", "") if entry.get("content") else ""

    return {
        "external_id": f"rss_{hashlib.md5(entry.get('link','').encode()).hexdigest()[:16]}",
        "title": title,
        "company": company,
        "location": location,
        "url": entry.get("link", ""),
        "raw_text": f"{title} {company} {location} {summary} {content}"[:3000],
        "source_name": source_name,
        "source_tier": 2,
    }


# ─── Sitemap Parser ───────────────────────────────────────────────────────────
async def fetch_sitemap(client: httpx.AsyncClient, sitemap_cfg: dict) -> list[dict]:
    url = sitemap_cfg["url"]
    name = sitemap_cfg["name"]

    # HEAD check first
    try:
        head = await client.head(url, timeout=10)
        lastmod_key = _lastmod_key(url)
        cached_lm = _redis.get(lastmod_key)
        server_lm = head.headers.get("Last-Modified", "")
        if cached_lm and cached_lm == server_lm:
            log.info("Sitemap unchanged (HEAD check)", name=name)
            return []
        if server_lm:
            _redis.setex(lastmod_key, 86400, server_lm)
    except Exception:
        pass

    try:
        resp = await client.get(url, timeout=30, follow_redirects=True)
        resp.raise_for_status()
        root = etree.fromstring(resp.content)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        urls = [loc.text for loc in root.findall(".//sm:loc", ns) if loc.text]
        log.info("Sitemap fetched", name=name, urls=len(urls))
        return [
            {
                "external_id": f"sm_{hashlib.md5(u.encode()).hexdigest()[:16]}",
                "title": "", "company": name, "location": "",
                "url": u, "raw_text": u, "source_name": name, "source_tier": 2,
            }
            for u in urls
        ]
    except Exception as e:
        log.error("Sitemap parse error", name=name, error=str(e))
        raise IngestionError("Failed to parse Sitemap", extra_context={"name": name, "url": url, "error": str(e)})


# ─── Main pipeline ────────────────────────────────────────────────────────────
async def run_tier2_ingestion():
    raw_jobs: list[dict] = []

    async with httpx.AsyncClient(http2=True) as client:
        tasks = [fetch_rss(client, f) for f in RSS_FEEDS]
        tasks += [fetch_sitemap(client, s) for s in SITEMAPS]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        for r in results:
            if isinstance(r, list):
                raw_jobs.extend(r)

    log.info("Tier2 total raw jobs", count=len(raw_jobs))

    to_process = [
        j for j in raw_jobs
        if not is_duplicate(j["external_id"], 2, j["title"], j["company"], j["location"])
    ]
    log.info("Tier2 new after dedup", new=len(to_process))

    if not to_process:
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
                salary_low=parsed.salary_low,
                salary_high=parsed.salary_high,
                currency=parsed.currency,
                tech_stack=parsed.tech_stack,
                remote_policy=parsed.remote_policy,
                location=location,
                url=raw["url"],
                source_tier=2, source_name=raw["source_name"],
            )
            job_obj.score = calculate_score(job_obj)
            job_obj.is_dream_company = (job_obj.score == 100)
            session.add(job_obj)
            mark_processed(raw["external_id"], 2, title, company, location)
            saved += 1

    log.info("Tier2 ingestion complete", saved=saved)


if __name__ == "__main__":
    asyncio.run(run_tier2_ingestion())

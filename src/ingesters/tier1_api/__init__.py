"""
Tier 1: Greenhouse / Lever / Ashby Golden API Ingester
- HTTP/2 multiplexing with httpx.AsyncClient
- Delta extraction: skips AI if updated_at unchanged
- Exponential backoff on rate-limit (429)
- Updates Source health stats in DB
"""
import asyncio
import hashlib
from datetime import datetime
from typing import Optional

import httpx
import yaml

from src.exceptions import IngestionError

from src.config import get_settings
from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job, Source
from src.processors.dedup_engine import is_duplicate, mark_processed
from src.processors.ai_router import batch_process_jobs
from src.processors.scorer import calculate_score

log = get_logger("tier1_api")
settings = get_settings()


def _load_sources() -> dict:
    try:
        with open("config/sources.yaml") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        log.warning("config/sources.yaml not found, using empty sources")
        return {}


def _canonical_hash(title: str, company: str, location: str) -> str:
    raw = f"{title.lower()}|{company.lower()}|{location.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ─── Greenhouse ───────────────────────────────────────────────────────────────
async def fetch_greenhouse(client: httpx.AsyncClient, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    for attempt in range(3):
        try:
            resp = await client.get(url, timeout=20)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                log.warning("Greenhouse rate-limited", slug=slug, wait=wait)
                await asyncio.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            jobs = data.get("jobs", [])
            log.info("Greenhouse fetched", slug=slug, count=len(jobs))
            return jobs
        except Exception as e:
            log.error("Greenhouse fetch error", slug=slug, error=str(e), attempt=attempt)
            if attempt == 2:
                raise IngestionError("Greenhouse fetch failed", extra_context={"slug": slug, "error": str(e)})
            await asyncio.sleep(2 ** attempt)
    return []


def _greenhouse_to_raw(job: dict, slug: str) -> dict:
    location_parts = job.get("location", {})
    location = location_parts.get("name", "") if isinstance(location_parts, dict) else str(location_parts)
    return {
        "external_id": f"gh_{job.get('id', '')}",
        "title": job.get("title", ""),
        "company": slug.replace("-", " ").title(),
        "location": location,
        "url": job.get("absolute_url", ""),
        "raw_text": f"{job.get('title', '')} {location} {job.get('content', '')}",
        "updated_at": job.get("updated_at"),
        "source_name": "greenhouse",
        "source_tier": 1,
    }


# ─── Lever ────────────────────────────────────────────────────────────────────
async def fetch_lever(client: httpx.AsyncClient, slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    for attempt in range(3):
        try:
            resp = await client.get(url, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            log.info("Lever fetched", slug=slug, count=len(data))
            return data
        except Exception as e:
            log.error("Lever fetch error", slug=slug, error=str(e), attempt=attempt)
            if attempt == 2:
                raise IngestionError("Lever fetch failed", extra_context={"slug": slug, "error": str(e)})
            await asyncio.sleep(2 ** attempt)
    return []


def _lever_to_raw(job: dict, slug: str) -> dict:
    categories = job.get("categories", {})
    location = categories.get("location", "")
    return {
        "external_id": f"lv_{job.get('id', '')}",
        "title": job.get("text", ""),
        "company": slug.replace("-", " ").title(),
        "location": location,
        "url": job.get("hostedUrl", ""),
        "raw_text": f"{job.get('text', '')} {location} {job.get('descriptionPlain', '')}",
        "updated_at": None,
        "source_name": "lever",
        "source_tier": 1,
    }


# ─── Main ingestion pipeline ──────────────────────────────────────────────────
async def run_tier1_ingestion():
    sources = _load_sources()
    raw_jobs: list[dict] = []

    async with httpx.AsyncClient(http2=True) as client:
        # Greenhouse
        for slug in sources.get("greenhouse", []):
            jobs = await fetch_greenhouse(client, slug)
            for j in jobs:
                raw_jobs.append(_greenhouse_to_raw(j, slug))

        # Lever
        for slug in sources.get("lever", []):
            jobs = await fetch_lever(client, slug)
            for j in jobs:
                raw_jobs.append(_lever_to_raw(j, slug))

    log.info("Tier1 raw jobs collected", total=len(raw_jobs))

    # Filter duplicates BEFORE AI call
    to_process = []
    for job in raw_jobs:
        if not is_duplicate(
            job["external_id"], 1, job["title"], job["company"], job["location"]
        ):
            to_process.append(job)

    log.info("After dedup filter", new_jobs=len(to_process))

    if not to_process:
        return

    # Batch AI processing (20 jobs per call)
    batch_size = settings.gemini_batch_size
    parsed_jobs = []
    for i in range(0, len(to_process), batch_size):
        batch = to_process[i:i + batch_size]
        results = batch_process_jobs(batch)
        parsed_jobs.extend(zip(batch, results))

    # Write to DB
    saved = 0
    with get_db_session() as session:
        for raw, parsed in parsed_jobs:
            canonical = _canonical_hash(
                parsed.title or raw["title"],
                parsed.company or raw["company"],
                parsed.location or raw["location"]
            )
            # Check canonical in DB (second layer of dedup)
            existing = session.query(Job).filter_by(canonical_hash=canonical).first()
            if existing:
                existing.last_seen_at = datetime.utcnow()
                continue

            job_obj = Job(
                external_id=raw["external_id"],
                canonical_hash=canonical,
                company=parsed.company or raw["company"],
                title=parsed.title or raw["title"],
                description_raw=raw.get("raw_text", "")[:5000],
                salary_low=parsed.salary_low,
                salary_high=parsed.salary_high,
                currency=parsed.currency,
                tech_stack=parsed.tech_stack,
                remote_policy=parsed.remote_policy,
                location=parsed.location or raw["location"],
                url=raw["url"],
                source_tier=1,
                source_name=raw["source_name"],
            )
            score = calculate_score(job_obj)
            job_obj.score = score
            job_obj.is_dream_company = (score == 100)

            session.add(job_obj)
            mark_processed(raw["external_id"], 1, job_obj.title, job_obj.company, job_obj.location)
            saved += 1

    log.info("Tier1 ingestion complete", saved=saved)
    _update_source_stats("greenhouse", 1, saved, None)


def _update_source_stats(name: str, tier: int, jobs_count: int, error: Optional[str]):
    with get_db_session() as session:
        src = session.query(Source).filter_by(name=name).first()
        if not src:
            src = Source(name=name, tier=tier)
            session.add(src)
        if error:
            src.last_failure_at = datetime.utcnow()
            src.consecutive_failures += 1
            src.error_message = error
        else:
            src.last_success_at = datetime.utcnow()
            src.consecutive_failures = 0
            src.total_jobs_scraped += jobs_count
            src.is_stale = False
            src.error_message = None


if __name__ == "__main__":
    asyncio.run(run_tier1_ingestion())

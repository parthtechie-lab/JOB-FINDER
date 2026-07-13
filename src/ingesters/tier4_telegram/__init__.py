"""
Tier 4: Telegram Channel Streamer
- Telethon polls Telegram channels with job postings every 5 minutes
- Pre-filter: discard messages without a company name OR salary mention
  (saves Gemini tokens)
- Sends high-scoring jobs to Telegram bot as alerts
"""
import asyncio
import hashlib
import re
from datetime import datetime

from src.config import get_settings
from src.exceptions import IngestionError
from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job
from src.processors.dedup_engine import is_duplicate, mark_processed
from src.processors.ai_router import batch_process_jobs
from src.processors.scorer import calculate_score

log = get_logger("tier4_telegram")
settings = get_settings()

# ─── Telegram channels to monitor ─────────────────────────────────────────────
TELEGRAM_JOB_CHANNELS = [
    "cybersecurity_jobs",
    "infosec_jobs",
    "appsec_remote",
    "soc_analyst_jobs",
]

_SALARY_RE = re.compile(r"\$[\d,]+[kK]?|\d+[kK]\s*(?:usd|eur|gbp)?", re.IGNORECASE)
_COMPANY_RE = re.compile(r"(?:at|@|company:?)\s+([A-Z][a-zA-Z0-9\s&,\.]+)", re.IGNORECASE)


def _has_salary(text: str) -> bool:
    return bool(_SALARY_RE.search(text))


def _has_company(text: str) -> bool:
    return bool(_COMPANY_RE.search(text)) or "@" in text or "company:" in text.lower()


def _extract_company(text: str) -> str:
    m = _COMPANY_RE.search(text)
    if m:
        return m.group(1).strip()[:100]
    return ""


def _canonical_hash(title: str, company: str, location: str) -> str:
    raw = f"{title.lower()}|{company.lower()}|{location.lower()}"
    return hashlib.sha256(raw.encode()).hexdigest()


async def run_tier4_ingestion():
    """
    Polls Telegram channels for job messages.
    Requires Telethon session to be initialized separately.
    """
    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        log.error("Telethon not installed. Run: poetry add telethon")
        return

    # Telethon needs API credentials (not the bot token)
    api_id = settings.__dict__.get("telegram_api_id")
    api_hash = settings.__dict__.get("telegram_api_hash")

    if not api_id or not api_hash:
        log.warning("TELEGRAM_API_ID/TELEGRAM_API_HASH not configured, skipping Tier 4")
        return

    raw_jobs: list[dict] = []

    try:
        async with TelegramClient("career_raider_session", api_id, api_hash) as client:
            for channel in TELEGRAM_JOB_CHANNELS:
                try:
                    messages = await client.get_messages(channel, limit=50)
                    for msg in messages:
                        text = msg.text or msg.message or ""
                        if not text or len(text) < 30:
                            continue

                        # PRE-FILTER: discard if no company AND no salary
                        if not _has_company(text) and not _has_salary(text):
                            log.debug("Discarding message (no company/salary)", channel=channel)
                            continue

                        company = _extract_company(text)
                        ext_id = f"tg_{msg.id}_{channel[:10]}"

                        raw_jobs.append({
                            "external_id": ext_id,
                            "title": text[:100],
                            "company": company,
                            "location": "",
                            "url": f"https://t.me/{channel}/{msg.id}",
                            "raw_text": text[:3000],
                            "source_name": f"telegram_{channel}",
                            "source_tier": 4,
                        })
                except Exception as e:
                    log.error("Telegram channel error", channel=channel, error=str(e))
                    raise IngestionError("Telegram channel fetch failed", extra_context={"channel": channel, "error": str(e)})

    except Exception as e:
        log.error("Telethon client error", error=str(e))
        raise IngestionError("Telethon client failed", extra_context={"error": str(e)})

    log.info("Tier4 messages collected", count=len(raw_jobs))

    to_process = [
        j for j in raw_jobs
        if not is_duplicate(j["external_id"], 4, j["title"], j["company"], j["location"])
    ]

    if not to_process:
        log.info("Tier4: no new jobs after dedup")
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
                url=raw["url"], source_tier=4, source_name=raw["source_name"],
            )
            job_obj.score = calculate_score(job_obj)
            job_obj.is_dream_company = (job_obj.score == 100)
            session.add(job_obj)
            mark_processed(raw["external_id"], 4, title, company, location)
            saved += 1

    log.info("Tier4 ingestion complete", saved=saved)


if __name__ == "__main__":
    asyncio.run(run_tier4_ingestion())

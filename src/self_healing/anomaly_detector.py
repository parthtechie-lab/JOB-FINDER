"""
Career Raider - Self-Healing: Anomaly Detector
Runs every 6 hours via Celery beat.
Checks each source for stale output (0 jobs in 24h).
If stale, triggers the patch generator.
"""
from datetime import datetime, timedelta

from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job, Source
from src.processors.alerter import _send_message

log = get_logger("anomaly_detector")

STALE_THRESHOLD_HOURS = 24
MAX_CONSECUTIVE_FAILURES = 3


def run_anomaly_detection():
    log.info("Running anomaly detection...")

    with get_db_session() as session:
        since = datetime.utcnow() - timedelta(hours=STALE_THRESHOLD_HOURS)

        # Check each source
        sources = session.query(Source).all()
        for src in sources:
            recent_count = (
                session.query(Job)
                .filter(Job.source_name == src.name, Job.ingested_at >= since)
                .count()
            )

            if recent_count == 0:
                log.warning("Source is STALE", source=src.name, tier=src.tier)
                src.is_stale = True
                _send_message(
                    f"⚠️ <b>Stale Source Detected</b>\n"
                    f"Source: <b>{src.name}</b> (Tier {src.tier})\n"
                    f"Last 24h jobs: 0\n"
                    f"Triggering self-healing scan..."
                )
                # Trigger patch generator for Tier 3 (Playwright) sources
                if src.tier == 3:
                    _trigger_patch_generator(src.name)
            else:
                src.is_stale = False
                log.info("Source healthy", source=src.name, jobs_24h=recent_count)

            # Flag sources with repeated failures
            if src.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.error(
                    "Source has repeated failures",
                    source=src.name,
                    failures=src.consecutive_failures
                )

    log.info("Anomaly detection complete")


def _trigger_patch_generator(source_name: str):
    """Kick off patch generator for a stale source."""
    try:
        from src.self_healing.patch_generator import generate_patch
        generate_patch(source_name)
    except Exception as e:
        log.error("Patch generator trigger failed", source=source_name, error=str(e))

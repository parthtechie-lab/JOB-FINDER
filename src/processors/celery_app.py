"""
Career Raider - Celery Task Definitions
Periodic beats schedule all 4 tiers + health checks + anomaly detection.
"""
import asyncio
from celery import Celery
from celery.schedules import crontab
from src.config import get_settings
from src.logger import get_logger
import sys

log = get_logger("celery")
settings = get_settings()
redis_url = settings.redis_url

app = Celery("career_raider", broker=redis_url, backend=redis_url)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_routes={
        "src.processors.tasks.run_tier1": {"queue": "fast_queue"},
        "src.processors.tasks.run_tier2": {"queue": "fast_queue"},
        "src.processors.tasks.run_tier3": {"queue": "heavy_queue"},
        "src.processors.tasks.run_tier4": {"queue": "fast_queue"},
        "src.processors.tasks.send_health_report": {"queue": "fast_queue"},
        "src.processors.tasks.run_anomaly_check": {"queue": "fast_queue"},
        "src.processors.tasks.send_high_score_alerts": {"queue": "fast_queue"},
        "src.processors.tasks.send_daily_summary": {"queue": "fast_queue"},
    },
    beat_schedule={
        # Tier 1: every 60 seconds
        "tier1-greenhouse-lever": {
            "task": "src.processors.tasks.run_tier1",
            "schedule": settings.greenhouse_poll_interval,
        },
        # Tier 2: every 5 minutes
        "tier2-rss-sitemaps": {
            "task": "src.processors.tasks.run_tier2",
            "schedule": settings.rss_poll_interval,
        },
        # Tier 3: every 15 minutes
        "tier3-playwright-imap": {
            "task": "src.processors.tasks.run_tier3",
            "schedule": settings.playwright_poll_interval,
        },
        # Tier 4: every 5 minutes
        "tier4-telegram": {
            "task": "src.processors.tasks.run_tier4",
            "schedule": settings.telegram_poll_interval,
        },
        # Send alerts for high-score jobs every 60 minutes
        "send-alerts": {
            "task": "src.processors.tasks.send_high_score_alerts",
            "schedule": 3600,
        },
        # Daily summary at 18:20 UTC (23:50 IST)
        "send-daily-summary": {
            "task": "src.processors.tasks.send_daily_summary",
            "schedule": crontab(hour=18, minute=20),
        },
        # 6-hour health report
        "health-report": {
            "task": "src.processors.tasks.send_health_report",
            "schedule": settings.health_report_interval,
        },
        # Anomaly detector every 6 hours
        "anomaly-check": {
            "task": "src.processors.tasks.run_anomaly_check",
            "schedule": settings.health_report_interval,
        },
    },
)


def _run_async(coro):
    """Safely run async code from a Celery (sync) task."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class CeleryBaseTask(app.Task):
    """Base task that catches unhandled errors, logs incidents, and triggers OpenHands."""
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        super().on_failure(exc, task_id, args, kwargs, einfo)
        
        # We don't trigger self-healing for normal Retries
        from celery.exceptions import Retry
        if isinstance(exc, Retry):
            return

        log.error("Celery task hard failure", task_name=self.name, task_id=task_id, error=str(exc))
        
        try:
            from src.self_healing.incident_logger import log_incident
            
            # Note: einfo.exc_info is a tuple (type, value, traceback)
            incident_path, should_trigger = log_incident(
                context_name=f"celery_{self.name}",
                exc_info=einfo.exc_info if einfo else None,
                extra_context={"task_id": task_id, "args": args, "kwargs": kwargs}
            )
            
            if should_trigger:
                from src.processors.alerter import _send_message
                _send_message(
                    f"🚨 <b>Critical Celery Failure</b>\n"
                    f"Task: <code>{self.name}</code>\n"
                    f"Error: {str(exc)}\n"
                    f"Incident logged. OpenHands is attempting to generate a fix PR..."
                )
                from src.self_healing.patch_generator import generate_patch
                # Using the task name as the source hint, but passing the incident path
                generate_patch(source_name=self.name, incident_path=incident_path)
        except Exception as e:
            log.error("Failed to run incident logger or patch generator in on_failure", error=str(e))


@app.task(name="src.processors.tasks.run_tier1", base=CeleryBaseTask, bind=True, max_retries=3)
def run_tier1(self):
    from src.ingesters.tier1_api import run_tier1_ingestion
    try:
        _run_async(run_tier1_ingestion())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@app.task(name="src.processors.tasks.run_tier2", base=CeleryBaseTask, bind=True, max_retries=3)
def run_tier2(self):
    from src.ingesters.tier2_static import run_tier2_ingestion
    try:
        _run_async(run_tier2_ingestion())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=120)


@app.task(name="src.processors.tasks.run_tier3", base=CeleryBaseTask, bind=True, max_retries=2)
def run_tier3(self):
    from src.ingesters.tier3_heavy import run_tier3_ingestion
    try:
        _run_async(run_tier3_ingestion())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=300)


@app.task(name="src.processors.tasks.run_tier4", base=CeleryBaseTask, bind=True, max_retries=3)
def run_tier4(self):
    from src.ingesters.tier4_telegram import run_tier4_ingestion
    try:
        _run_async(run_tier4_ingestion())
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@app.task(name="src.processors.tasks.send_high_score_alerts")
def send_high_score_alerts():
    from src.processors.alerter import alert_high_score_jobs
    alert_high_score_jobs()


@app.task(name="src.processors.tasks.send_health_report")
def send_health_report():
    from src.processors.alerter import send_telegram_health_report
    send_telegram_health_report()


@app.task(name="src.processors.tasks.run_anomaly_check")
def run_anomaly_check():
    from src.self_healing.anomaly_detector import run_anomaly_detection
    run_anomaly_detection()


@app.task(name="src.processors.tasks.send_daily_summary")
def send_daily_summary():
    from src.processors.alerter import send_daily_job_summary
    send_daily_job_summary()

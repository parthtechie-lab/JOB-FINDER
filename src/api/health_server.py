"""
Career Raider - Industrial FastAPI Health Server
- /health (liveness probe)
- /ready  (readiness probe: checks DB + Redis)
- /metrics (Prometheus metrics)
- /trigger/{tier} (manual trigger endpoint for any ingestion tier)
- /jobs (query recent jobs)
"""
import time
from datetime import datetime, timedelta
from typing import Optional

import redis as redis_lib
import prometheus_client
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, JSONResponse
from pydantic import BaseModel

from src.exceptions import CareerRaiderError

from src.config import get_settings
from src.logger import get_logger, setup_logging
from src.models.database import init_db, check_db_health, get_db_session, wait_for_db
from src.models.job import Job, Source

setup_logging()
log = get_logger("health_server")
settings = get_settings()

app = FastAPI(
    title="Career Raider API",
    description="Job scraping and alerting system — health & control endpoints",
    version="1.0.0",
)

# ─── Prometheus Metrics ───────────────────────────────────────────────────────
scraped_jobs_total = prometheus_client.Counter(
    "scraped_jobs_total", "Total jobs scraped", ["source", "tier"]
)
ai_processing_latency_seconds = prometheus_client.Histogram(
    "ai_processing_latency_seconds", "Latency of AI (Gemini) processing calls",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0]
)
telegram_connection_status = prometheus_client.Gauge(
    "telegram_connection_status", "Telegram connection (1=alive, 0=dead)"
)
redis_dedup_cache_hits = prometheus_client.Counter(
    "redis_dedup_cache_hits_total", "Redis dedup cache hits"
)
celery_queue_depth = prometheus_client.Gauge(
    "celery_queue_depth", "Number of tasks in Celery queue", ["queue"]
)


# ─── Startup ──────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    log.info("Starting Career Raider API server...")
    wait_for_db(max_retries=10, delay=3)
    init_db()
    log.info("Career Raider API ready ✅")


@app.exception_handler(CareerRaiderError)
async def career_raider_exception_handler(request: Request, exc: CareerRaiderError):
    """Catch custom structured errors and return consistent JSON."""
    log.error(f"CareerRaiderError ({exc.__class__.__name__})", url=str(request.url), error=str(exc))
    
    # Log incident with deep context
    try:
        from src.self_healing.incident_logger import log_incident
        incident_path, should_trigger = log_incident(
            context_name=f"api_{exc.__class__.__name__}",
            exc_info=None,
            extra_context={
                "url": str(request.url),
                "method": request.method,
                "client": request.client.host if request.client else "unknown",
                "custom_context": exc.extra_context
            }
        )
        if should_trigger:
            from src.processors.alerter import _send_message
            _send_message(
                f"🚨 <b>API Error: {exc.__class__.__name__}</b>\n"
                f"Endpoint: <code>{request.method} {request.url.path}</code>\n"
                f"Error: {exc.message}\n"
                f"OpenHands is investigating..."
            )
            from src.self_healing.patch_generator import generate_patch
            generate_patch(source_name="api_server", incident_path=incident_path)
    except Exception as e:
        log.error("Failed to run incident logger in CareerRaiderError handler", error=str(e))
        
    return JSONResponse(
        status_code=500,
        content={
            "error": exc.__class__.__name__,
            "message": exc.message,
            "details": exc.extra_context
        }
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch all 500s, log an incident, and potentially trigger OpenHands."""
    log.error("Unhandled API exception", url=str(request.url), error=str(exc))
    
    try:
        from src.self_healing.incident_logger import log_incident
        
        incident_path, should_trigger = log_incident(
            context_name="api_500",
            exc_info=None,  # sys.exc_info() is implicitly captured
            extra_context={
                "url": str(request.url),
                "method": request.method,
                "client": request.client.host if request.client else "unknown"
            }
        )
        
        if should_trigger:
            from src.processors.alerter import _send_message
            _send_message(
                f"🚨 <b>API 500 Error</b>\n"
                f"Endpoint: <code>{request.method} {request.url.path}</code>\n"
                f"Error: {str(exc)}\n"
                f"OpenHands is investigating..."
            )
            from src.self_healing.patch_generator import generate_patch
            generate_patch(source_name="api_server", incident_path=incident_path)
            
    except Exception as e:
        log.error("Failed to run incident logger in global handler", error=str(e))
        
    # Return RFC 7807 Problem Details
    return JSONResponse(
        status_code=500,
        content={
            "type": "about:blank",
            "title": "Internal Server Error",
            "status": 500,
            "detail": "An unexpected error occurred. The self-healing agent has been notified."
        }
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
def liveness():
    """Kubernetes liveness probe."""
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}


@app.get("/ready", tags=["Health"])
def readiness():
    """Kubernetes readiness probe — checks DB and Redis."""
    issues = []

    if not check_db_health():
        issues.append("PostgreSQL unreachable")

    try:
        r = redis_lib.from_url(settings.redis_url)
        r.ping()
    except Exception:
        issues.append("Redis unreachable")

    if issues:
        raise HTTPException(status_code=503, detail={"ready": False, "issues": issues})

    return {"ready": True, "timestamp": datetime.utcnow().isoformat()}


@app.get("/metrics", tags=["Monitoring"])
def metrics():
    """Prometheus metrics endpoint."""
    return PlainTextResponse(
        content=prometheus_client.generate_latest().decode(),
        media_type="text/plain; version=0.0.4"
    )


@app.get("/jobs", tags=["Data"])
def list_jobs(
    limit: int = Query(20, ge=1, le=100),
    min_score: int = Query(0, ge=0, le=100),
    remote_only: bool = Query(False),
    hours: int = Query(24, ge=1, le=168),
):
    """Query recent high-scoring jobs."""
    since = datetime.utcnow() - timedelta(hours=hours)
    with get_db_session() as session:
        q = session.query(Job).filter(Job.ingested_at >= since, Job.score >= min_score)
        if remote_only:
            q = q.filter(Job.remote_policy == "remote")
        jobs = q.order_by(Job.score.desc(), Job.ingested_at.desc()).limit(limit).all()

        return [
            {
                "id": str(j.id),
                "company": j.company,
                "title": j.title,
                "score": j.score,
                "salary_low": j.salary_low,
                "salary_high": j.salary_high,
                "remote_policy": j.remote_policy,
                "tech_stack": j.tech_stack,
                "location": j.location,
                "url": j.url,
                "source": j.source_name,
                "tier": j.source_tier,
                "ingested_at": j.ingested_at.isoformat(),
                "is_dream_company": j.is_dream_company,
            }
            for j in jobs
        ]


@app.get("/stats", tags=["Data"])
def stats():
    """Returns per-tier job counts and source health."""
    since = datetime.utcnow() - timedelta(hours=24)
    with get_db_session() as session:
        total = session.query(Job).count()
        last_24h = session.query(Job).filter(Job.ingested_at >= since).count()
        tier_counts = {
            t: session.query(Job).filter(Job.source_tier == t).count()
            for t in range(1, 5)
        }
        sources = [
            {
                "name": s.name, "tier": s.tier,
                "last_success": s.last_success_at.isoformat() if s.last_success_at else None,
                "failures": s.consecutive_failures,
                "is_stale": s.is_stale,
                "total_scraped": s.total_jobs_scraped,
            }
            for s in session.query(Source).all()
        ]

    return {
        "total_jobs": total,
        "last_24h": last_24h,
        "by_tier": tier_counts,
        "sources": sources,
    }


@app.post("/trigger/{tier}", tags=["Control"])
def manual_trigger(tier: int):
    """Manually trigger ingestion for a specific tier (1-4)."""
    from src.processors.celery_app import run_tier1, run_tier2, run_tier3, run_tier4
    tasks = {1: run_tier1, 2: run_tier2, 3: run_tier3, 4: run_tier4}
    if tier not in tasks:
        raise HTTPException(status_code=400, detail=f"Invalid tier: {tier}. Must be 1-4.")
    task = tasks[tier].delay()
    log.info("Manual trigger", tier=tier, task_id=task.id)
    return {"status": "queued", "tier": tier, "task_id": task.id}

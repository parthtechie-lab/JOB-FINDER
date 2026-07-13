"""
Career Raider - Prometheus Metrics Exporter
Populates Prometheus gauges from the database for Grafana dashboards.
Run alongside celery beat or as a standalone sidecar.
"""
import time
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import prometheus_client
from datetime import datetime, timedelta
from src.models.database import get_db_session
from src.models.job import Job, Source
from src.logger import get_logger, setup_logging

setup_logging()
log = get_logger("metrics_exporter")

# Metrics
jobs_total = prometheus_client.Gauge("career_raider_jobs_total", "Total jobs in DB")
jobs_24h = prometheus_client.Gauge("career_raider_jobs_24h", "Jobs ingested in last 24h")
jobs_by_tier = prometheus_client.Gauge("career_raider_jobs_by_tier", "Jobs by tier", ["tier"])
dream_jobs_total = prometheus_client.Gauge("career_raider_dream_jobs_total", "Dream company jobs")
stale_sources = prometheus_client.Gauge("career_raider_stale_sources", "Count of stale sources")
source_failures = prometheus_client.Gauge(
    "career_raider_source_consecutive_failures", "Max consecutive failures across all sources"
)


def collect_metrics():
    since = datetime.utcnow() - timedelta(hours=24)
    try:
        with get_db_session() as session:
            total = session.query(Job).count()
            jobs_total.set(total)

            h24 = session.query(Job).filter(Job.ingested_at >= since).count()
            jobs_24h.set(h24)

            for tier in range(1, 5):
                c = session.query(Job).filter(Job.source_tier == tier).count()
                jobs_by_tier.labels(tier=str(tier)).set(c)

            dream = session.query(Job).filter(Job.is_dream_company == True).count()
            dream_jobs_total.set(dream)

            srcs = session.query(Source).all()
            stale = sum(1 for s in srcs if s.is_stale)
            stale_sources.set(stale)
            max_fail = max((s.consecutive_failures for s in srcs), default=0)
            source_failures.set(max_fail)

        log.info("Metrics collected", total=total, last_24h=h24)
    except Exception as e:
        log.error("Metrics collection error", error=str(e))


if __name__ == "__main__":
    prometheus_client.start_http_server(9091)
    log.info("Metrics exporter started on :9091")
    while True:
        collect_metrics()
        time.sleep(60)

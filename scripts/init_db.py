#!/usr/bin/env python3
"""
Career Raider - Database Initialization Script
Idempotent: creates tables + initial seed data.
"""
import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config import get_settings
from src.logger import get_logger, setup_logging
from src.models.database import init_db, wait_for_db, get_db_session
from src.models.job import Source

setup_logging()
log = get_logger("init_db")


def seed_sources():
    """Seed the sources table with known sources."""
    import yaml
    with open("config/sources.yaml") as f:
        cfg = yaml.safe_load(f) or {}

    with get_db_session() as session:
        for slug in cfg.get("greenhouse", []):
            if not session.query(Source).filter_by(name=slug).first():
                session.add(Source(
                    name=slug, tier=1,
                    url=f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
                ))
        for slug in cfg.get("lever", []):
            if not session.query(Source).filter_by(name=slug).first():
                session.add(Source(
                    name=slug, tier=1,
                    url=f"https://api.lever.co/v0/postings/{slug}"
                ))
        # RSS feeds
        rss_names = [
            "indeed_remote_rust", "indeed_remote_golang", "stackoverflow_jobs",
            "remotive_software", "weworkremotely_prog", "remoteco"
        ]
        for name in rss_names:
            if not session.query(Source).filter_by(name=name).first():
                session.add(Source(name=name, tier=2))

    log.info("Sources seeded")


if __name__ == "__main__":
    log.info("Waiting for database...")
    wait_for_db()
    log.info("Initializing tables...")
    init_db()
    log.info("Seeding sources...")
    seed_sources()
    log.info("✅ Database initialized successfully!")

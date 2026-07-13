"""
Career Raider - SQLAlchemy Database Models
PostgreSQL 16 schema with UUID PKs, ARRAY fields, and proper indexes.
"""
import uuid
from datetime import datetime
from sqlalchemy import (
    Column, String, Integer, Text, DateTime, Boolean, Float,
    ForeignKey, Index, func, text
)
from sqlalchemy.dialects.postgresql import UUID, ARRAY, JSON
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Job(Base):
    """
    Primary job posting record.
    canonical_hash = SHA256(lower(title)|lower(company)|lower(location))
    Ensures cross-platform deduplication.
    """
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    external_id = Column(String(255), unique=True, nullable=True)  # Greenhouse/Lever ID
    canonical_hash = Column(String(64), unique=True, nullable=False)  # SHA256

    # Job details
    company = Column(String(255), nullable=False, index=True)
    title = Column(Text, nullable=False)
    description_raw = Column(Text, nullable=True)  # Raw text before AI parse

    # Salary
    salary_low = Column(Integer, nullable=True)
    salary_high = Column(Integer, nullable=True)
    currency = Column(String(3), nullable=True, default="USD")

    # Tech & remote
    tech_stack = Column(ARRAY(String), nullable=True, default=list)
    certifications = Column(ARRAY(String), nullable=True, default=list)
    certifications_required = Column(ARRAY(String), nullable=True, default=list)
    certifications_preferred = Column(ARRAY(String), nullable=True, default=list)
    remote_policy = Column(String(20), nullable=True)  # remote | hybrid | onsite
    location = Column(String(255), nullable=True)

    # Salary structure
    salary_structure = Column(String(255), nullable=True)
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_currency = Column(String(3), nullable=True)
    salary_period = Column(String(10), nullable=True)
    compensation_breakdown = Column(JSON, nullable=True)
    
    # YOE
    years_of_experience = Column(Integer, nullable=True)

    # Source metadata
    url = Column(Text, nullable=True)
    source_tier = Column(Integer, nullable=False)
    source_name = Column(String(100), nullable=True)  # e.g. "greenhouse"

    # Scoring
    score = Column(Integer, nullable=True, default=0)
    is_dream_company = Column(Boolean, nullable=False, default=False)
    alerted = Column(Boolean, nullable=False, default=False)
    alerted_at = Column(DateTime, nullable=True)

    # Timestamps
    ingested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_canonical", "canonical_hash"),
        Index("idx_company_updated", "company", "updated_at"),
        Index("idx_score_alerted", "score", "alerted"),
        Index("idx_score_alerted_at", "score", "alerted_at"),
        Index("idx_source_tier_ingested", "source_tier", "ingested_at"),
    )

    def __repr__(self):
        return f"<Job {self.company} – {self.title}>"


class Source(Base):
    """
    Tracks the health/status of each scraping source.
    Used by the anomaly detector / self-healing system.
    """
    __tablename__ = "sources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(100), unique=True, nullable=False)
    tier = Column(Integer, nullable=False)
    url = Column(Text, nullable=True)

    # Stats
    last_success_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    consecutive_failures = Column(Integer, default=0)
    total_jobs_scraped = Column(Integer, default=0)
    is_stale = Column(Boolean, default=False)

    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Source {self.name} (Tier {self.tier})>"


class Alert(Base):
    """
    Audit log of every Telegram alert sent.
    """
    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=False)
    channel = Column(String(50), nullable=False, default="telegram")
    score = Column(Integer, nullable=True)
    sent_at = Column(DateTime, default=datetime.utcnow)
    delivered = Column(Boolean, default=True)

    job = relationship("Job", backref="alerts")

    def __repr__(self):
        return f"<Alert job={self.job_id} channel={self.channel}>"

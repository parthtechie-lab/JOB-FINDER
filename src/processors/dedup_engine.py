"""
Career Raider - Deduplication Engine (Upgraded)
Redis dual-dedup with PostgreSQL fallback.
"""
import hashlib
import redis
from src.config import get_settings
from src.logger import get_logger

log = get_logger("dedup")
settings = get_settings()
_redis = redis.from_url(settings.redis_url, decode_responses=True)

EXT_TTL = 30 * 24 * 3600   # 30 days
CAN_TTL = 60 * 24 * 3600   # 60 days


def generate_canonical_hash(title: str, company: str, location: str) -> str:
    raw = f"{title.lower().strip()}|{company.lower().strip()}|{location.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def is_duplicate(external_id: str, source_tier: int, title: str, company: str, location: str) -> bool:
    """
    Two-layer dedup:
      Layer 1 (μs): Redis external_id check  → same job from same API
      Layer 2 (μs): Redis canonical hash check → same job across platforms
    Returns True if already seen.
    """
    # Layer 1: External ID
    if external_id:
        ext_key = f"dedup:ext:{source_tier}:{external_id}"
        if _redis.exists(ext_key):
            log.debug("Dedup hit (external_id)", ext_id=external_id)
            return True

    # Layer 2: Canonical hash
    if title or company:
        can_hash = generate_canonical_hash(title, company, location)
        can_key = f"dedup:can:{can_hash}"
        if _redis.exists(can_key):
            log.debug("Dedup hit (canonical)", hash=can_hash[:16])
            return True

    return False


def mark_processed(external_id: str, source_tier: int, title: str, company: str, location: str):
    """Marks both keys in Redis after successfully writing to DB."""
    pipe = _redis.pipeline()

    if external_id:
        ext_key = f"dedup:ext:{source_tier}:{external_id}"
        pipe.setex(ext_key, EXT_TTL, "1")

    if title or company:
        can_hash = generate_canonical_hash(title, company, location)
        can_key = f"dedup:can:{can_hash}"
        pipe.setex(can_key, CAN_TTL, "1")

    pipe.execute()


def dedup_stats() -> dict:
    """Return stats about dedup cache size."""
    ext_count = len(_redis.keys("dedup:ext:*"))
    can_count = len(_redis.keys("dedup:can:*"))
    return {"external_id_entries": ext_count, "canonical_entries": can_count}

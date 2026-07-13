"""
Career Raider - Test Suite
All tests mock settings/Redis so they work without real credentials.
"""
import pytest
import hashlib
from unittest.mock import patch, MagicMock


# ─── Settings fixture: patches config module to avoid .env validation ──────────
@pytest.fixture(autouse=True)
def mock_settings():
    """
    Auto-applied to all tests. Provides a fake Settings object so tests
    never need real API keys or a running Redis/DB.
    """
    fake_settings = MagicMock()
    fake_settings.gemini_api_key = "test-key"
    fake_settings.telegram_bot_token = "test-token"
    fake_settings.telegram_chat_id = "12345"
    fake_settings.alert_channel = "telegram"
    fake_settings.smtp_host = "smtp.gmail.com"
    fake_settings.smtp_port = 587
    fake_settings.smtp_username = "test@gmail.com"
    fake_settings.smtp_password = "test-password"
    fake_settings.alert_email_recipient = "recipient@gmail.com"
    fake_settings.redis_url = "redis://localhost:6379/0"
    fake_settings.database_url = "postgresql://user:pass@localhost/testdb"
    fake_settings.gemini_batch_size = 20
    fake_settings.gemini_model = "gemini-2.0-flash"
    fake_settings.min_score_for_alert = 80
    fake_settings.min_salary_for_bonus = 150000

    with patch("src.config.get_settings", return_value=fake_settings), \
         patch("src.processors.dedup_engine.get_settings", return_value=fake_settings), \
         patch("src.processors.ai_router._get_redis", return_value=MagicMock()), \
         patch("src.processors.ai_router._get_model", return_value=None):
        yield fake_settings


# ─── Dedup Engine Tests ────────────────────────────────────────────────────────
class TestDedupEngine:
    def test_canonical_hash_consistent(self):
        from src.processors.dedup_engine import generate_canonical_hash
        h1 = generate_canonical_hash("Senior AppSec Engineer", "Stripe", "Remote")
        h2 = generate_canonical_hash("Senior AppSec Engineer", "Stripe", "Remote")
        assert h1 == h2

    def test_canonical_hash_case_insensitive(self):
        from src.processors.dedup_engine import generate_canonical_hash
        h1 = generate_canonical_hash("SENIOR APPSEC ENGINEER", "STRIPE", "REMOTE")
        h2 = generate_canonical_hash("senior appsec engineer", "stripe", "remote")
        assert h1 == h2

    def test_different_jobs_different_hashes(self):
        from src.processors.dedup_engine import generate_canonical_hash
        h1 = generate_canonical_hash("AppSec Engineer", "Stripe", "Remote")
        h2 = generate_canonical_hash("Infosec Engineer", "Stripe", "Remote")
        assert h1 != h2

    def test_hash_is_sha256(self):
        from src.processors.dedup_engine import generate_canonical_hash
        h = generate_canonical_hash("title", "company", "location")
        assert len(h) == 64  # SHA256 = 64 hex chars


# ─── Scorer Tests ─────────────────────────────────────────────────────────────
class TestScorer:
    def _make_job(self, company="Acme", remote="remote", salary_min=0, tech_stack=None, title="", yoe=None):
        job = MagicMock()
        job.company = company
        job.title = title
        job.remote_policy = remote
        job.salary_min = salary_min
        job.salary_max = None
        job.salary_low = None
        job.salary_high = None
        job.tech_stack = tech_stack or []
        job.years_of_experience = yoe
        job.tech_stack = tech_stack or []
        return job

    def test_dream_company_scores_100(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", ["stripe"]):
            job = self._make_job(company="Stripe")
            assert calculate_score(job) == 100

    def test_remote_appsec_high_salary_scores_high(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", []):
            job = self._make_job(remote="remote", salary_min=160_000, tech_stack=["appsec"])
            score = calculate_score(job)
            assert score >= 80

    def test_onsite_no_salary_scores_low(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", []):
            job = self._make_job(remote="onsite", salary_min=0, tech_stack=[])
            score = calculate_score(job)
            assert score <= 55

    def test_score_never_exceeds_99_for_non_dream(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", []):
            job = self._make_job(remote="remote", salary_min=200_000, tech_stack=["appsec", "cissp"])
            score = calculate_score(job)
            assert score <= 99

    def test_multiplicative_scoring_senior(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", []):
            job = self._make_job(title="Senior Security Engineer", salary_min=160_000)
            score = calculate_score(job)
            assert score < 20  # penalized heavily

    def test_multiplicative_scoring_fresher(self):
        from src.processors.scorer import calculate_score
        with patch("src.processors.scorer._DREAM_COMPANIES", []):
            job = self._make_job(title="Junior SOC Analyst", yoe=1)
            score = calculate_score(job)
            assert score > 80  # rewarded heavily


# ─── AI Router Pre-Processor Tests ───────────────────────────────────────────
class TestAIRouterPreProcessor:
    def test_extracts_salary_dollars(self):
        from src.processors.ai_router import _preprocess
        hints = _preprocess("Salary: $120,000 - $180,000 USD")
        assert hints["salary_low"] == 120000
        assert hints["salary_high"] == 180000

    def test_extracts_salary_k_notation(self):
        from src.processors.ai_router import _preprocess
        hints = _preprocess("We offer $120k - $160k")
        assert hints["salary_low"] == 120000
        assert hints["salary_high"] == 160000

    def test_extracts_remote_policy(self):
        from src.processors.ai_router import _preprocess
        hints = _preprocess("We are a fully remote team.")
        assert "remote" in (hints["remote_policy"] or "")

    def test_extracts_tech_stack(self):
        from src.processors.ai_router import _preprocess
        hints = _preprocess("You will work with AppSec, Kubernetes, and PostgreSQL.")
        tech = hints["tech_stack"]
        assert "appsec" in tech
        assert "kubernetes" in tech

    def test_no_false_positives_in_tech(self):
        from src.processors.ai_router import _preprocess
        hints = _preprocess("We value teamwork and communication.")
        assert len(hints["tech_stack"]) == 0

    def test_cache_key_deterministic(self):
        from src.processors.ai_router import _cache_key
        assert _cache_key("text A") == _cache_key("text A")
        assert _cache_key("text A") != _cache_key("text B")


# ─── AI Router ParsedJob Validation Tests ────────────────────────────────────
class TestParsedJobValidation:
    def test_remote_normalization_fully_remote(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob(remote_policy="Fully Remote")
        assert j.remote_policy == "remote"

    def test_remote_normalization_hybrid(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob(remote_policy="Hybrid - 2 days/week")
        assert j.remote_policy == "hybrid"

    def test_remote_normalization_onsite(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob(remote_policy="In-Office")
        assert j.remote_policy == "onsite"

    def test_tech_stack_lowercased(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob(tech_stack=["APPSEC", " CISSP ", "TypeScript"])
        assert "appsec" in j.tech_stack
        assert "cissp" in j.tech_stack
        assert "typescript" in j.tech_stack

    def test_currency_normalized(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob(currency="usd")
        assert j.currency == "USD"

    def test_empty_job_defaults(self):
        from src.processors.ai_router import ParsedJob
        j = ParsedJob()
        assert j.remote_policy == "unknown"
        assert j.currency == "USD"
        assert j.tech_stack == []


# ─── Tier 1 Raw Converter Tests ───────────────────────────────────────────────
class TestTier1Converter:
    def test_greenhouse_to_raw_maps_fields(self):
        with patch("src.processors.dedup_engine._redis", MagicMock()), \
             patch("src.ingesters.tier1_api.get_db_session", MagicMock()), \
             patch("src.ingesters.tier1_api.is_duplicate", return_value=False):
            from src.ingesters.tier1_api import _greenhouse_to_raw
            gh_job = {
                "id": 12345,
                "title": "Senior AppSec Engineer",
                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/12345",
                "location": {"name": "Remote"},
                "content": "We are looking for an AppSec engineer...",
                "updated_at": "2024-01-01T00:00:00Z",
            }
            raw = _greenhouse_to_raw(gh_job, "stripe")
            assert raw["external_id"] == "gh_12345"
            assert raw["title"] == "Senior AppSec Engineer"
            assert raw["source_tier"] == 1
            assert "Remote" in raw["location"]
            assert raw["source_name"] == "greenhouse"

    def test_lever_to_raw_maps_fields(self):
        from src.ingesters.tier1_api import _lever_to_raw
        lv_job = {
            "id": "abc-123",
            "text": "Infosec Engineer",
            "hostedUrl": "https://jobs.lever.co/netflix/abc-123",
            "categories": {"location": "San Francisco, CA"},
            "descriptionPlain": "We need an Infosec expert.",
        }
        raw = _lever_to_raw(lv_job, "netflix")
        assert raw["external_id"] == "lv_abc-123"
        assert raw["title"] == "Infosec Engineer"
        assert raw["source_tier"] == 1
        assert raw["location"] == "San Francisco, CA"


class TestAlerter:
    @patch("src.processors.alerter.requests.post")
    @patch("smtplib.SMTP")
    @patch("smtplib.SMTP_SSL")
    def test_alerter_routing_both(self, mock_smtp_ssl, mock_smtp, mock_post, mock_settings):
        from src.processors.alerter import _send_message
        
        mock_settings.alert_channel = "both"
        mock_settings.telegram_bot_token = "token"
        mock_settings.telegram_chat_id = "123"
        mock_settings.smtp_username = "user@gmail.com"
        mock_settings.alert_email_recipient = "rec@gmail.com"
        mock_settings.smtp_port = 587
        
        mock_post.return_value.status_code = 200
        
        success = _send_message("Test message content")
        
        assert success is True
        mock_post.assert_called_once()
        mock_smtp.assert_called_once()

    @patch("smtplib.SMTP")
    def test_alerter_routing_email_only(self, mock_smtp, mock_settings):
        from src.processors.alerter import _send_message
        
        mock_settings.alert_channel = "email"
        mock_settings.smtp_username = "user@gmail.com"
        mock_settings.alert_email_recipient = "rec@gmail.com"
        mock_settings.smtp_port = 587
        
        success = _send_message("Test message content")
        
        assert success is True
        mock_smtp.assert_called_once()


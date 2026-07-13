# Career Raider 🎯
> Industrial-grade job scraping, AI-processing, and multi-channel alerting system.
> Scrapes 5,000+ structured jobs/day — only alerts you for Cybersecurity, remote, $150k+ roles.

## Architecture

```
4-Tier Ingestion → Redis Dedup → Gemini AI (batch) → PostgreSQL → Telegram Alert
      ↓                                                               ↑
Anomaly Detector → OpenHands (Docker) → GitHub Draft PR → Approve? ─┘
```

## Quick Start

### 1. Configure secrets
```bash
cp .env .env.local
# Edit .env with your real API keys:
# GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

### 2. Start all services
```bash
docker compose up -d
```

### 3. Check health
```bash
curl http://localhost:9090/health     # Liveness
curl http://localhost:9090/ready      # Readiness (DB + Redis)
curl http://localhost:9090/stats      # Per-tier stats
curl http://localhost:9090/jobs?min_score=80  # High-score jobs
```

### 4. Manually trigger a tier
```bash
curl -X POST http://localhost:9090/trigger/1   # Tier 1 (Greenhouse/Lever)
curl -X POST http://localhost:9090/trigger/2   # Tier 2 (RSS/Sitemaps)
```

## Configuration

### Dream Companies (`config/target_companies.yaml`)
Any job from these companies scores **100** → instant Telegram alert.
```yaml
- "Stripe"
- "OpenAI"
- "Anthropic"
```

### API Sources (`config/sources.yaml`)
```yaml
greenhouse:
  - stripe
  - discord
lever:
  - netflix
```

## Services

| Service | Purpose | Queue |
|---------|---------|-------|
| `api-server` | Health/metrics/control | — |
| `celery-beat` | Schedules all jobs | — |
| `celery-worker-fast` | Tier 1+2+4, alerts | `fast_queue` |
| `celery-worker-heavy` | Tier 3 (Playwright+IMAP) | `heavy_queue` |
| `metrics-exporter` | Prometheus `:9091` | — |

## Poll Intervals

| Tier | Source | Interval |
|------|--------|----------|
| 1 | Greenhouse + Lever APIs | 60s |
| 2 | RSS Feeds + Sitemaps | 5m |
| 3 | Playwright + LinkedIn IMAP | 15m |
| 4 | Telegram Channels | 5m |

## Zero-Hallucination AI Design
- `temperature=0` on Gemini (deterministic)
- Strict JSON schema enforcement (`response_mime_type="application/json"`)
- Pre-processing via Regex before LLM (salary, tech, remote)
- Pydantic V2 validation layer after LLM
- Redis result caching (7 days) — identical text skips Gemini entirely
- Retry with exponential backoff (max 3 attempts)

## Self-Healing
When a source goes stale (0 jobs in 24h):
1. Anomaly detector fires
2. OpenHands runs in isolated Docker (read-only mount)
3. Generates a unified diff
4. `git apply --check` validates the patch
5. GitHub Draft PR created
6. **YOU** approve/reject via Telegram inline button

## Run Tests
```bash
poetry run pytest tests/ -v
```

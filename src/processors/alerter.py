"""
Career Raider - Telegram Alerter
- Sends instant alerts for high-score and dream-company jobs
- 6-hour health report with per-tier stats
- Approve/reject inline keyboard for self-healing PRs
"""
import requests
from datetime import datetime, timedelta

from src.config import get_settings
from src.logger import get_logger
from src.models.database import get_db_session
from src.models.job import Job, Alert, Source

log = get_logger("alerter")
settings = get_settings()

TELEGRAM_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


def _determine_subject(text: str) -> str:
    import re
    # Strip HTML tags to find the first line for the subject
    clean_text = re.sub(r'<[^>]+>', '', text).strip()
    lines = [line.strip() for line in clean_text.split('\n') if line.strip()]
    if lines:
        return lines[0][:100]
    return "Career Raider Alert"


def _send_telegram(text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
    chat_id = settings.telegram_chat_id
    if not chat_id:
        log.warning("TELEGRAM_CHAT_ID not set, skipping Telegram alert")
        return False

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": False,
    }
    if reply_markup:
        import json
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram send failed", error=str(e))
        return False


def _send_email(text: str) -> bool:
    import smtplib
    import re
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    if not settings.smtp_username or not settings.alert_email_recipient:
        log.warning("Email settings not fully configured, skipping email alert")
        return False

    subject = _determine_subject(text)
    
    # Premium card HTML layout with system fonts
    html_body = f"""
    <html>
      <head>
        <style>
          body {{
            font-family: 'Outfit', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background-color: #f8fafc;
            color: #1e293b;
            padding: 20px;
            margin: 0;
          }}
          .card {{
            background: #ffffff;
            border: 1px solid #e2e8f0;
            border-radius: 12px;
            padding: 24px;
            max-width: 600px;
            margin: 0 auto;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -2px rgba(0, 0, 0, 0.05);
          }}
          .footer {{
            text-align: center;
            font-size: 12px;
            color: #64748b;
            margin-top: 16px;
          }}
          a {{
            color: #2563eb;
            text-decoration: none;
            font-weight: 500;
          }}
          a:hover {{
            text-decoration: underline;
          }}
          pre, code {{
            background-color: #f1f5f9;
            padding: 2px 6px;
            border-radius: 4px;
            font-family: monospace;
            font-size: 14px;
          }}
          pre {{
            padding: 12px;
            overflow-x: auto;
          }}
        </style>
      </head>
      <body>
        <div class="card">
          {text.replace('\n', '<br>')}
        </div>
        <div class="footer">
          Career Raider Notification System &bull; {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
        </div>
      </body>
    </html>
    """
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.smtp_username
    msg["To"] = settings.alert_email_recipient
    
    plain_text = re.sub(r'<[^>]+>', '', text)
    
    part1 = MIMEText(plain_text, "plain")
    part2 = MIMEText(html_body, "html")
    msg.attach(part1)
    msg.attach(part2)
    
    try:
        if settings.smtp_port == 465:
            server = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)
        else:
            server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
            if settings.smtp_port == 587:
                server.starttls()
        
        if settings.smtp_password:
            server.login(settings.smtp_username, settings.smtp_password)
            
        server.sendmail(settings.smtp_username, settings.alert_email_recipient, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        log.error("Email send failed", error=str(e))
        return False


def _send_message(text: str, parse_mode: str = "HTML", reply_markup: dict = None) -> bool:
    channel = (settings.alert_channel or "telegram").lower()
    
    telegram_success = True
    email_success = True
    
    if channel in ("telegram", "both"):
        telegram_success = _send_telegram(text, parse_mode, reply_markup)
        
    if channel in ("email", "both"):
        modified_text = text
        if reply_markup and "inline_keyboard" in reply_markup:
            buttons_html = []
            for row in reply_markup["inline_keyboard"]:
                for btn in row:
                    if "url" in btn:
                        buttons_html.append(f"<a href='{btn['url']}'>{btn['text']}</a>")
            if buttons_html:
                modified_text += "\n\n<b>Actions:</b>\n" + " | ".join(buttons_html)
        
        email_success = _send_email(modified_text)
        
    if channel == "telegram":
        return telegram_success
    elif channel == "email":
        return email_success
    else:  # both
        return telegram_success or email_success


def _format_salary(job: Job) -> str:
    if not job.salary_min:
        return "💰 Not disclosed"
    parts = [f"${job.salary_min:,}"]
    if job.salary_max:
        parts.append(f"${job.salary_max:,}")
    period = f" / {job.salary_period.lower()}" if job.salary_period else ""
    return "💰 " + " – ".join(parts) + f" {job.salary_currency or 'USD'}{period}"


def _format_tech(job: Job) -> str:
    lines = []
    if job.tech_stack:
        lines.append("🔧 " + " · ".join(job.tech_stack[:8]))
    if job.certifications_required:
        lines.append("📜 Req: " + ", ".join(job.certifications_required))
    if not lines:
        return "🔧 Not specified"
    return "\n".join(lines)


def _build_job_alert(job: Job) -> str:
    remote_emoji = {"remote": "🏠", "hybrid": "🔄", "onsite": "🏢"}.get(job.remote_policy or "", "📍")
    dream_badge = "⭐ DREAM COMPANY! " if job.is_dream_company else ""

    lines = [
        f"{'🔥' if job.score >= 90 else '✅'} <b>{dream_badge}NEW JOB ALERT</b>",
        f"",
        f"🏢 <b>{job.company}</b>",
        f"💼 {job.title}",
        f"{remote_emoji} {(job.remote_policy or 'unknown').title()} | {job.location or 'Unknown Location'}",
        f"{_format_salary(job)}",
        f"{_format_tech(job)}",
        f"📊 Score: <b>{job.score}/100</b>",
        f"",
        f"🔗 <a href=\"{job.url}\">Apply Now</a>",
        f"",
        f"<i>Tier {job.source_tier} | {job.source_name} | {job.ingested_at.strftime('%H:%M UTC')}</i>",
    ]
    return "\n".join(lines)


def alert_high_score_jobs():
    """Send alerts for any unalerted jobs above min_score threshold."""
    min_score = settings.min_score_for_alert
    channel = (settings.alert_channel or "telegram").lower()
    
    with get_db_session() as session:
        jobs = (
            session.query(Job)
            .filter(Job.score >= min_score, Job.alerted_at == None)
            .order_by(Job.score.desc(), Job.ingested_at.desc())
            .limit(10)
            .all()
        )
        for job in jobs:
            text = _build_job_alert(job)
            if channel in ("telegram", "both"):
                delivered = _send_telegram(text)
                alert = Alert(job_id=job.id, channel="telegram", score=job.score, delivered=delivered)
                session.add(alert)
            if channel in ("email", "both"):
                delivered = _send_email(text)
                alert = Alert(job_id=job.id, channel="email", score=job.score, delivered=delivered)
                session.add(alert)
            job.alerted_at = datetime.utcnow()

    log.info("Alerts sent", count=len(jobs))

def send_daily_job_summary():
    from datetime import timezone
    min_score = 60
    # Current time in UTC
    now_utc = datetime.now(timezone.utc)
    # Convert to IST to find "today"
    ist_offset = timedelta(hours=5, minutes=30)
    now_ist = now_utc + ist_offset
    start_of_day_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day_ist = start_of_day_ist + timedelta(days=1)
    
    start_utc = start_of_day_ist - ist_offset
    end_utc = end_of_day_ist - ist_offset

    # DB uses naive UTC datetime
    start_utc_naive = start_utc.replace(tzinfo=None)
    end_utc_naive = end_utc.replace(tzinfo=None)

    with get_db_session() as session:
        jobs = (
            session.query(Job)
            .filter(Job.score >= min_score)
            .filter(Job.ingested_at >= start_utc_naive)
            .filter(Job.ingested_at < end_utc_naive)
            .filter(Job.alerted_at == None)
            .order_by(Job.score.desc(), Job.ingested_at.desc())
            .limit(10)
            .all()
        )
        
        if not jobs:
            log.info("No new jobs for daily summary")
            return
            
        lines = [f"📊 <b>Daily Fresher Summary ({now_ist.strftime('%Y-%m-%d')})</b>", ""]
        for j in jobs:
            lines.append(f"• <b>{j.company}</b>: {j.title} (Score: {j.score})")
            lines.append(f"  <a href=\"{j.url}\">Apply Here</a>")
            j.alerted_at = datetime.utcnow()
            
        total_count = session.query(Job).filter(Job.score >= min_score, Job.ingested_at >= start_utc_naive, Job.ingested_at < end_utc_naive, Job.alerted_at == None).count()
        if total_count > 10:
            lines.append("")
            lines.append(f"<i>...and {total_count - 10} more. View all {total_count} jobs on the dashboard.</i>")
            
        text = "\n".join(lines)
        _send_message(text)
        log.info("Daily summary sent", count=len(jobs))


def send_telegram_health_report():
    """Send a 6-hour health report with per-tier stats."""
    since = datetime.utcnow() - timedelta(hours=6)
    with get_db_session() as session:
        total = session.query(Job).filter(Job.ingested_at >= since).count()
        tier_counts = {}
        for tier in range(1, 5):
            c = session.query(Job).filter(Job.source_tier == tier, Job.ingested_at >= since).count()
            tier_counts[tier] = c

        sources = session.query(Source).all()
        stale_sources = [s.name for s in sources if s.is_stale]
        failing_sources = [s for s in sources if s.consecutive_failures >= 3]

    tier_lines = []
    tier_emoji = {1: "🥇", 2: "🥈", 3: "🥉", 4: "📡"}
    for t, count in tier_counts.items():
        emoji = tier_emoji.get(t, "")
        status = "✅" if count > 0 else "⚠️"
        tier_lines.append(f"  {status} {emoji} Tier {t}: <b>{count}</b> new jobs")

    stale_line = ""
    if stale_sources:
        stale_line = f"\n⚠️ Stale sources: {', '.join(stale_sources)}"
    if failing_sources:
        stale_line += f"\n🔴 Failing (3+ errors): {', '.join(s.name for s in failing_sources)}"

    report = (
        f"🩺 <b>Career Raider — Health Report</b>\n"
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC\n\n"
        f"📥 <b>New jobs (last 6h): {total}</b>\n"
        + "\n".join(tier_lines)
        + stale_line
        + f"\n\n<i>System running normally ✅</i>"
    )
    _send_message(report)


def send_self_healing_pr_alert(pr_url: str, source_name: str, patch_summary: str):
    """
    Alert user that a self-healing PR is ready. Includes YES/NO inline buttons.
    """
    text = (
        f"🛠️ <b>Self-Healing PR Ready</b>\n\n"
        f"Source: <b>{source_name}</b>\n"
        f"Summary: {patch_summary[:300]}\n\n"
        f"🔗 <a href=\"{pr_url}\">View Pull Request</a>\n\n"
        f"Tap to approve or reject merge:"
    )
    reply_markup = {
        "inline_keyboard": [[
            {"text": "✅ YES — Merge PR", "callback_data": f"approve_pr:{pr_url}"},
            {"text": "❌ NO — Close PR", "callback_data": f"reject_pr:{pr_url}"},
        ]]
    }
    _send_message(text, reply_markup=reply_markup)

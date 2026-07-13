"""
Career Raider - Scorer (Upgraded)
Dynamic priority scoring with dream-company fast-track.
"""
import yaml
from src.logger import get_logger

log = get_logger("scorer")

_DREAM_COMPANIES: list[str] = []

def _load_dream_companies():
    global _DREAM_COMPANIES
    try:
        with open("config/target_companies.yaml") as f:
            data = yaml.safe_load(f) or []
        _DREAM_COMPANIES = [c.lower().strip() for c in data if isinstance(c, str)]
        log.info("Dream companies loaded", count=len(_DREAM_COMPANIES))
    except FileNotFoundError:
        log.warning("config/target_companies.yaml not found")
        _DREAM_COMPANIES = []

_load_dream_companies()

# Premium tech keywords that score higher
PREMIUM_TECH = {"cissp", "appsec", "soc", "siem", "incident response", "penetration testing", "vulnerability management", "oscp", "cism", "ceh"}
GOOD_TECH = {"infosec", "network security", "cloud security", "iam", "threat hunting", "grc", "security engineer"}

SENIOR_KEYWORDS = {"senior", "lead", "staff", "principal", "director", "manager", "head", "vp"}
FRESHER_KEYWORDS = {"junior", "fresher", "entry level", "new grad", "intern", "0-2 years", "associate", "graduate", "trainee", "apprentice", "analyst i", "analyst 1"}


def calculate_score(job) -> int:
    """
    Score = 0-100. Returns 100 for dream companies (instant alert bypass).
    """
    # Dream company = instant 100
    company = (job.company or "").lower().strip()
    if company in _DREAM_COMPANIES:
        log.info("Dream company match!", company=job.company, title=job.title)
        return 100

    base = 50

    # YOE logic
    yoe = job.years_of_experience
    if yoe is not None:
        if yoe <= 2:
            base += 50
        elif yoe >= 5:
            base -= 80

    # Remote policy
    remote = (job.remote_policy or "").lower()
    if remote == "remote":
        base += 20
    elif remote == "hybrid":
        base += 8

    # Salary
    salary_min = job.salary_min or job.salary_low
    if salary_min and salary_min >= 150_000:
        base += 15
    elif salary_min and salary_min >= 100_000:
        base += 8

    # Tech stack
    tech = set(t.lower() for t in (job.tech_stack or []))
    if tech & PREMIUM_TECH:
        base += 15
    elif tech & GOOD_TECH:
        base += 8

    title = (job.title or "").lower()
    
    # Fresher rules: multiplicative multipliers
    multiplier = 1.0
    if any(k in title for k in SENIOR_KEYWORDS):
        multiplier = 0.1
    elif any(k in title for k in FRESHER_KEYWORDS):
        multiplier = 2.0

    final_score = int(base * multiplier)

    return min(final_score, 99)  # 100 reserved for dream companies only


def reload_dream_companies():
    """Hot-reload the dream company list without restart."""
    _load_dream_companies()

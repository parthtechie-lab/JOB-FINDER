"""
Career Raider - Self-Healing: Patch Generator
SAFE pull-request model — NEVER auto-merges.
1. Spins up OpenHands in Docker (isolated, read-only code mount)
2. Injects error context + page snapshot
3. OpenHands outputs a unified diff
4. git_ops.py creates a Draft PR
5. Telegram alert: YES (merge) / NO (close)
"""
import os
import subprocess
import tempfile
from datetime import datetime

from src.logger import get_logger
from src.processors.alerter import send_self_healing_pr_alert

log = get_logger("patch_generator")


def generate_patch(source_name: str, incident_path: str = None):
    """
    Main entry point — called when a source is detected as stale or encounters a critical error.
    """
    log.info("Generating self-healing patch", source=source_name, incident_path=incident_path)

    # Get recent error logs for this source
    error_context = _get_error_context(source_name)
    
    incident_context = ""
    if incident_path and os.path.exists(incident_path):
        with open(incident_path, "r") as f:
            incident_context = f"CRITICAL INCIDENT REPORT:\n{f.read()}\n\n"

    # Build the OpenHands prompt
    prompt = (
        f"You are a web scraper and system repair expert.\n"
        f"The system '{source_name}' has encountered an issue or is returning 0 results.\n\n"
        f"{incident_context}"
        f"Recent general error logs:\n{error_context}\n\n"
        f"Task: Investigate the issue using the provided tracebacks and logs. Fix the broken logic (CSS selectors, API logic, etc).\n"
        f"Output ONLY a unified diff (git diff format). Do not explain. Just output the patch."
    )

    # Try OpenHands via Docker
    patch_content = _run_openhands(prompt, source_name)

    if not patch_content:
        log.warning("Patch generation produced no output", source=source_name)
        return

    # Validate patch can apply cleanly
    if not _validate_patch(patch_content):
        log.error("Patch validation failed, not creating PR", source=source_name)
        return

    # Create GitHub PR
    pr_url = _create_github_pr(patch_content, source_name)
    if pr_url:
        send_self_healing_pr_alert(
            pr_url=pr_url,
            source_name=source_name,
            patch_summary=f"Auto-generated fix for {source_name} — selector/parsing repair"
        )
    else:
        log.error("PR creation failed", source=source_name)


def _get_error_context(source_name: str) -> str:
    """Read last 100 lines of logs for this source."""
    log_paths = [
        f"/var/log/career_raider/{source_name}.log",
        f"logs/{source_name}.log",
    ]
    for path in log_paths:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    lines = f.readlines()
                return "".join(lines[-100:])
            except Exception:
                pass
    return f"No recent error logs found for {source_name}"


def _run_openhands(prompt: str, source_name: str) -> str:
    """
    Runs OpenHands in a Docker container (isolated, read-only mount).
    Returns the patch content if successful.
    """
    openhands_image = os.getenv("OPENHANDS_IMAGE", "ghcr.io/all-hands-ai/openhands:latest")
    api_key = os.getenv("OPENHANDS_API_KEY", "")
    codebase_path = os.path.abspath(".")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(prompt)
        prompt_file = f.name

    output_file = tempfile.mktemp(suffix=".patch")

    try:
        cmd = [
            "docker", "run", "--rm",
            "--read-only",                          # Read-only code mount
            "-v", f"{codebase_path}:/workspace:ro",
            "-v", f"{prompt_file}:/prompt.txt:ro",
            "-v", f"{os.path.dirname(output_file)}:/output",
            "-e", f"OPENHANDS_API_KEY={api_key}",
            openhands_image,
            "solve", "/prompt.txt", "--output", f"/output/{os.path.basename(output_file)}",
        ]

        log.info("Running OpenHands container", source=source_name)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            log.error("OpenHands container failed", stderr=result.stderr[:500])
            return ""

        if os.path.exists(output_file):
            with open(output_file) as f:
                return f.read()
        return result.stdout

    except subprocess.TimeoutExpired:
        log.error("OpenHands timed out (300s)")
        return ""
    except FileNotFoundError:
        log.warning("Docker not found — OpenHands skipped")
        return ""
    except Exception as e:
        log.error("OpenHands run error", error=str(e))
        return ""
    finally:
        for path in [prompt_file, output_file]:
            if os.path.exists(path):
                os.unlink(path)


def _validate_patch(patch_content: str) -> bool:
    """Run git apply --check to validate the patch before creating a PR."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".diff", delete=False) as f:
        f.write(patch_content)
        patch_file = f.name

    try:
        result = subprocess.run(
            ["git", "apply", "--check", patch_file],
            capture_output=True, text=True, cwd=".",
        )
        if result.returncode == 0:
            log.info("Patch validation passed")
            return True
        else:
            log.error("Patch validation failed", stderr=result.stderr)
            return False
    except Exception as e:
        log.error("Patch validation error", error=str(e))
        return False
    finally:
        os.unlink(patch_file)


def _create_github_pr(patch_content: str, source_name: str) -> str:
    """Create a Draft PR on GitHub using the GitHub CLI."""
    from src.self_healing.git_ops import create_pr
    return create_pr(patch_content, source_name)

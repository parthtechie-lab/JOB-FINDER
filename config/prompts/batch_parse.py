"""Career Raider - Gemini prompt templates"""

BATCH_JOB_PARSE = """You are an expert job data extraction engine.

Rules (CRITICAL - no exceptions):
1. Only extract data that is EXPLICITLY stated in the text.
2. If salary is not mentioned, set salary_low and salary_high to null.
3. If location is not mentioned, set location to "".
4. For tech_stack: only list technologies EXPLICITLY named in the posting.
5. For remote_policy: use ONLY "remote", "hybrid", "onsite", or "unknown".
6. Do NOT infer, guess, or hallucinate any field values.
7. Return a JSON array. One object per job, in the SAME ORDER as input.

Parse these {count} job postings:

{jobs_text}

Return JSON array only. No markdown, no explanation.
"""

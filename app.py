"""
Investment Thesis & FinTech Code Auditor
----------------------------------------
Dashboard (not a chatbot) with two audit modes:
  - Business Pitch: VC-style scoring of a startup pitch
  - Technical Code: security/bug audit of a code snippet, with a fixed version
Both modes search Hindsight Cloud memory for similar past deals or bugs first,
and automatically save what they learn back to memory afterwards.

.env (next to app.py):
    HINDSIGHT_API_KEY=your_hindsight_kry_here
    GROQ_API_KEY=your_groq_key_here

Run:
    pip install -r requirements.txt
    streamlit run app.py
"""

import streamlit as st
import json
import os
import asyncio
import signal
import sys
import time
import difflib
import shlex
import shutil
import subprocess
import tempfile
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import certifi
from dotenv import load_dotenv
from groq import Groq
from hindsight_client import Hindsight

load_dotenv()

# Fixes "CERTIFICATE_VERIFY_FAILED" on Macs where Python has no root certificates
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

# ----------------------------- Configuration -----------------------------

HINDSIGHT_URL = "https://api.hindsight.vectorize.io"
BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "investment-thesis-auditor")
# llama-3.3-70b-versatile was retired by Groq on 16 Aug 2026; these are tried in order.
GROQ_MODELS = [m for m in [os.getenv("GROQ_MODEL"), "openai/gpt-oss-120b",
                           "openai/gpt-oss-20b", "qwen/qwen3.6-27b"] if m]
APP_DIR = Path(__file__).resolve().parent  # find data files next to app.py, whatever the terminal folder
DATA_FILES = ["past_investments.json", "past-investments.json"]  # accept either spelling
BUGS_FILES = ["past_code_bugs.json", "past-code-bugs.json"]

MODE_BUSINESS = "Business Pitch"
MODE_CODE = "Technical Code"
TASK_REVIEW = "Code Review"
TASK_HEAL = "Crash Self-Heal"
TASK_MONITOR = "Night Watch (Auto-Monitor)"
AUTOHEAL_SCRIPT = APP_DIR / "autoheal.py"
PID_FILE = APP_DIR / ".autoheal.pid"
WATCH_LOG = APP_DIR / "autoheal_watch.log"
STATUS_LABELS = {
    "fix_verified_saved": "✅ Fix verified and saved to memory",
    "fix_verified": "✅ Fix verified and saved to memory",
    "fix_applied": "🔧 Fix applied, verifying",
    "fix_applied_unverified": "🔧 Fix applied (not verified)",
    "dry_run": "🧪 Dry run: no files changed",
    "proposed_only": "🧪 Fix proposed only",
    "no_similar_bug": "🛑 No similar past bug: needs a human",
    "no_match": "🛑 No genuine match: needs a human",
    "low_confidence": "🛑 Confidence too low: needs a human",
    "fix_rejected": "🛑 Fix rejected: needs a human",
    "empty_fix": "🛑 Empty fix: needs a human",
    "invalid_fix": "🛑 Invalid fix: needs a human",
    "fix_failed_rolled_back": "↩️ Fix failed, original restored",
    "tests_failed_rolled_back": "↩️ Tests failed, original restored",
    "memory_unavailable": "⚠️ Memory unavailable: not fixing blindly",
    "groq_failed": "⚠️ Groq unavailable",
}
INCIDENT_LOG = APP_DIR / "autoheal_incidents.jsonl"
BACKUP_DIR = APP_DIR / ".autoheal_backups"

HEAL_SYSTEM = """You are an autonomous site reliability engineer. A production service just \
crashed. You receive the CRASH TRACEBACK, the CURRENT SOURCE CODE, and HISTORICAL MEMORY: \
records of similar bugs this team resolved before, with their fixes.

Rules:
- Only treat the crash as a known problem if the historical memory contains a genuinely similar \
bug. If it does not, set "similar_bug_matches" to false and leave "fixed_code" empty.
- If it matches, apply the SAME KIND of fix that worked before, with the smallest change that \
stops the crash. Do not refactor, rename or change unrelated behaviour.
- "fixed_code" must be the COMPLETE corrected source, not a diff or a snippet.
- "confidence" is an integer 1-10: how sure you are that this fix removes the crash without \
breaking anything else.
- Never invent past bugs. "matched_bug" and "past_financial_impact" must be copied from the \
historical memory ("Not stated" if the memory gives no figure).

Return ONLY a valid JSON object with exactly this shape and no other text:
{
  "similar_bug_matches": <true or false>,
  "matched_bug": "<title of the past bug>",
  "past_financial_impact": "<financial damage of the past bug, or 'Not stated'>",
  "confidence": <int 1-10>,
  "root_cause": "<one or two sentences>",
  "explanation": "<what the fix changes and why>",
  "fixed_code": "<the complete corrected source code>"
}"""

BUSINESS_SCHEMA = """{
  "scores": {"Team": <int 1-10>, "Market": <int 1-10>, "Product": <int 1-10>},
  "verdict": "<short 1-2 sentence verdict>",
  "business_red_flags": ["<specific red flag>", "<specific red flag>"],
  "historical_loss": "<financial impact of past similar failures, or 'None found'>",
  "similar_idea_found": <true or false>,
  "similar_past_cases": [
    {
      "name": "<name of the past investment, exactly as in the historical context>",
      "similarity": "<what is similar to the new pitch>",
      "scope": "<sector, market scope and how far that past idea got>",
      "outcome": "<Loss, Profit, Open or Unknown>",
      "financial_result": "<amount and/or percentage lost or gained, exactly as in the context>",
      "lesson": "<what that case teaches about this pitch>"
    }
  ]
}"""

CODE_SCHEMA = """{
  "security_score": <int 1-10, where 10 is fully secure>,
  "code_bugs": ["<specific bug or vulnerability>", "<specific bug or vulnerability>"],
  "historical_loss": "<past financial damage from this kind of vulnerability, or 'None found'>",
  "auto_fixed_code": "<the fully corrected, secure code snippet>",
  "similar_idea_found": <true or false>,
  "similar_past_bugs": [
    {
      "title": "<title of the past bug, exactly as in the historical context>",
      "similarity": "<what is similar to the new code>",
      "impact": "<what that bug caused>",
      "financial_result": "<financial damage, exactly as in the context, or 'Not stated'>",
      "fix_summary": "<how it was fixed>"
    }
  ]
}"""

BUSINESS_SYSTEM = """You are a venture capitalist auditing a startup pitch. \
VC/Engineer Strictness Level: {STRICTNESS}/10. {TONE}

Scoring: score "Team", "Market" and "Product" as integers from 1 to 10. At strictness 10, \
5 is already generous and 8+ needs hard evidence in the pitch. At strictness 1, be \
encouraging and give credit for potential.

Never invent past investments, companies or financial figures that are not in the \
historical context. "historical_loss" must come from the historical context, or be \
exactly "None found".

Similar-case rules:
- Check the historical context for past investments (or past bugs) similar to the new input. \
If you find any, set "similar_idea_found" to true and describe each one in full: what is similar, \
its scope, whether it ended in Loss or Profit, and the exact financial result.
- Records starting with "Business audit learning" or "Resolved code bug learning" are earlier \
AUDITS, not real investments. Never present them as profit or loss unless a financial result is \
explicitly stated in them.
- If nothing in the context is genuinely similar, set "similar_idea_found" to false and the \
similar list to an empty array. Do not stretch to find a match.
- Copy names and numbers exactly from the context. Never invent them.

Return ONLY a valid JSON object with exactly this shape and no other text:
""" + BUSINESS_SCHEMA

CODE_SYSTEM = """You are a senior FinTech security engineer auditing code. \
VC/Engineer Strictness Level: {STRICTNESS}/10. {TONE}

Scoring: "security_score" is an integer from 1 to 10 where 10 is fully secure. At \
strictness 10, treat any unvalidated input, float money math, missing auth check, hardcoded \
secret or race condition as serious. At strictness 1, flag only critical problems.

"auto_fixed_code" must be the complete corrected code, not a diff or a description. \
Never invent past incidents or financial figures that are not in the historical context. \
"historical_loss" must come from the historical context, or be exactly "None found".

Similar-case rules:
- Check the historical context for past investments (or past bugs) similar to the new input. \
If you find any, set "similar_idea_found" to true and describe each one in full: what is similar, \
its scope, whether it ended in Loss or Profit, and the exact financial result.
- Records starting with "Business audit learning" or "Resolved code bug learning" are earlier \
AUDITS, not real investments. Never present them as profit or loss unless a financial result is \
explicitly stated in them.
- If nothing in the context is genuinely similar, set "similar_idea_found" to false and the \
similar list to an empty array. Do not stretch to find a match.
- Copy names and numbers exactly from the context. Never invent them.

Return ONLY a valid JSON object with exactly this shape and no other text:
""" + CODE_SCHEMA

CONTEXT_FOUND = (
    "HISTORICAL CONTEXT FOUND. Compare the new input against these past "
    "failures/successes, and name the specific past records it resembles."
)
COLD_START = (
    "NO HISTORICAL CONTEXT FOUND. Deeply analyze the standalone fundamentals "
    "{what} on its own merits."
)


def strictness_tone(level: int) -> str:
    if level <= 3:
        return "Tone: supportive and constructive. Flag only major problems."
    if level <= 6:
        return "Tone: balanced and professional. Credit what is good, call out real weaknesses."
    if level <= 8:
        return "Tone: skeptical and demanding. Assume claims are unproven until shown."
    return "Tone: ruthless. Assume it will fail, challenge everything, do not soften criticism."


# ----------------------------- Cached clients -----------------------------

def _env(name: str) -> str:
    """Read a key from the environment, ignoring stray spaces or quotes from .env."""
    return os.getenv(name, "").strip().strip("\"'").strip()


@st.cache_resource
def get_hindsight_config() -> dict:
    api_key = _env("HINDSIGHT_API_KEY")
    if not api_key:
        raise RuntimeError("HINDSIGHT_API_KEY is missing from your .env file.")
    return {"base_url": HINDSIGHT_URL, "api_key": api_key, "timeout": 60.0}


def run_hindsight(fn):
    """Run fn(client) in its own thread with its own event loop. The Hindsight client is
    async under the hood, and calling it directly from Streamlit's thread fails with
    'event loop is closed / already running'."""
    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return fn(get_hindsight_client())
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(worker).result()


def get_hindsight_client() -> Hindsight:
    """Fresh client per call. The Hindsight client runs on an asyncio event loop that
    Streamlit closes between reruns, so a cached instance fails with 'Event loop is
    closed'. Only its config is cached."""
    return Hindsight(**get_hindsight_config())


@st.cache_resource
def get_groq_client() -> Groq:
    api_key = _env("GROQ_API_KEY")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is missing from your .env file.")
    return Groq(api_key=api_key, timeout=30.0, max_retries=2)


# ----------------------------- Input handling -----------------------------

def extract_text_from_upload(uploaded_file) -> str:
    """Read text from an uploaded .txt or .pdf file."""
    if uploaded_file.name.lower().endswith(".pdf"):
        from pypdf import PdfReader  # imported here so .txt users don't need it

        reader = PdfReader(uploaded_file)
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    return uploaded_file.read().decode("utf-8", errors="ignore").strip()


# ----------------------------- Hindsight memory -----------------------------

def _is_missing_bank(err: Exception) -> bool:
    msg = str(err).lower()
    return "404" in msg or "not found" in msg


def ensure_bank(client: Hindsight) -> None:
    """Create the memory bank if it does not exist yet."""
    try:
        try:
            client.create_bank(bank_id=BANK_ID, name=BANK_ID)
        except TypeError:
            client.create_bank(bank_id=BANK_ID)
    except Exception as e:
        msg = str(e).lower()
        if "exist" in msg or "409" in msg or "conflict" in msg:
            return  # bank is already there
        raise


MAX_MEMORIES = 12
MAX_MEMORY_CHARS = 700


def prepare_memories(texts: list[str], limit: int = MAX_MEMORIES, chars: int = MAX_MEMORY_CHARS) -> list[str]:
    """Deduplicate and cap recalled memories so the Groq prompt stays small."""
    seen, out = set(), []
    for t in texts:
        t = " ".join(str(t).split())
        key = t.lower()
        if not t or key in seen:
            continue
        seen.add(key)
        out.append(t[:chars])
        if len(out) >= limit:
            break
    return out


def recall_similar(client: Hindsight, text: str, mode: str) -> list[str]:
    """Search Hindsight for past similar deals (business) or bugs (code)."""
    prefix = (
        "Past similar startup investments, failures and financial losses: "
        if mode == MODE_BUSINESS
        else "Past similar code bugs, security vulnerabilities and their fixes: "
    )
    try:
        response = client.recall(bank_id=BANK_ID, query=prefix + text[:2000])
    except Exception as e:
        if _is_missing_bank(e):  # first run: bank not created yet, so no history
            ensure_bank(client)
            return []
        raise
    return prepare_memories([r.text for r in response.results])


def save_learnings(client: Hindsight, mode: str, input_text: str, audit: dict) -> None:
    """Autonomously store the flaws, bugs and fixes from this audit."""
    ensure_bank(client)
    snippet = " ".join(input_text.split())[:400]
    if mode == MODE_BUSINESS:
        s = audit["scores"]
        content = (
            "Business audit learning (an evaluated pitch, NOT an actual investment).\n"
            f"Pitch excerpt: {snippet}\n"
            f"Scores: Team {s['Team']}, Market {s['Market']}, Product {s['Product']}.\n"
            f"Red flags: {'; '.join(audit['business_red_flags']) or 'none'}.\n"
            f"Historical loss: {audit['historical_loss']}\n"
            f"Verdict: {audit['verdict']}"
        )
        context = "Historical Business Loss"
    else:
        content = (
            "Resolved code bug learning (from a code audit).\n"
            f"Code excerpt: {snippet}\n"
            f"Security score: {audit['security_score']}/10.\n"
            f"Bugs found: {'; '.join(audit['code_bugs']) or 'none'}.\n"
            f"Historical loss: {audit['historical_loss']}\n"
            f"Fix applied:\n{audit['auto_fixed_code'][:1500]}"
        )
        context = "Resolved Code Bug"
    client.retain(bank_id=BANK_ID, content=content, context=context)


def investment_to_text(inv: dict) -> str:
    """Turn one past-investment record into a self-contained narrative."""
    thesis = inv.get("thesis", {})
    entry = inv.get("entry", {})
    exit_ = inv.get("exit")
    outcome = inv.get("outcome", {})
    held = outcome.get("assumptionsHeld", [])

    assumptions = []
    for i, a in enumerate(thesis.get("keyAssumptions", [])):
        status = ""
        if i < len(held):
            status = " (HELD)" if held[i] else " (FAILED)"
        assumptions.append(f"- {a}{status}")

    lines = [
        f"Past investment {inv.get('id')}: {inv.get('asset')} ({inv.get('ticker')}), "
        f"sector {inv.get('sector')}.",
        f"Entered {entry.get('date')} at {entry.get('price')}.",
        f"Thesis: {thesis.get('summary')}",
        "Key assumptions:",
        *assumptions,
        f"Risks at the time: {', '.join(thesis.get('risks', [])) or 'none'}.",
    ]
    if exit_:
        lines.append(f"Exited {exit_.get('date')} at {exit_.get('price')}: {exit_.get('reason')}.")
    lines.append(
        f"Outcome: {str(outcome.get('result')).upper()}, return {outcome.get('returnPct')}%."
    )
    if inv.get("financialLoss"):
        lines.append(f"Financial loss: {inv['financialLoss']}.")
    if inv.get("lessons"):
        lines.append("Lessons: " + "; ".join(inv["lessons"]) + ".")
    return "\n".join(lines)


def bug_to_text(bug: dict) -> str:
    """Turn one resolved-bug record into a self-contained narrative."""
    return "\n".join([
        f"Resolved code bug {bug.get('id')}: {bug.get('title')} "
        f"({bug.get('category')}, {bug.get('language')}).",
        f"Vulnerable code:\n{bug.get('vulnerableCode')}",
        f"Impact: {bug.get('impact')}.",
        f"Historical loss: {bug.get('historicalLoss')}.",
        f"Fix applied:\n{bug.get('fixedCode')}",
        f"Lesson: {bug.get('lesson')}",
    ])


def _load_list(filenames: list[str], key: str) -> list[dict]:
    for name in filenames:
        path = APP_DIR / name
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return data[key] if isinstance(data, dict) else data
    raise FileNotFoundError(f"None of {filenames} found in {APP_DIR}")


def seed_memory(client: Hindsight) -> tuple[int, int]:
    """Load past investments and resolved code bugs into Hindsight."""
    investments = _load_list(DATA_FILES, "investments")
    bugs = _load_list(BUGS_FILES, "bugs")
    ensure_bank(client)
    for inv in investments:
        client.retain(
            bank_id=BANK_ID,
            content=investment_to_text(inv),
            context="Historical Business Loss",
            document_id=inv.get("id"),
        )
    for bug in bugs:
        client.retain(
            bank_id=BANK_ID,
            content=bug_to_text(bug),
            context="Resolved Code Bug",
            document_id=bug.get("id"),
        )
    return len(investments), len(bugs)


@st.cache_data(ttl=30, show_spinner=False)
def fetch_memory_view(view: str) -> list[str]:
    """Retrieve stored memories for the Memory Bank tab."""
    query = (
        "past startup investment failures and financial losses"
        if view == "Historical Business Losses"
        else "resolved code bugs, security vulnerabilities and fixes"
    )
    try:
        response = run_hindsight(lambda c: c.recall(bank_id=BANK_ID, query=query))
    except Exception as e:
        if _is_missing_bank(e):  # bank not created yet: nothing stored
            return []
        raise
    return prepare_memories([r.text for r in response.results], limit=50, chars=1500)


# Shown only when Hindsight has nothing yet (or can't be reached)
PLACEHOLDER_LOSSES = [
    {"Source": "Sample", "Case": "Example Payments Co", "Category": "Unit economics",
     "Impact": "Placeholder: total write-off", "Lesson": "CAC exceeded lifetime value"},
    {"Source": "Sample", "Case": "Example Lending Ltd", "Category": "Compliance",
     "Impact": "Placeholder: regulatory fine plus shutdown", "Lesson": "Licensing assumed, not verified"},
]
PLACEHOLDER_BUGS = [
    {"Source": "Sample", "Bug": "Float arithmetic on currency", "Type": "Logic",
     "Impact": "Placeholder: rounding drift in ledger", "Fix": "Use Decimal or integer minor units"},
    {"Source": "Sample", "Bug": "Missing idempotency key on payments", "Type": "Concurrency",
     "Impact": "Placeholder: duplicate charges", "Fix": "Require and store an idempotency key"},
]


# ----------------------------- Groq auditing -----------------------------

def _parse_json(content: str) -> dict:
    """Parse the model reply, tolerating stray text around the JSON object."""
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        try:
            return json.loads(content[content.index("{"): content.rindex("}") + 1])
        except (TypeError, ValueError):
            raise RuntimeError("The model did not return valid JSON. Please run the audit again.")


def groq_json(groq: Groq, messages: list[dict], temperature: float = 0.2) -> dict:
    """Call Groq in JSON mode, trying fallback models if one is unavailable."""
    completion, last_error = None, None
    for model in GROQ_MODELS:
        try:
            completion = groq.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
            break
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            if any(k in msg for k in ("model_not_found", "does not exist", "decommissioned",
                                      "response_format", "json_object", "not support")):
                continue  # this model is unavailable or lacks JSON mode: try the next one
            raise
    if completion is None:
        raise RuntimeError(f"No available Groq model worked. Last error: {last_error}")
    return _parse_json(completion.choices[0].message.content)


def run_groq_audit(groq: Groq, mode: str, text: str, memories: list[str], strictness: int) -> dict:
    system = BUSINESS_SYSTEM if mode == MODE_BUSINESS else CODE_SYSTEM
    system = system.replace("{STRICTNESS}", str(strictness)).replace(
        "{TONE}", strictness_tone(strictness)
    )
    if memories:
        numbered = [f"[{i + 1}] {m}" for i, m in enumerate(memories)]
        context_block = CONTEXT_FOUND + "\n\n" + "\n\n---\n\n".join(numbered)
    else:
        what = "of this startup idea" if mode == MODE_BUSINESS else "and code logic of this snippet"
        context_block = COLD_START.format(what=what)

    label = "STARTUP PITCH" if mode == MODE_BUSINESS else "CODE TO AUDIT"
    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"{context_block}\n\n{label}:\n{text[:8000]}\n\nRespond with the JSON object only.",
        },
    ]
    raw = groq_json(groq, messages)
    audit = normalise(mode, raw)

    # Guard against invented history: with no memories there can be no past match
    if not memories:
        audit["similar_idea_found"] = False
        audit["historical_loss"] = "None found"
        audit["similar_past_cases" if mode == MODE_BUSINESS else "similar_past_bugs"] = []
    return audit


def _clamp(value) -> int:
    try:
        return max(1, min(10, int(value)))
    except (TypeError, ValueError):
        return 1


def _as_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    return [str(value)] if value else []


def _as_dict_list(value, keys: list[str]) -> list[dict]:
    """Coerce a list of case objects into dicts that always contain `keys`."""
    out = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            out.append({k: str(item.get(k) or "Not stated") for k in keys})
        elif item:
            out.append({k: (str(item) if i == 0 else "Not stated") for i, k in enumerate(keys)})
    return out


CASE_KEYS = ["name", "similarity", "scope", "outcome", "financial_result", "lesson"]
BUG_KEYS = ["title", "similarity", "impact", "financial_result", "fix_summary"]


def normalise(mode: str, raw: dict) -> dict:
    """Make sure the UI never breaks on a slightly-off model response."""
    if not isinstance(raw, dict):
        raw = {}
    historical_loss = str(raw.get("historical_loss") or "None found")
    found = raw.get("similar_idea_found")
    found = found if isinstance(found, bool) else str(found).strip().lower() == "true"
    if mode == MODE_BUSINESS:
        scores = raw.get("scores", {}) if isinstance(raw.get("scores"), dict) else {}
        cases = _as_dict_list(raw.get("similar_past_cases"), CASE_KEYS)
        return {
            "scores": {k: _clamp(scores.get(k)) for k in ("Team", "Market", "Product")},
            "verdict": str(raw.get("verdict", "No verdict returned.")),
            "business_red_flags": _as_list(raw.get("business_red_flags")),
            "historical_loss": historical_loss,
            "similar_idea_found": found or bool(cases),
            "similar_past_cases": cases,
        }
    bugs = _as_dict_list(raw.get("similar_past_bugs"), BUG_KEYS)
    return {
        "security_score": _clamp(raw.get("security_score")),
        "code_bugs": _as_list(raw.get("code_bugs")),
        "historical_loss": historical_loss,
        "auto_fixed_code": str(raw.get("auto_fixed_code", "")),
        "similar_idea_found": found or bool(bugs),
        "similar_past_bugs": bugs,
    }


# ----------------------------- Crash self-heal -----------------------------

def recall_similar_bugs(client: Hindsight, error_text: str, code: str) -> list[str]:
    """Find previously resolved bugs that look like this crash."""
    query = ("Past similar code bugs, crashes and their fixes: "
             + error_text[-1200:] + "\n" + code[:600])
    try:
        response = client.recall(bank_id=BANK_ID, query=query)
    except Exception as e:
        if _is_missing_bank(e):
            ensure_bank(client)
            return []
        raise
    return prepare_memories([r.text for r in response.results])


def ask_groq_for_fix(groq: Groq, error_text: str, code: str, memories: list[str]) -> dict:
    memory_block = "\n\n---\n\n".join(f"[{i + 1}] {m}" for i, m in enumerate(memories))
    messages = [
        {"role": "system", "content": HEAL_SYSTEM},
        {"role": "user", "content": (
            f"HISTORICAL MEMORY:\n{memory_block}\n\n"
            f"CRASH TRACEBACK:\n{error_text[-3000:]}\n\n"
            f"CURRENT SOURCE CODE:\n{code[:12000]}\n\n"
            "Respond with the JSON object only.")},
    ]
    return groq_json(groq, messages, temperature=0.1)


def min_confidence_for(strictness: int) -> int:
    """Higher strictness demands more confidence before a fix is trusted (5 to 8)."""
    return 4 + -(-strictness // 3)


def heal_decision(result: dict, code: str, is_python: bool, min_conf: int) -> tuple[bool, str, int]:
    """Decide whether a proposed fix is safe enough to use. Returns (ok, reason, confidence)."""
    try:
        confidence = max(1, min(10, int(result.get("confidence", 0))))
    except (TypeError, ValueError):
        confidence = 1
    fixed = str(result.get("fixed_code") or "")
    if not result.get("similar_bug_matches"):
        return False, "Groq found no genuinely similar past bug, so no fix was trusted.", confidence
    if confidence < min_conf:
        return False, f"Confidence {confidence}/10 is below the required {min_conf}/10 at this strictness level.", confidence
    if not fixed.strip() or fixed.strip() == code.strip():
        return False, "The proposed fix is empty or identical to the current code.", confidence
    if is_python:
        try:
            compile(fixed, "fixed_code", "exec")
        except SyntaxError as e:
            return False, f"The proposed fix has a syntax error ({e}).", confidence
    return True, "", confidence


def make_diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (before)", tofile=f"{name} (after)"))


def apply_fix_to_file(path: Path, fixed: str) -> Path:
    """Back up the file, then overwrite it with the fix. Returns the backup path."""
    BACKUP_DIR.mkdir(exist_ok=True)
    backup = BACKUP_DIR / f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(path, backup)
    path.write_text(fixed, encoding="utf-8")
    return backup


def verify_fix(cmd: str, seconds: int) -> tuple[bool, str]:
    """Run the service briefly. Stable = still alive after `seconds`, or exited with code 0."""
    with tempfile.TemporaryFile("w+") as out:
        proc = subprocess.Popen(shlex.split(cmd), stdout=out, stderr=subprocess.STDOUT, text=True)
        try:
            code = proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return True, ""
        out.seek(0)
        return code == 0, out.read()[-1500:]


def record_incident(event: dict) -> None:
    try:
        event = {"time": datetime.now().isoformat(timespec="seconds"), **event}
        with INCIDENT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass  # logging must never break the app


def save_verified_fix(client: Hindsight, error_text: str, result: dict, diff: str, seconds: int) -> None:
    """Store a fix in memory only after it has proven stable."""
    content = (
        "Resolved code bug learning (auto-healed and verified stable).\n"
        f"Crash: {error_text[-500:]}\n"
        f"Root cause: {result.get('root_cause', '')}\n"
        f"Fix applied: {result.get('explanation', '')}\n"
        f"Similar past bug used: {result.get('matched_bug', '')}\n"
        f"Verified: the service stayed up for {seconds} seconds after the fix.\n"
        f"Diff:\n{diff[:1500]}"
    )
    client.retain(bank_id=BANK_ID, content=content, context="Resolved Code Bug")


# ----------------------------- Night Watch (background AutoHeal) -----------------------------

def pid_alive(pid: int) -> bool:
    """True if the process exists and is not a finished (zombie) child of this server."""
    try:
        if hasattr(os, "WNOHANG"):
            try:
                done, _ = os.waitpid(pid, os.WNOHANG)  # reaps it if it is our finished child
                if done:
                    return False
            except ChildProcessError:
                pass  # not our child (for example after the dashboard restarted)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def monitor_status() -> dict:
    """Read the saved Night Watch record and check whether it is still running."""
    try:
        info = json.loads(PID_FILE.read_text(encoding="utf-8"))
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return {"running": False, "pid": None}
    return {**info, "pid": pid, "running": pid_alive(pid)}


def start_monitor(cmd: str, target: str, apply: bool, min_conf: int, stable: int,
                  test_cmd: str, keep_awake: bool) -> tuple[bool, str]:
    """Launch autoheal.py as a detached background process that outlives this page."""
    target_path = Path(target).expanduser()
    if not cmd.strip():
        return False, "Enter the command that starts your service."
    if not target_path.is_file():
        return False, f"File not found: {target_path}"
    if not AUTOHEAL_SCRIPT.exists():
        return False, f"autoheal.py must be in the same folder as app.py ({AUTOHEAL_SCRIPT})."
    if not (_env("GROQ_API_KEY") and _env("HINDSIGHT_API_KEY")):
        return False, "Both API keys must be set in your .env file."
    if monitor_status()["running"]:
        return False, "Night Watch is already running."

    target_path = target_path.resolve()
    args = [sys.executable, "-u", str(AUTOHEAL_SCRIPT), "--cmd", cmd.strip(),
            "--target", str(target_path), "--min-confidence", str(int(min_conf)),
            "--stable-seconds", str(int(stable))]
    if apply:
        args.append("--apply")
    if test_cmd.strip():
        args += ["--test-cmd", test_cmd.strip()]
    if keep_awake and shutil.which("caffeinate"):
        args = ["caffeinate", "-i"] + args  # stop the Mac from idle-sleeping while watching

    with WATCH_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n--- Night Watch started {datetime.now():%Y-%m-%d %H:%M:%S} ---\n")
        log.flush()
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        proc = subprocess.Popen(args, cwd=str(target_path.parent), stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, **kwargs)
    time.sleep(1.5)
    if proc.poll() is not None:
        return False, "Night Watch exited immediately. Open the log below for the reason."
    PID_FILE.write_text(json.dumps({
        "pid": proc.pid, "cmd": cmd.strip(), "target": str(target_path), "apply": bool(apply),
        "started": datetime.now().isoformat(timespec="seconds")}), encoding="utf-8")
    return True, "Night Watch started."


def stop_monitor() -> str:
    """Stop the watcher and the service it supervises (they share one process group)."""
    status = monitor_status()
    pid = status.get("pid")
    if not pid or not status["running"]:
        return "Night Watch was not running."
    try:
        if os.name == "posix" and os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError) as e:
        return f"Could not stop Night Watch: {e}"
    for _ in range(20):  # wait up to 2s for it to exit
        if not pid_alive(pid):
            break
        time.sleep(0.1)
    return "Night Watch stopped."


def read_incidents(limit: int = 40) -> list[dict]:
    """Latest incidents first, from the log shared by AutoHeal and the dashboard."""
    try:
        lines = INCIDENT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            ev = json.loads(line)
            if isinstance(ev, dict):
                events.append(ev)
        except ValueError:
            continue
    return list(reversed(events))


def tail_text(path: Path, lines: int = 30) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="ignore").splitlines()[-lines:])
    except OSError:
        return ""


# ----------------------------- Rendering -----------------------------

def render_historical_loss(text: str) -> None:
    st.subheader("Historical Loss")
    if text.strip().lower().startswith("none found"):
        st.info(text)
    else:
        st.error(text)


def outcome_icon(outcome: str) -> str:
    o = outcome.strip().lower()
    return "🟢" if o.startswith("profit") else "🔴" if o.startswith("loss") else "⚪"


def render_business_matches(audit: dict) -> None:
    st.subheader("🔎 Similar Ideas in Your Past")
    if not audit["similar_past_cases"]:
        st.info("No similar idea found in your history. This audit judged the pitch on its own merits.")
        return
    st.warning(f"Found {len(audit['similar_past_cases'])} similar past idea(s) in your investment history.")
    for case in audit["similar_past_cases"]:
        with st.container(border=True):
            top = st.columns([2, 1, 2])
            top[0].markdown(f"**{case['name']}**")
            top[1].metric("Outcome", f"{outcome_icon(case['outcome'])} {case['outcome']}")
            top[2].metric("Financial result", case["financial_result"])
            st.markdown(f"**Why it is similar:** {case['similarity']}")
            st.markdown(f"**Scope:** {case['scope']}")
            st.markdown(f"**Lesson:** {case['lesson']}")


def render_code_matches(audit: dict) -> None:
    st.subheader("🔎 Similar Bugs in Your Past")
    if not audit["similar_past_bugs"]:
        st.info("No similar bug found in your history. This audit judged the code on its own merits.")
        return
    st.warning(f"Found {len(audit['similar_past_bugs'])} similar past bug(s) in your memory.")
    for bug in audit["similar_past_bugs"]:
        with st.container(border=True):
            st.markdown(f"**{bug['title']}**")
            st.error(f"Financial result: {bug['financial_result']}")
            st.markdown(f"**Why it is similar:** {bug['similarity']}")
            st.markdown(f"**Impact:** {bug['impact']}")
            st.markdown(f"**How it was fixed:** {bug['fix_summary']}")


def render_business(audit: dict) -> None:
    render_business_matches(audit)
    st.subheader("🚩 Business Red Flags")
    if audit["business_red_flags"]:
        for flag in audit["business_red_flags"]:
            st.error(flag)
    else:
        st.success("No red flags identified.")
    render_historical_loss(audit["historical_loss"])

    st.subheader("Scores")
    cols = st.columns(4)
    for col, key in zip(cols[:3], ("Team", "Market", "Product")):
        col.metric(key, f"{audit['scores'][key]}/10")
    cols[3].metric("Overall", f"{sum(audit['scores'].values()) / 3:.1f}/10")

    with st.container(border=True):
        st.subheader("Final Verdict")
        st.write(audit["verdict"])


def render_code(audit: dict) -> None:
    render_code_matches(audit)
    cols = st.columns(2)
    cols[0].metric("Security Score", f"{audit['security_score']}/10")
    cols[1].metric("Bugs Found", len(audit["code_bugs"]))

    st.subheader("🐞 Code Bugs")
    if audit["code_bugs"]:
        for bug in audit["code_bugs"]:
            st.error(bug)
    else:
        st.success("No bugs identified.")
    render_historical_loss(audit["historical_loss"])

    st.subheader("Auto-Fixed Code")
    st.code(audit["auto_fixed_code"] or "# No fixed code returned.", language=None)


def render_self_heal_ui(strictness: int) -> None:
    st.caption(
        "Paste a crash log. The app searches your memory for a similar bug you fixed before. "
        "Only if one exists does it write a fix, and it can apply it to your file with a backup, "
        "verify it, and roll back if it fails."
    )
    crash_log = st.text_area("Crash log / traceback", height=180, key="heal_log",
                             placeholder="Traceback (most recent call last): ...")
    path_str = st.text_input("Path to the crashed file on this machine (needed for auto-apply)",
                             key="heal_path", placeholder="/Users/you/project/server.py")
    pasted_code = st.text_area("Or paste the crashed code", height=220, key="heal_code")
    opt1, opt2, opt3 = st.columns([2, 2, 1])
    auto_apply = opt1.checkbox("Auto-apply the fix to the file (makes a backup first)", key="heal_apply")
    verify_cmd = opt2.text_input("Verification command (optional)", key="heal_cmd",
                                 placeholder="python3 server.py")
    window = opt3.number_input("Stable for (s)", min_value=3, max_value=300, value=15, key="heal_window")
    run = st.button("Run Self-Heal", type="primary", use_container_width=True, key="heal_run")
    if not run:
        return

    # ---- resolve inputs ----
    if not crash_log.strip():
        st.warning("Paste the crash log first.")
        return
    path = None
    if path_str.strip():
        path = Path(path_str.strip()).expanduser()
        if not path.is_file():
            st.error(f"File not found: {path}")
            return
        code = path.read_text(encoding="utf-8", errors="ignore")
    else:
        code = pasted_code.strip()
        if auto_apply:
            st.warning("Auto-apply needs a file path. Showing the proposed fix only.")
    if not code:
        st.warning("Give the crashed code: a file path or pasted code.")
        return
    is_python = path.suffix == ".py" if path else True
    can_apply = bool(auto_apply and path)

    # ---- 1. memory ----
    try:
        with st.spinner("Searching memory for a similar past bug..."):
            memories = run_hindsight(lambda c: recall_similar_bugs(c, crash_log, code))
    except Exception as e:
        st.error(f"Could not read memory ({e}). Auto-fix skipped, because fixing without history would be a guess.")
        record_incident({"status": "memory_unavailable", "error": crash_log[-800:]})
        return
    if not memories:
        st.error("No similar past bug found in your memory. Auto-fix skipped on purpose: "
                 "without a known fix it would be a guess. Use Code Review mode, then fix it by hand.")
        record_incident({"status": "no_similar_bug", "error": crash_log[-800:]})
        return

    # ---- 2. fix ----
    try:
        with st.spinner(f"Found {len(memories)} related records. Asking Groq for the fix..."):
            result = ask_groq_for_fix(get_groq_client(), crash_log, code, memories)
    except Exception as e:
        st.error(f"Could not get a fix from Groq: {e}")
        record_incident({"status": "groq_failed", "error": crash_log[-800:], "detail": str(e)})
        return

    result = result if isinstance(result, dict) else {}
    ok, reason, confidence = heal_decision(result, code, is_python, min_confidence_for(strictness))

    st.subheader("🔎 Similar Past Bug")
    if not result.get("similar_bug_matches"):
        st.info("No genuinely similar bug in your history.")
    else:
        st.warning(f"Matched: {result.get('matched_bug', 'Not stated')}")
        st.error(f"Past financial impact: {result.get('past_financial_impact', 'Not stated')}")
    cols = st.columns(3)
    cols[0].metric("Confidence", f"{confidence}/10")
    cols[1].metric("Required", f"{min_confidence_for(strictness)}/10")
    status_box = cols[2].empty()

    with st.expander(f"Memory records used ({len(memories)})"):
        for m in memories:
            st.text(m)
            st.divider()

    if not ok:
        status_box.metric("Status", "Not applied")
        st.warning(f"Fix not trusted: {reason}")
        record_incident({"status": "fix_rejected", "reason": reason, "error": crash_log[-800:]})
        return

    fixed = str(result["fixed_code"])
    diff = make_diff(code, fixed, path.name if path else "code")
    st.subheader("Root Cause")
    st.error(str(result.get("root_cause", "Not stated")))
    st.markdown(f"**Fix:** {result.get('explanation', '')}")
    st.subheader("Proposed Change")
    st.code(diff or "(no changes)", language="diff")
    with st.expander("Complete fixed code"):
        st.code(fixed, language=None)

    if not can_apply:
        status_box.metric("Status", "Proposed only")
        st.info("Nothing was changed on disk. Tick auto-apply and give a file path to let the app apply it.")
        record_incident({"status": "proposed_only", "matched_bug": result.get("matched_bug"), "diff": diff})
        return

    # ---- 3. apply, verify, roll back ----
    backup = apply_fix_to_file(path, fixed)
    st.success(f"Fix applied to {path.name}. Backup: {backup.name}")
    if not verify_cmd.strip():
        status_box.metric("Status", "Applied (unverified)")
        st.warning("Not saved to memory, because the fix was not verified. Add a verification command "
                   "so the app can confirm the service stays up.")
        record_incident({"status": "fix_applied_unverified", "matched_bug": result.get("matched_bug"),
                         "diff": diff, "backup": str(backup)})
        return

    try:
        with st.spinner(f"Verifying: running the service for {int(window)} seconds..."):
            stable, output = verify_fix(verify_cmd.strip(), int(window))
    except Exception as e:
        stable, output = False, f"Could not run the verification command: {e}"

    if stable:
        status_box.metric("Status", "Applied and verified")
        st.success(f"Verified: the service stayed up for {int(window)} seconds.")
        record_incident({"status": "fix_verified", "matched_bug": result.get("matched_bug"), "diff": diff})
        try:
            run_hindsight(lambda c: save_verified_fix(c, crash_log, result, diff, int(window)))
            fetch_memory_view.clear()
            st.toast("Learnings automatically saved to Memory Bank")
        except Exception as e:
            st.toast(f"Could not save learnings to memory: {e}")
    else:
        shutil.copy2(backup, path)
        status_box.metric("Status", "Rolled back")
        st.error("The service crashed again after the fix, so the original file was restored.")
        if output:
            st.code(output, language=None)
        record_incident({"status": "fix_failed_rolled_back", "matched_bug": result.get("matched_bug")})


def _incident_feed() -> None:
    events = read_incidents()
    if not events:
        st.caption("No incidents yet. When the service crashes, what AutoHeal did will appear here.")
    else:
        st.dataframe(
            [{"Time": e.get("time", ""),
              "What happened": STATUS_LABELS.get(e.get("status"), str(e.get("status", ""))),
              "Matched past bug": e.get("matched_bug") or "",
              "Confidence": e.get("confidence", "")} for e in events],
            use_container_width=True, hide_index=True)
        latest_diff = next((e for e in events if e.get("diff")), None)
        if latest_diff:
            with st.expander(f"Latest change made ({latest_diff.get('time', '')})"):
                st.code(latest_diff["diff"], language="diff")
    with st.expander("Night Watch log"):
        st.code(tail_text(WATCH_LOG) or "(empty)", language=None)


# Refreshes itself every few seconds on recent Streamlit versions
_fragment = getattr(st, "fragment", None)
render_incident_feed = _fragment(run_every=5)(_incident_feed) if _fragment else _incident_feed


def render_monitor_ui(strictness: int) -> None:
    st.caption(
        "Night Watch runs your service under AutoHeal in the background. If it crashes, AutoHeal searches "
        "your memory for a similar past bug, fixes the file, restarts the service, and restores the "
        "original if the fix fails. It keeps running even if you close this page, as long as this computer "
        "stays on."
    )
    status = monitor_status()
    if status["running"]:
        st.success(f"🟢 Night Watch is running (PID {status['pid']}).")
        st.markdown(
            f"**Service:** `{status.get('cmd', '')}`  \n**Target file:** `{status.get('target', '')}`  \n"
            f"**Mode:** {'Auto-apply fixes' if status.get('apply') else 'Dry run (no files are changed)'}  \n"
            f"**Started:** {status.get('started', '')}")
        if st.button("Stop Night Watch (also stops the service it started)", key="mon_stop"):
            st.toast(stop_monitor())
            st.rerun()
    else:
        if status.get("pid"):
            st.warning("Night Watch stopped on its own. The incident feed and log below say why.")
        else:
            st.info("Night Watch is not running.")
        cmd = st.text_input("Command that starts your service", key="mon_cmd",
                            placeholder="python3 server.py")
        target = st.text_input("File AutoHeal may fix (full path)", key="mon_target",
                               placeholder="/Users/you/project/server.py")
        c1, c2 = st.columns(2)
        apply = c1.checkbox("Apply fixes automatically (otherwise dry run: only logs what it would do)",
                            key="mon_apply")
        keep_awake = c2.checkbox("Keep this Mac awake while watching", value=True, key="mon_awake")
        c3, c4 = st.columns(2)
        stable = c3.number_input("Fix counts as working after (seconds up)", min_value=5, max_value=600,
                                 value=20, key="mon_stable")
        test_cmd = c4.text_input("Test command to run after a fix (optional)", key="mon_test")
        st.caption(f"Minimum confidence to trust a fix: {min_confidence_for(strictness)}/10 "
                   "(set by the strictness slider).")
        if st.button("Start Night Watch", type="primary", use_container_width=True, key="mon_start"):
            ok, msg = start_monitor(cmd, target, apply, min_confidence_for(strictness), int(stable),
                                    test_cmd, keep_awake)
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)
    st.subheader("Incident feed")
    render_incident_feed()


# ----------------------------- UI -----------------------------

st.set_page_config(page_title="Investment Thesis & FinTech Code Auditor", page_icon="📊", layout="wide")
st.title("📊 Investment Thesis & FinTech Code Auditor")

with st.sidebar:
    st.header("Audit Settings")
    strictness = st.slider("VC/Engineer Strictness Level", min_value=1, max_value=10, value=7)
    st.divider()
    st.subheader("API Status")
    st.write(f"{'✅' if _env('GROQ_API_KEY') else '❌'} Groq API key")
    st.write(f"{'✅' if _env('HINDSIGHT_API_KEY') else '❌'} Hindsight API key")

tab_audit, tab_memory = st.tabs(["Auditor Workspace", "Unified Memory Bank"])

# ---- Tab 1: Auditor Workspace ----
def render_audit_ui(mode: str) -> None:
    is_business = mode == MODE_BUSINESS

    uploaded = st.file_uploader(
        "Upload a pitch deck summary" if is_business else "Upload code",
        type=["txt", "pdf"],
        key=f"upload_{mode}",
    )
    pasted = st.text_area(
        "Or paste the startup pitch" if is_business else "Or paste the code to audit",
        height=300,
        placeholder="Problem, solution, market, team, traction, ask..." if is_business
        else "Paste the function, contract or module here...",
        key=f"text_{mode}",
    )
    run = st.button("Run Audit", type="primary", use_container_width=True)

    if run:
        parts, read_error = [], None
        if uploaded is not None:
            try:
                parts.append(extract_text_from_upload(uploaded))
            except Exception as e:
                read_error = f"Could not read the uploaded file: {e}"
        if pasted.strip():
            parts.append(pasted.strip())
        text = "\n\n".join(p for p in parts if p)

        if read_error:
            st.error(read_error)
        elif not text:
            st.warning("Upload a file or paste some input first. A scanned PDF may have no extractable text.")
        else:
            audit, memories, failed = None, [], False
            try:
                with st.spinner("Searching Hindsight for similar past cases..."):
                    try:
                        memories = run_hindsight(lambda c: recall_similar(c, text, mode))
                    except Exception as e:
                        st.warning(f"Could not reach Hindsight memory ({e}). Running as a cold start.")
                with st.spinner("Running the audit..."):
                    audit = run_groq_audit(get_groq_client(), mode, text, memories, strictness)
            except Exception as e:
                failed = True
                cause = f" (cause: {e.__cause__})" if getattr(e, "__cause__", None) else ""
                low = str(e).lower()
                if "429" in low or "rate limit" in low or "rate_limit" in low:
                    st.error("Groq rate limit reached. Wait about a minute and run the audit again.")
                elif "413" in low or "too large" in low:
                    st.error("The input is too large for the model. Paste a shorter pitch or code snippet.")
                else:
                    st.error(f"Audit failed: {e}{cause}")

            if audit and not failed:
                if memories:
                    st.success(f"Found {len(memories)} similar past records. Audit compares against them.")
                else:
                    st.info("No historical context found. Audit analyzes the standalone input.")

                if is_business:
                    render_business(audit)
                else:
                    render_code(audit)

                with st.expander(f"Historical context used ({len(memories)})"):
                    if memories:
                        for m in memories:
                            st.text(m)
                            st.divider()
                    else:
                        st.write("None.")

                # Autonomous memory: save learnings right after rendering, no button
                try:
                    run_hindsight(lambda c: save_learnings(c, mode, text, audit))
                    fetch_memory_view.clear()
                    st.toast("Learnings automatically saved to Memory Bank")
                except Exception as e:
                    st.toast(f"Could not save learnings to memory: {e}")


with tab_audit:
    mode = st.radio("Audit mode", [MODE_BUSINESS, MODE_CODE], horizontal=True)
    if mode == MODE_CODE:
        task = st.radio("Technical task", [TASK_REVIEW, TASK_HEAL, TASK_MONITOR], horizontal=True)
    else:
        task = TASK_REVIEW
    if task == TASK_HEAL:
        render_self_heal_ui(strictness)
    elif task == TASK_MONITOR:
        render_monitor_ui(strictness)
    else:
        render_audit_ui(mode)

# ---- Tab 2: Unified Memory Bank ----
with tab_memory:
    st.subheader("Unified Memory Bank")
    view = st.selectbox("View", ["Historical Business Losses", "Resolved Code Bugs"])

    records, live = [], False
    try:
        records = fetch_memory_view(view)
        live = bool(records)
    except Exception as e:
        st.warning(f"Could not retrieve memories from Hindsight ({e}). Showing placeholder data.")

    if live:
        st.caption(f"{len(records)} records retrieved from Hindsight.")
        st.dataframe(
            [{"#": i + 1, "Memory": r} for i, r in enumerate(records)],
            use_container_width=True,
            hide_index=True,
            column_config={"Memory": st.column_config.TextColumn(width="large")},
        )
    else:
        st.caption("Nothing stored yet. Showing placeholder data. Open the seed section below and click Seed Memory Bank, or run an audit to start filling the memory bank.")
        st.dataframe(
            PLACEHOLDER_LOSSES if view == "Historical Business Losses" else PLACEHOLDER_BUGS,
            use_container_width=True,
            hide_index=True,
        )

    with st.expander("Seed memory (past_investments.json + past_code_bugs.json)"):
        st.caption("One-time setup: loads past business deals and resolved code bugs into Hindsight so both audit modes have history to compare against.")
        if st.button("Seed Memory Bank"):
            try:
                with st.spinner("Uploading..."):
                    n_inv, n_bugs = run_hindsight(seed_memory)
                fetch_memory_view.clear()
                st.success(f"Uploaded {n_inv} past investments and {n_bugs} resolved code bugs.")
            except Exception as e:
                st.error(f"Seeding failed: {e}")

"""
AutoHeal: self-healing supervisor for a service that crashes at night
---------------------------------------------------------------------
Runs your service. If it crashes, AutoHeal:
  1. reads the crash traceback
  2. searches Hindsight memory for a SIMILAR bug you resolved before
  3. only if one exists, asks Groq for a fix (using that past fix as the guide)
  4. backs up the file, applies the fix, restarts the service
  5. waits to see that the service stays up; if it crashes again, it ROLLS BACK
  6. saves the verified fix back to memory, so the next similar crash is even easier

Nothing is changed on disk unless you pass --apply. Without it, AutoHeal only shows
what it WOULD do (dry run).

Example:
    python3 autoheal.py --cmd "python3 demo_service.py" --target demo_service.py --apply

.env (next to this file):
    HINDSIGHT_API_KEY=your_hindsight_key_here
    GROQ_API_KEY=your_groq_key_here
"""

import argparse
import asyncio
import difflib
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import certifi
from dotenv import load_dotenv
from groq import Groq
from hindsight_client import Hindsight

load_dotenv()
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

HINDSIGHT_URL = "https://api.hindsight.vectorize.io"
BANK_ID = os.getenv("HINDSIGHT_BANK_ID", "investment-thesis-auditor")
GROQ_MODELS = [m for m in [os.getenv("GROQ_MODEL"), "openai/gpt-oss-120b",
                           "openai/gpt-oss-20b", "qwen/qwen3.6-27b"] if m]
APP_DIR = Path(__file__).resolve().parent
INCIDENT_LOG = APP_DIR / "autoheal_incidents.jsonl"
BACKUP_DIR = APP_DIR / ".autoheal_backups"

SYSTEM_PROMPT = """You are an autonomous site reliability engineer. A production service just \
crashed. You receive the CRASH TRACEBACK, the CURRENT SOURCE FILE, and HISTORICAL MEMORY: \
records of similar bugs this team resolved before, with their fixes.

Rules:
- Only treat the crash as a known problem if the historical memory contains a genuinely similar \
bug. If it does not, set "similar_bug_matches" to false and leave "fixed_code" empty.
- If it matches, apply the SAME KIND of fix that worked before, with the smallest change that \
stops the crash. Do not refactor, rename or change unrelated behaviour.
- "fixed_code" must be the COMPLETE corrected source file, not a diff or a snippet.
- "confidence" is an integer 1-10: how sure you are that this fix removes the crash without \
breaking anything else.
- Never invent past bugs. "matched_bug" must be copied from the historical memory.

Return ONLY a valid JSON object with exactly this shape and no other text:
{
  "similar_bug_matches": <true or false>,
  "matched_bug": "<title of the past bug>",
  "confidence": <int 1-10>,
  "root_cause": "<one or two sentences>",
  "explanation": "<what the fix changes and why>",
  "fixed_code": "<the complete corrected source file>"
}"""


# ----------------------------- Utilities -----------------------------

def log(msg: str) -> None:
    print(f"[autoheal {datetime.now():%H:%M:%S}] {msg}", flush=True)


def record_incident(event: dict) -> None:
    event = {"time": datetime.now().isoformat(timespec="seconds"), **event}
    with INCIDENT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _env(name: str) -> str:
    return os.getenv(name, "").strip().strip("\"'").strip()


def get_groq_client() -> Groq:
    key = _env("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is missing from your .env file.")
    return Groq(api_key=key, timeout=60.0, max_retries=2)


def get_hindsight_client() -> Hindsight:
    key = _env("HINDSIGHT_API_KEY")
    if not key:
        raise RuntimeError("HINDSIGHT_API_KEY is missing from your .env file.")
    return Hindsight(base_url=HINDSIGHT_URL, api_key=key, timeout=60.0)


def run_hindsight(fn):
    """Run fn(client) in its own thread with its own event loop (the client is async inside)."""
    def worker():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return fn(get_hindsight_client())
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(worker).result()


# ----------------------------- Memory -----------------------------

def recall_similar_bugs(error_text: str, code: str) -> list[str]:
    """Find resolved bugs in Hindsight that look like this crash."""
    query = ("Past similar code bugs, crashes and their fixes: "
             + error_text[-1200:] + "\n" + code[:600])
    try:
        response = run_hindsight(lambda c: c.recall(bank_id=BANK_ID, query=query))
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return []  # bank doesn't exist yet: no history
        raise
    seen, out = set(), []
    for r in response.results:
        t = " ".join(str(r.text).split())
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:900])
        if len(out) >= 10:
            break
    return out


def save_verified_fix(pending: dict, stable_seconds: int) -> None:
    """Store a fix in memory ONLY after it has proven stable in production."""
    content = (
        "Resolved code bug learning (auto-healed and verified stable).\n"
        f"Crash: {pending['error'][-500:]}\n"
        f"Root cause: {pending['root_cause']}\n"
        f"Fix applied: {pending['explanation']}\n"
        f"Similar past bug used: {pending['matched_bug']}\n"
        f"Verified: the service stayed up for {stable_seconds} seconds after the fix.\n"
        f"Diff:\n{pending['diff'][:1500]}"
    )
    run_hindsight(lambda c: c.retain(bank_id=BANK_ID, content=content, context="Resolved Code Bug"))


# ----------------------------- AI fix -----------------------------

def parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        try:
            return json.loads(content[content.index("{"): content.rindex("}") + 1])
        except (TypeError, ValueError):
            raise RuntimeError("Groq did not return valid JSON.")


def ask_groq_for_fix(error_text: str, code: str, memories: list[str]) -> dict:
    groq = get_groq_client()
    memory_block = "\n\n---\n\n".join(f"[{i + 1}] {m}" for i, m in enumerate(memories))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"HISTORICAL MEMORY:\n{memory_block}\n\n"
            f"CRASH TRACEBACK:\n{error_text[-3000:]}\n\n"
            f"CURRENT SOURCE FILE:\n{code[:12000]}\n\n"
            "Respond with the JSON object only.")},
    ]
    last_error = None
    for model in GROQ_MODELS:
        try:
            completion = groq.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_object"}, temperature=0.1)
            return parse_json(completion.choices[0].message.content)
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            if any(k in msg for k in ("model_not_found", "does not exist", "decommissioned",
                                      "response_format", "json_object", "not support")):
                continue
            raise
    raise RuntimeError(f"No available Groq model worked. Last error: {last_error}")


# ----------------------------- Healing -----------------------------

def make_diff(old: str, new: str, name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"{name} (before)", tofile=f"{name} (after)"))


def rollback(pending: dict, target: Path) -> None:
    shutil.copy2(pending["backup"], target)
    log(f"↩️  Rolled back {target.name} from backup.")


def heal(error_text: str, target: Path, args) -> dict | None:
    """Try to fix the crash. Returns a 'pending' record if a fix was applied, else None."""
    code = target.read_text(encoding="utf-8")

    log("Searching memory for a similar past bug...")
    try:
        memories = recall_similar_bugs(error_text, code)
    except Exception as e:
        log(f"Could not read memory ({e}). Not auto-fixing without history.")
        record_incident({"status": "memory_unavailable", "error": error_text[-800:], "detail": str(e)})
        return None

    if not memories:
        log("No similar past bug found in memory. Escalating to a human instead of guessing.")
        record_incident({"status": "no_similar_bug", "error": error_text[-800:]})
        return None
    log(f"Found {len(memories)} related memory records. Asking Groq for the fix...")

    try:
        result = ask_groq_for_fix(error_text, code, memories)
    except Exception as e:
        log(f"Could not get a fix from Groq ({e}).")
        record_incident({"status": "groq_failed", "error": error_text[-800:], "detail": str(e)})
        return None

    fixed = str(result.get("fixed_code") or "")
    try:
        confidence = int(result.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0

    if not result.get("similar_bug_matches"):
        log("Groq found no genuinely similar past bug. Escalating to a human.")
        record_incident({"status": "no_match", "error": error_text[-800:]})
        return None
    if confidence < args.min_confidence:
        log(f"Confidence {confidence}/10 is below the {args.min_confidence} threshold. Escalating to a human.")
        record_incident({"status": "low_confidence", "confidence": confidence, "error": error_text[-800:]})
        return None
    if not fixed.strip() or fixed.strip() == code.strip():
        log("The proposed fix is empty or identical to the current file. Escalating to a human.")
        record_incident({"status": "empty_fix", "error": error_text[-800:]})
        return None
    if target.suffix == ".py":
        try:
            compile(fixed, str(target), "exec")
        except SyntaxError as e:
            log(f"The proposed fix has a syntax error ({e}). Escalating to a human.")
            record_incident({"status": "invalid_fix", "error": error_text[-800:]})
            return None

    diff = make_diff(code, fixed, target.name)
    log(f"Matched past bug: {result.get('matched_bug')}  |  confidence {confidence}/10")
    log(f"Root cause: {result.get('root_cause')}")
    print(diff, flush=True)

    if not args.apply:
        log("DRY RUN: no files changed. Re-run with --apply to let AutoHeal apply fixes.")
        record_incident({"status": "dry_run", "matched_bug": result.get("matched_bug"),
                         "confidence": confidence, "diff": diff})
        return None

    BACKUP_DIR.mkdir(exist_ok=True)
    backup = BACKUP_DIR / f"{target.name}.{datetime.now():%Y%m%d-%H%M%S}.bak"
    shutil.copy2(target, backup)
    target.write_text(fixed, encoding="utf-8")
    pending = {
        "backup": backup, "diff": diff, "error": error_text,
        "root_cause": str(result.get("root_cause", "")),
        "explanation": str(result.get("explanation", "")),
        "matched_bug": str(result.get("matched_bug", "")), "confidence": confidence,
    }
    log(f"✍️  Fix applied. Backup saved at {backup.name}")

    if args.test_cmd:
        log(f"Running tests: {args.test_cmd}")
        if subprocess.run(shlex.split(args.test_cmd)).returncode != 0:
            log("Tests failed after the fix.")
            rollback(pending, target)
            record_incident({"status": "tests_failed_rolled_back", "diff": diff})
            return None

    record_incident({"status": "fix_applied", "matched_bug": pending["matched_bug"],
                     "confidence": confidence, "diff": diff, "backup": str(backup)})
    return pending


# ----------------------------- Supervisor -----------------------------

def start_service(cmd: str):
    """Start the service, echoing its errors while keeping the last lines for diagnosis."""
    proc = subprocess.Popen(shlex.split(cmd), stderr=subprocess.PIPE, text=True, bufsize=1)
    buf: list[str] = []

    def pump():
        for line in proc.stderr:
            sys.stderr.write(line)
            buf.append(line)
            del buf[:-200]

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    return proc, buf, t


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Self-healing supervisor powered by Hindsight memory + Groq")
    p.add_argument("--cmd", required=True, help='command that runs your service, e.g. "python3 server.py"')
    p.add_argument("--target", required=True, help="source file AutoHeal may fix when the service crashes")
    p.add_argument("--apply", action="store_true", help="actually modify the target file (default: dry run)")
    p.add_argument("--min-confidence", type=int, default=7, help="minimum Groq confidence (1-10) to apply a fix")
    p.add_argument("--stable-seconds", type=int, default=20, help="how long the service must stay up to count the fix as working")
    p.add_argument("--test-cmd", default="", help="optional test command run after a fix, before restart")
    args = p.parse_args(argv)

    target = Path(args.target).resolve()
    if not target.exists():
        log(f"Target file not found: {target}")
        return 2
    log(f"Mode: {'APPLY (fixes will be written to disk)' if args.apply else 'DRY RUN (no files will change)'}")

    pending = None
    while True:
        log(f"Starting service: {args.cmd}")
        proc, buf, pump = start_service(args.cmd)
        try:
            if pending:
                try:
                    code = proc.wait(timeout=args.stable_seconds)
                except subprocess.TimeoutExpired:
                    log(f"✅ Fix held: service stayed up for {args.stable_seconds}s. Saving it to memory.")
                    try:
                        save_verified_fix(pending, args.stable_seconds)
                        record_incident({"status": "fix_verified_saved", "matched_bug": pending["matched_bug"]})
                    except Exception as e:
                        log(f"Could not save the learning to memory ({e}).")
                    pending = None
                    code = proc.wait()
                else:
                    pump.join(2)
                    log("❌ The service crashed again right after the fix.")
                    rollback(pending, target)
                    record_incident({"status": "fix_failed_rolled_back", "matched_bug": pending["matched_bug"]})
                    log("Not retrying. A human needs to look at this one.")
                    return 2
            else:
                code = proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            log("Stopped by user.")
            return 130

        pump.join(2)
        if code == 0:
            log("Service exited cleanly.")
            return 0

        error_text = "".join(buf)[-4000:]
        log(f"💥 Service crashed (exit code {code}).")
        pending = heal(error_text, target, args)
        if pending is None:
            log("No automatic fix was applied. Stopping so the crash is not hidden.")
            return 1


if __name__ == "__main__":
    sys.exit(main())

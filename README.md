# Streamlit
# Investment Thesis & FinTech Code Auditor

An auditor that remembers. It judges startup pitches and FinTech code against **your own history**, and it can fix a crashed service by itself, but only if it has fixed a similar bug before.

Memory is provided by [Hindsight](https://github.com/vectorize-io/hindsight) (Hindsight Cloud), the model runs on [Groq](https://groq.com/), and the dashboard is built with [Streamlit](https://streamlit.io/).

<img width="1438" height="1012" alt="retain and recall" src="https://github.com/user-attachments/assets/a2d2052b-c480-4b9f-b066-fc49bfc85e2f" />


## What it does

**Business Pitch mode**
- Paste a pitch or upload a `.txt` / `.pdf`.
- Get Team, Market and Product scores (1 to 10), red flags, a verdict, and the financial impact of past similar failures.
- A **Similar Ideas in Your Past** section lists matching past investments with outcome (loss or profit), financial result, scope, and lesson.

**Technical Code mode**
- **Code Review:** security score, bugs found, historical loss for that kind of bug, similar past bugs, and a fully corrected version of the code.
- **Crash Self-Heal:** paste a crash log. If memory holds a similar resolved bug, the app writes a fix, can apply it to your file (with a backup), verifies the service stays up, and rolls back if it does not.
- **Night Watch:** runs `autoheal.py` in the background so crashes are handled while nobody is watching, with a live incident feed.

**Unified Memory Bank tab**
- Browse stored business losses and resolved code bugs, and seed memory from the two JSON files.

A strictness slider (1 to 10) changes the tone and how harshly the model scores. In Self-Heal it also sets how confident the model must be before a fix is trusted (5 out of 10 at low strictness, up to 8 out of 10 at 10).

## How Hindsight memory is used

Memory is the center of the design, not a feature added on top.

| Action | Where | What it does |
| --- | --- | --- |
| `retain` | Seeding | Loads `past_investments.json` and `past_code_bugs.json` into the memory bank. Records use stable IDs, so seeding again updates instead of duplicating. |
| `retain` | After every audit | Saves the flaws, bugs and fixes found, labelled as audits so they are never mistaken for real investments. |
| `retain` | After a verified fix | Saves a crash fix **only after** the patched service stayed up for the stability window. |
| `recall` | Before every audit | Searches for similar past investments or bugs, and the result is given to the model as context. |
| `recall` | Before any auto-fix | Gate: no similar past bug in memory means no edit. |

Two rules shape everything:
1. **Read gate:** the agent may only fix a crash if memory shows a similar bug was resolved before.
2. **Write gate:** a fix only goes into memory after it has proven stable. A fix that crashes again is rolled back and not learned.
<img width="1600" height="888" alt="hindsight memory" src="https://github.com/user-attachments/assets/489472d4-30e8-49b6-b6f5-96fae46ad816" />

Other details:
- Recall returns many small facts (99 results for 14 stored records), so results are deduplicated and capped at 12 records of up to 700 characters before reaching the model.
- If nothing is recalled, the app forces "no similar idea found" so the model cannot invent history.
- Hindsight's client is async internally, so every call runs in its own short-lived thread with its own event loop. This avoids "event loop is closed" errors in Streamlit.

## Architecture

```
          Streamlit dashboard (app.py)
   Business Pitch | Code Review | Crash Self-Heal | Night Watch
                 |                        |
          recall / retain          starts / stops
                 |                        |
         Hindsight Cloud  <---------  autoheal.py  ----> your service
                 ^                   (background supervisor)
                 |                        |
         Groq (JSON mode)          backup, patch, verify, roll back
```

## Setup

**Requirements:** Python 3.10 or newer, a Hindsight Cloud API key, and a Groq API key.

```bash
git clone <your-repo-url>
cd <your-repo-folder>
pip install -r requirements.txt
```

Create a `.env` file next to `app.py` (see `.env.example`):

```
HINDSIGHT_API_KEY=your_hindsight_cloud_key
GROQ_API_KEY=your_groq_key
```

Optional settings: `HINDSIGHT_BANK_ID` (default `investment-thesis-auditor`) and `GROQ_MODEL` to force a specific model.

Run the dashboard:

```bash
python3 -m streamlit run app.py
```

In the app, open **Unified Memory Bank**, expand the seed section, and click **Seed Memory Bank** once. Wait a few seconds for Hindsight to finish processing before the first audit.

## Try it

- **Business:** paste a pitch for a payments app with no revenue model and a solo founder. It should match a past payments startup and report how that one ended.
- **Code:** paste a login function that builds SQL by string concatenation. It should match the SQL injection record and return a parameterized version.
- **Self-heal:** paste `ZeroDivisionError: division by zero` in **Crash Self-Heal**, set the path to `demo_service.py`, tick auto-apply, and set the verification command to `python3 demo_service.py`.
- **Refusal:** paste a crash with no precedent, such as an unfamiliar `KeyError`. It should refuse to change anything.

## AutoHeal from the command line

`autoheal.py` supervises a service and heals it when it crashes:

```bash
python3 autoheal.py --cmd "python3 demo_service.py" --target demo_service.py --apply
```

Without `--apply` it runs as a dry run and changes nothing. Useful options: `--min-confidence`, `--stable-seconds`, `--test-cmd`.

Safeguards:
- No similar past bug in memory means no fix.
- A fix is rejected if it is low-confidence, empty, a non-match, or has a syntax error.
- The target file is backed up before every change, and restored if the service crashes again.
- Memory is only updated with fixes that held.
- Memory unavailable means it does not fix blindly.
- After an unfixable crash it stops, so the failure is not hidden by a restart loop.

Files it creates: `autoheal_incidents.jsonl` (incident log), `.autoheal_backups/` (backups), `autoheal_watch.log` (Night Watch output), `.autoheal.pid` (Night Watch status).

## Project structure

```
app.py                    Streamlit dashboard and audit logic
autoheal.py               Background self-healing supervisor
demo_service.py           Small service that crashes on purpose (divide by zero)
past_investments.json     Past investments used as business memory
past_code_bugs.json       Resolved bugs used as code memory
requirements.txt
.env.example
```

## Data

The records in `past_investments.json` and `past_code_bugs.json` are sample data with fictional company names. Replace them with your own history for real use. A few bug records reference widely reported public incidents; verify those figures before citing them. The rest are illustrative.

## Limitations

- Fixes are written by a model. For anything that handles money or credentials, run your tests before trusting a change (`--test-cmd`).
- AutoHeal edits one file at a time.
- Night Watch only works while the computer stays on. On a Mac, keep it awake (the dashboard offers this) and note that closing the lid sleeps it.
- Night Watch starts background processes with POSIX process groups, so it supports macOS and Linux. The rest of the app runs anywhere Python and Streamlit run.
- Hindsight recall returns facts ranked by relevance, not a strict similarity score, so the model decides which retrieved facts are genuinely similar.
<img width="1280" height="832" alt="s4" src="https://github.com/user-attachments/assets/bcd072a4-44e3-48a8-af26-2f163ae70331" />
<img width="1280" height="832" alt="s5" src="https://github.com/user-attachments/assets/53f92b8e-0b03-4ae4-8f58-9251c12dc725" />
<img width="1280" height="832" alt="s6" src="https://github.com/user-attachments/assets/1e88aa67-588c-49e3-baac-38068cfb2cc3" />

## Hackathon Submission Links

*   **Read the full story on Dev.to:** https://dev.to/sravanth_bikkasani_bc8b62
*   **Watch the Demo Video:** 
*   **Social Post:** https://x.com/Sravanth2008
*   **Reddit:**https://www.reddit.com/user/Sravanthbikkasani/

    ##Team Deliverables:
    
    Subham:
    https://dev.to/shubham_srivastava_7/i-stopped-my-hindsight-backed-agent-from-inventing-my-past-3ojk
    https://lnkd.in/p/gF5xHrAK
    https://www.reddit.com/u/Shubham151209/s/mDuit6qvwV
    
    Sumedha:
    https://www.linkedin.com/feed/update/urn:li:activity:7524833597292388352/
    https://www.reddit.com/r/SideProject/comments/1xq9r79/can_an_ai_agent_actually_benefit_from_experience/
    https://dev.to/sumedha_ghosh_302008/your-ai-agent-is-making-up-its-past-heres-how-we-caught-it-5bem
    https://www.reddit.com/r/LLMDevs/comments/1xq6zw5/your_ai_agent_is_making_up_its_past_heres_how_we/
    
    Aarush:
    https://dev.to/arcturuss8170/i-stopped-treating-agent-memory-like-prompt-history-2h38
    https://lnkd.in/p/g9cD_JPM
       https://www.reddit.com/r/SideProject/comments/1xq9r79/can_an_ai_agent_actually_benefit_from_experience/
  

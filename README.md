# Dukaan Dost

An autonomous growth analyst for Paytm merchants. Built for the Paytm Build for India AI
Hackathon, Bengaluru Edition, Track 1 (Merchant Growth AI) by Team Pick Me: Sampurna Ghosh
and Mohammed Nooman.

Every night Dukaan Dost studies one shop's transactions, finds the patterns a shopkeeper
cannot see from behind the counter, and proposes candidate growth actions written for that
shop. It prices each candidate against the shop's own history, refuses anything that loses
money, telephones the owner in Hindi for a ten second spoken approval, dispatches the
campaign to a random 85 percent of the target segment, and 72 hours later measures the lift
against the 15 percent it deliberately held back. What it predicted and what actually
happened both go into memory, so the next campaign predicts better than the last.

## The design rule

> The language model proposes what action to take and for whom, and writes the merchant
> facing Hindi. It never produces a number.

The model chooses the mechanic and the audience, and writes the script the agent speaks and
the message each customer receives. Code owns every figure: it prices the discount depth at
three levels and keeps whichever survives the guardrails, assigns the holdout, and measures
the outcome by subtraction.

This is enforced rather than requested. Merchant facing text from the model must carry
placeholders like `{lapsed_count}` and `{value_at_risk}`, and a digit guard in
`core/generate.py` rejects any text field containing an ASCII digit, a Devanagari digit, a
percent sign or a rupee sign outside a placeholder. A candidate that breaks the rule gets one
retry and is then dropped with its reason logged. Code fills every placeholder from the
simulator before anything is spoken or sent.

## Quick start

```bash
git clone <this repo>
cd dukaandost

python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cp .env.example .env               # Windows: Copy-Item .env.example .env
```

Generate the synthetic ledger and check it:

```bash
python -m data.generate
python -m scripts.check_data
```

Run the API and open the dashboard at `http://localhost:8000`:

```bash
uvicorn api.main:app --port 8000
```

Populate the learning curve with six campaigns, open a call, and check everything is ready:

```bash
python -m scripts.run_campaigns --campaigns 6
curl -X POST http://localhost:8000/call -H "Content-Type: application/json" -d "{}"
python -m scripts.demo_check
```

`demo_check` prints one line per check and exits non zero if anything is wrong. Pass
`--tunnel <url>` to also check the dashboard and the campaign launch endpoint answer from
outside.

### Running with no API key

Set `LLM_MODE=replay` and the entire loop runs offline from cached responses in
`data/llm_cache` and `data/audio_cache`, including the generated Hindi script and its
text to speech audio. No Sarvam key is needed and no network call is made. This was verified
by running the full loop with the key blanked and the HTTP transport blocked: every endpoint
answered, and the health check reported `key_present=False`.

The three modes are `live`, `record` (the default, which uses the cache when warm and calls
Sarvam otherwise) and `replay`.

## Architecture

The nightly loop, and the module that owns each stage:

| Stage | Module | What it does |
|---|---|---|
| Triage | `core/triage.py` | Pure arithmetic over the ledger. Lapsed regulars, off peak gaps, basket affinity gaps, and whether the shop is worth a call tonight. No LLM. |
| Generate | `core/generate.py` | Asks the model for candidate actions given categorical facts only. Resolves messy item names. Runs the digit guard. |
| Simulate | `core/simulate.py` | Prices every candidate at three discount depths against the shop's own history. Enforces the margin floor, the monthly discount budget and a positive profit requirement. |
| Approve | `voice/local_soundbox.py` | Speaks the filled Hindi script and reads the reply as approved, declined or unclear. `voice/base.py` holds the interface both backends satisfy. |
| Assign | `core/holdout.py` | Deterministic 85 and 15 split by hashing the campaign and customer id, so a retry produces the identical split. |
| Dispatch | `core/dispatch.py` | Renders one personalised Hindi message per treated customer. The control group is never rendered. |
| Measure | `core/measure.py` | Lift is treated conversion minus control conversion. Nothing here estimates anything. |
| Learn | `memory/store.py` | Pools measured lift across campaigns, shrunk toward the prior, and hands the next campaign a better number. |

Orchestration lives in n8n, which calls the FastAPI endpoints as workflow steps, including a
72 hour Wait node for the measurement and a webhook that receives the call outcome.

```
dukaandost/
├── api/          FastAPI service, one endpoint per stage, plus the mid call launch endpoint
├── core/         the nightly loop: ledger, triage, generation, simulation, holdout, dispatch, measurement
├── voice/        the dial() interface and the local soundbox implementation
├── memory/       campaign memory in SQLite, behind a graph interface
├── world/        the simulated world that stands in for reality, never imported by the agent
├── data/         the synthetic ledger generator, the databases, and the response caches
├── dashboard/    the six panel judge dashboard and the merchant approval page
├── workflows/    n8n workflow JSON, importable
├── scripts/      data checks, the six campaign run, and the demo day preflight
├── tests/        pytest, all offline
└── logs/         one JSON line per decision, including the nights it stays silent
```

## What is real and what is stubbed

Real, and doing the work:

- Triage, the simulator, the holdout, dispatch rendering, measurement and the learning
  update are all real code operating on real data.
- Sarvam chat completions, text to speech and speech to text all work against the live API.
- The dashboard reads only from the running service.

Stubbed or simplified, deliberately:

- **SQLite, not Postgres.** One merchant does not need a server. The ledger is opened read
  only everywhere, and campaign memory is a separate database so a bug in the agent cannot
  corrupt the evidence it reasons about.
- **The dashboard is vanilla HTML, CSS and JavaScript with no build step and no CDN.** That
  is a decision, not a shortcut: it has to open on venue wifi with nothing installed. A test
  asserts the page contains no external references. The learning chart is inline SVG drawn
  in the file.
- **WhatsApp dispatch is rendered and logged, not sent.** The WhatsApp Business API needs
  business verification and template approval, which is days rather than hours. Every
  message is genuinely composed, personalised and stored, and the dashboard shows them. A
  message still carrying an unfilled placeholder is blocked rather than sent.
- **Outcomes for measurement are simulated.** `world/outcomes.py` holds a hidden true uplift
  and rolls what each customer did. It lives in its own package and no agent module imports
  it, which a test enforces, because an agent that can read the answer key is not predicting
  anything.
- **Real telephony is not wired.** The voice interface has two implementations behind one
  `dial()` signature; the local soundbox is the one that runs.

## Cognee

The memory layer sits behind `memory/graph.py`, which offers three operations, writing a
campaign outcome, reading a merchant's history, and querying past campaigns by action type
and segment. There are two backends, selected by `MEMORY_BACKEND` and defaulting to SQLite.

The cognee backend is written against that interface and cannot run. `cognify()` needs two
providers: a chat model, which Sarvam serves because `sarvam-105b` is OpenAI compatible, and
an embedding model for its vector store, which Sarvam does not offer. Searching Sarvam's API
index for `embed` returns no matches. Running cognee would mean adding a second provider,
so it is installed and reports its own readiness rather than pretending.

Asking for the cognee backend returns SQLite with a note explaining why, and a test asserts
the fallback returns identical history.

## Results on the demo ledger

The synthetic ledger holds 400 customers and about 34,500 transactions across 18 months. The
generator tunes a cohort of 12 daily regulars to lapse three weeks before the simulated
today, and writes their ids to `data/ground_truth.json`.

- **Triage rediscovers all 12 from transactions alone.** It never reads
  `ground_truth.json`, and a test booby traps `open()` to prove it. It values the cohort at
  9,460 rupees a month against a true 9,385, which is 0.8 percent out.
- **The simulator refuses two of the three proposed action types at all three discount
  depths.** The off peak fill and the attach upsell lose money at LOW, MEDIUM and HIGH; only
  the lapsed winback survives, and the simulator picks MEDIUM. On a fresh install that reads
  148.03, 186.62 and 107.02 for the three depths.
- **Belief climbs from 0.140 to 0.254 across six campaigns against a hidden truth of 0.30,
  and error falls from 0.160 to 0.046.** Individual measured lifts swing from minus 0.20 to
  plus 0.50, because 15 percent of a 12 person cohort is a 2 person control group. The
  learning pools counts across campaigns rather than averaging noisy per campaign lifts,
  which is why a single bad measurement does not move the belief far.

Reproduce with `python -m scripts.run_campaigns --campaigns 6`.

## Versions

Read from `requirements.txt` and the virtual environment, not from memory.

| | |
|---|---|
| Python | 3.12.4 |
| FastAPI | 0.141.1 |
| pydantic | 2.13.5 |
| httpx | 0.28.1 |
| uvicorn | 0.53.0 |
| pandas | 3.0.5 |
| numpy | 2.5.3 |
| scikit-learn | 1.9.1 |
| python-dotenv | 1.2.3 |
| python-multipart | 0.0.32 |
| pytest | 9.1.1 |
| cognee | 1.5.4, installed but not in `requirements.txt` because it cannot run here |
| Sarvam chat | `sarvam-105b` |
| Sarvam text to speech | `bulbul:v3` |
| Sarvam speech to text | `saaras:v3` |
| n8n Cloud | 2.40.2 |
| cloudflared | 2026.9.1 |

```
$ python -m pytest tests/ -q
209 passed, 1 skipped in 48.64s
```

The one skip is the test that would compare the cognee and SQLite backends. It skips with its
reason printed rather than passing on a technicality.

## The data is synthetic

Every customer, transaction, name and item in this repo is generated by `data/generate.py`
from a fixed seed. There are no real customers, no real phone numbers and no real merchant.
Running `python -m data.generate` reproduces the identical ledger, which is why the numbers
above are stable. Customer names are Indian first names with a surname initial, drawn from a
separate generator so that adding them did not shift the rest of the ledger.

## The merchant never sees a phone number

Triage, the simulator and the holdout all work on customer ids. A name is resolved once, at
the last step, by `core/dispatch.py`, because writing "Namaste Ramesh" is the only part of
the system that needs to know who anyone is. A test asserts that triage output carries no
name field.

The merchant approves a strategy, not a contact list. In production Paytm sends on his
behalf, every message carries opt out, and frequency caps and quiet hours are enforced at the
orchestration layer. Insight, never identity.

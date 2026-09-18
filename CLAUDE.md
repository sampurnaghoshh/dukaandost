# Dukaan Dost

An autonomous growth analyst for Paytm merchants. Built for the Paytm Build for India AI Hackathon, Bengaluru Edition, Track 1 (Merchant Growth AI).

**Team Pick Me**: Sampurna Ghosh, Mohammed Nooman.

---

## Hard constraint: read this first

**Demo day is Saturday 19 September 2026, 9:00 AM to 5:00 PM.** Work started 6pm on 17 September. That is roughly 30 hours of build time before the event, plus 8 hours on site.

This is a **hiring hackathon**. Judges are Paytm product and engineering staff. The score is decided far more by a working live demo than by architectural completeness. Therefore:

> **Prefer a smaller thing that runs to a larger thing that almost runs.**
> If a feature cannot be demoed on stage in 90 seconds, it is not a priority.

When you are asked to add something and it is not on the critical path below, say so and propose deferring it.

---

## Hour zero: nothing exists yet

**Starting from scratch. No accounts, no keys, no repo.** Do this before writing a line of application code. Budget 45 minutes total. If any step stalls, take the fallback and move on; none of these are worth losing an hour to.

### 1. Sarvam (10 min), the only one that matters tonight

- Sign up at `dashboard.sarvam.ai`. Instant, no credit card, no approval queue.
- New accounts get free credits (₹100 to ₹1,000 depending on which page you read) that never expire. Speech APIs run roughly ₹15 to ₹45 per hour of audio, so a demo costs rupees, not thousands.
- Generate an API key manually: dashboard → API Keys → create. It is not auto-issued.
- Auth header is `api-subscription-key`. Note that **auth failures return 403, not 401**, so do not write error handling that branches on 401.
- Put it in `.env` as `SARVAM_API_KEY`.

**Then immediately try to rent a phone number** (dashboard → Voice Agents → phone numbers → "Rent from Sarvam"). This is the single biggest unknown in the whole build: it may need payment details, KYC, or may not be self-serve at all.

> **Exotel is not an option.** Their KYC takes 1 to 3 business days. Starting 18 September that lands after the event. Do not start it.

**Decision point, hard stop 7:30pm:** if a number is not provisioned and a test call has not rung your phone, abandon real telephony and build `LocalSoundbox` instead. Do not keep trying. The fallback demos nearly as well and the whole architecture is designed so the swap is one config flag.

### 2. LLM access (5 min)

`sarvam-105b` via the same key, `POST https://api.sarvam.ai/v1/chat/completions`, OpenAI-compatible. One key covers generation, STT and TTS. **Sarvam-M is deprecated and no longer served**, confirmed against the docs on 17 September 2026; `sarvam-105b` is the flagship and `sarvam-105b-conversations` is the variant tuned for voice agents. There is no documented `response_format` or JSON schema parameter, so ask for JSON in the prompt and parse defensively. The model thinks by default and the thinking counts against `max_tokens`, so send `reasoning_effort: null` for structured output. If Sarvam is unavailable or rate-limited, any LLM works for candidate generation; it is a swappable component.

### 3. n8n Cloud (10 min, HIGH PRIORITY)

**Redeem the hackathon voucher first thing.** Organisers issued one month of n8n Cloud:

```
2026-COMMUNITY-HACKATHON-INDIA-18D35A55
```

Redemption guide: `https://n8n.notion.site/voucher-code`. It expires one week after the event, so redeem tonight.

**Use Cloud, not local.** Two reasons:

1. **Public webhooks.** Sarvam's voice agent must call back into our system mid-conversation when the merchant approves. Cloud gives a public HTTPS webhook URL with no tunnelling. Local n8n would need ngrok, which is one more thing to fail on venue wifi.
2. **There is a separate prize.** Best Use of n8n wins every team member a year of n8n Cloud Pro, judged independently of the track prize. See the dedicated section below.

No mentorship is available from n8n this event. Resources: `docs.n8n.io`, `n8n.io/workflows` (template library), `community.n8n.io/c/tutorials/28`.

### 4. Cognee (10 min, P2)

`pip install cognee`, runs locally with a local vector store. No account needed for the open-source path. **If setup exceeds 30 minutes, use SQLite and call Cognee the production memory layer.** This is a sponsor-points item, not a critical-path item.

### 5. Repo and environment (10 min)

```bash
mkdir dukaan-dost && cd dukaan-dost
git init
python -m venv .venv && source .venv/bin/activate
printf '.venv/\n.env\n__pycache__/\n*.db\n' > .gitignore
touch .env
```

`.env` needs: `SARVAM_API_KEY`, `SARVAM_AGENT_ID` (once the voice agent exists), `SARVAM_PHONE_NUMBER`, `DEMO_PHONE` (your own number for test calls), `VOICE_MODE=telephony|local`.

### What costs money

Sarvam speech APIs, billed per hour of audio, covered by free credits for a demo of this size. Possibly the phone number rental. n8n Cloud is covered by the hackathon voucher. Cognee and the Python stack are free and local. **No credit card should be needed to reach a working demo.**

### Before any of the above

Check your email and the hackathon portal for organiser-issued Sarvam credits or a partner code. Sponsors usually provide them and you may already have something sitting unclaimed. Two minutes, potentially saves the whole telephony budget.

---

## The idea in one paragraph

A shopkeeper knows whether today was busy. He cannot see that eleven regulars quietly stopped coming, that Tuesday afternoons have been dead for six weeks, or that his tea buyers never add a snack. A large company hires an analyst to find this and a marketer to act on it. A tea stall cannot. Dukaan Dost is that analyst and that marketer, running every night. It scans the shop's transactions, invents candidate growth actions, simulates each against that shop's own history, calls the owner for a ten second spoken approval in his own language, dispatches the campaign, holds back a random control group, measures true lift at 72 hours, and writes the outcome back to memory so next week's prediction is better.

## The one line that wins the room

> **The language model never produces a number.**
> The LLM generates hypotheses and merchant-facing language. The statistical layer estimates value. A randomised holdout decides the truth.

Keep this true in the code. If you ever find yourself asking an LLM to output a rupee figure, a lift percentage or a probability, that is a bug.

---

## The nightly loop (core architecture)

```
  ┌─────────────────────────────────────────────────────────┐
  │  EVERY MERCHANT, EVERY NIGHT : PURE ARITHMETIC         │
  └─────────────────────────────────────────────────────────┘
       Transactions  →  Triage  →  "Worth a call?"
                                        │ NO → no call tonight (~97%)
                                        ▼ YES
  ┌─────────────────────────────────────────────────────────┐
  │  ONLY THE ~3% THAT CLEAR THRESHOLD : REASONING LAYER   │
  └─────────────────────────────────────────────────────────┘
       Memory  →  LLM generates candidates  →  Simulator ranks
                                                    │
                                                    ▼
                                              Voice call
                                                    │
                                    NO → logged as declined
                                                    ▼ "haan"
       Dispatch (85% treated / 15% holdout)  →  Measure at 72h
                                                    │
                                        Lift = treated minus control
                                                    │
                                        └──→ written back to memory
```

### Stage detail

1. **Triage.** Pure arithmetic, runs on every merchant. Opportunity score from lapsed-regular value at risk, off-peak gaps, basket affinity gaps. No LLM. Cheap enough to run on 40M merchants. This is the answer to "what does this cost at scale?"
2. **Memory.** Per-merchant graph: customers, items, temporal patterns, and every past campaign with what was proposed, approved, predicted and actually delivered.
3. **Generate.** LLM proposes candidate interventions openly, written for this shop. Not selected from a hardcoded menu. Output is structured JSON describing the action, the target segment and the offer, with **no numeric estimates**.
4. **Simulate.** Scores each candidate against the shop's own history. Returns expected incremental revenue, discount cost and expected profit. Refuses negative-profit actions. Respects a monthly discount budget.
5. **Approve.** Outbound voice call in Hindi. Ten seconds. Merchant says haan. Agent invokes the campaign API mid-call.
6. **Dispatch.** 85% treated, 15% random holdout.
7. **Measure.** At 72h, lift = treated conversion minus control conversion. Never a model estimate.
8. **Learn.** Prediction error written back. Campaign 6 should predict better than campaign 1, and this must be visible on a chart.

---

## Scope: what is IN and what is CUT

### Critical path (must work on stage)

| Priority | Feature | Why |
|---|---|---|
| P0 | Live outbound voice call in Hindi, merchant says haan, campaign fires mid-call | The moment judges remember |
| P0 | Simulator with ranked candidate list and expected profit | Answers "why this action?" |
| P0 | Randomised holdout, lift = treated minus control | Answers "how do you know it was you?" |
| P0 | Learning curve: predicted vs actual converging over 6 campaigns | Proves it is a system, not a demo |
| P1 | Judge dashboard showing the agent's reasoning trace | Makes the invisible visible |
| P1 | LLM open-ended candidate generation | The "best use of AI" argument |
| P1 | n8n Cloud as the real orchestration backbone | Second prize category, and it hosts our public webhooks |
| P2 | Cognee as the memory layer | Sponsor points, powers the learning loop |

### Explicitly cut

- **WhatsApp Business API.** Onboarding needs business verification and template approval. Days, not hours. **Do not attempt it.** Messages render live in the judge dashboard with real personalised Hindi text. For the demo beat, one real message goes to a judge's phone via a click-to-chat link from a personal WhatsApp. Say openly on stage that dispatch is stubbed and WABA is the production path.
- **Multiple merchants.** One merchant, fully working.
- **Multiple campaign types.** One type end to end (lapsed-customer winback). A second only if Friday goes well.
- **Auth, multi-tenancy, real Paytm APIs, deployment.** All out.
- **Deep Cognee graph modelling.** Use it for campaign history. If integration exceeds two hours, fall back to Postgres or SQLite and describe Cognee as the production memory layer.

---

## Tech stack

- **Python 3.11+**, FastAPI backend
- **Sarvam AI**: Voice Agents for the outbound call (number rented from Sarvam; Exotel BYO is ruled out, see Hour Zero), Indic STT/TTS, `sarvam-105b` for generation. The agent's **API tool** calls our backend mid-conversation to launch the campaign.
- **Cognee**: per-merchant memory and campaign history
- **n8n**: nightly trigger, call dispatch, webhook branching on approved/declined/no-answer, holdout assignment, 72h measurement job
- **pandas, scikit-learn**: triage and simulation
- **React** (or plain HTML if faster): judge dashboard
- **SQLite** for local state (Postgres is overkill for one merchant)

### Voice layer must be swappable

Define one interface and write two implementations. This is not optional; it is the demo-day insurance policy.

```python
def dial(merchant_id: str, brief: Brief) -> Decision:
    """Returns {approved: bool, modifications: dict, transcript: str}"""
```

- `SarvamTelephony`: real outbound call via Sarvam Instant Outbound
- `LocalSoundbox`: same script through Sarvam TTS in a browser tab, mic listening for the reply

Same `brief` in, same `Decision` out. One config flag switches them. Everything upstream (triage, simulator, holdout, dispatch, measurement) is identical either way.

---

## Winning "Best Use of n8n"

This is a second, independently judged prize. Most teams will use n8n as a cron trigger wrapped around a Python script, which is not a use of n8n, it is a scheduler. Ours must be the actual orchestration backbone: if you deleted the n8n workflows, the product would stop working.

### What the nightly workflow must contain

The whole loop lives in n8n, calling our FastAPI endpoints as steps:

1. **Schedule trigger** at 9pm, fans out over merchants
2. **HTTP node** calls `/triage`, returns an opportunity score
3. **IF node**: score below threshold, terminate the branch and log "no call tonight"
4. **HTTP nodes** call memory, then LLM generation, then the simulator
5. **HTTP node** fires Sarvam Instant Outbound to place the call
6. **Webhook node** receives Sarvam's callback when the call ends
7. **Switch node** branches three ways on `approved` / `declined` / `no-answer`
8. On approved: **Code node** assigns the 85/15 holdout split, then dispatch with **retry on fail** configured
9. **Wait node** for 72 hours, then call `/measure`
10. **HTTP node** writes lift and prediction error back to memory

### Things that specifically score points

- **A three-way Switch**, not a binary IF. Declined and no-answer are different outcomes handled differently.
- **The Wait node** for the 72h measurement. It is the n8n feature that makes a long-running business process possible without us writing a scheduler, and almost nobody uses it in a hackathon.
- **Error handling**: retry on fail for dispatch, plus an error workflow that logs failures rather than dying silently.
- **Sub-workflows**: extract "run one merchant" as a sub-workflow called by the parent. Shows structural thinking rather than one sprawling canvas.
- **Webhook receiving a real third-party callback** from Sarvam. This is the hardest part to fake and the most convincing.
- **A clean, readable canvas.** Screenshot it for the deck and have it open in a browser tab on demo day. Judges will look at the picture before they read anything.

### Stretch goal, Friday only if ahead of schedule

Have the agent **generate n8n workflow JSON for each new campaign type it invents**, posted through the n8n API. New campaign type, new executable workflow, no human writing it. AI producing running automation is a genuine wow and fits the open action space perfectly. It is also real extra work and a new failure mode, so build the fixed pipeline first and attempt this only if Friday afternoon is clear.

### Say it out loud on stage

One line during the pitch: "the entire nightly loop is orchestrated in n8n, including a 72 hour wait node for measurement and a webhook that receives the call outcome from Sarvam." Judges scoring the n8n prize may not be the same people scoring the track, so make the integration audible, not just visible.

---

## Repo layout

```
dukaan-dost/
├── CLAUDE.md
├── STATUS.md                # running build log, current state, next step
├── requirements.txt
├── .env.example             # variable names only, no values
├── data/
│   ├── generate.py          # synthetic merchant ledger
│   ├── dukaan.db            # SQLite ledger (gitignored)
│   └── ground_truth.json    # evaluation only, never read by agent code
├── core/
│   ├── ledger.py            # read only SQLite access shared by triage and simulate
│   ├── triage.py            # arithmetic opportunity score
│   ├── simulate.py          # candidate scoring, expected profit, guardrails
│   ├── generate.py          # LLM candidate generation (no numbers)
│   ├── fixtures.py          # placeholder candidates, replaced by generate.py in Step 3
│   ├── holdout.py           # random assignment, 85/15
│   ├── measure.py           # lift = treated minus control
│   ├── campaign.py          # one campaign: assign, predict, dispatch, observe, measure
│   ├── dispatch.py          # renders per customer Hindi messages, treated only
│   ├── speech.py            # Sarvam text to speech and speech to text, cached
│   ├── llm.py               # Sarvam chat completions, record and replay
│   └── run_night.py         # one nightly cycle end to end, CLI entry point
├── voice/
│   ├── base.py              # dial() interface, Brief, Decision
│   ├── sarvam_telephony.py
│   └── local_soundbox.py
├── memory/
│   ├── store.py             # campaign memory in SQLite, the demo path
│   └── graph.py             # the memory interface, SQLite and Cognee backends
├── api/
│   └── main.py              # FastAPI, incl. mid-call campaign endpoint
├── dashboard/               # judge view: reasoning trace, learning curve
│   ├── index.html           # the six panel judge dashboard, plain HTML and vanilla JS
│   ├── soundbox.html        # merchant approval page, plays the script, Haan and Nahi buttons
│   └── learning.json        # exported learning series, written by scripts/run_campaigns.py
├── workflows/               # n8n JSON exports
├── world/
│   └── outcomes.py          # THE SIMULATED WORLD, holds the hidden truth, never imported by the agent
├── scripts/
│   ├── check_data.py        # validates the generated ledger, exits non-zero on failure
│   └── run_campaigns.py     # six campaigns end to end, writes the learning curve
├── tests/                   # pytest
└── logs/                    # decision logs the dashboard reads (gitignored)
```

---

## Synthetic data requirements

One merchant (a tea stall), ~400 customers, 18 months of UPI transactions. Quality here is the difference between a demo that feels real and one that does not. Must include:

- Weekday and time-of-day patterns, with a genuinely dead Tuesday afternoon
- Festival spikes (Diwali, Holi)
- A cohort of ~12 regulars who lapse three weeks before "today"
- Basket affinity: tea buyers rarely add snacks
- An Indian first name and surname initial per customer, stored on the customers table. The name is resolved at dispatch and nowhere earlier: triage, the simulator and the holdout all work on ids.
- Messy Hinglish item names on purpose: `chai`, `CHAI 10`, `tea spl`, `चाय`. The LLM resolving these to one product is a real capability worth showing.
- Realistic ticket sizes for a Bengaluru tea stall (₹10 to ₹60)

---

## Demo script (90 seconds), build toward this

1. A phone on the judging table rings. Agent speaks Hindi: *"Namaste Ramesh bhai. Aapke 12 regular grahak teen hafte se nahi aaye. Mahine ka lagbhag ₹9,400 ka nuksan. Unko chai pe 10% offer bhejun?"*
2. Presenter: *"Haan, bhej do."*
3. Dashboard shows personalised messages generating per customer, different offers by segment. Agent confirms on the line: *"Bhej diya, 12 logon ko."*
4. Fast-forward the 72h simulation: 5 returned, ₹3,100 recovered, counter moves.
5. Reasoning trace on screen: why these 12, why this offer, what was predicted, what it learned.

**Ask a judge for their number before the pitch so one real message lands on a real phone.**

### Demo-day decision rule

At 8:45 AM, place one live call on the venue network. Connects cleanly twice → use real telephony. Stutters → flip the config to `LocalSoundbox` and never mention it.

**A clean recording of the full flow must exist by Friday 8pm.** If the live call fails mid-pitch, cut to the video and say "that's the venue wifi, here it is running this morning."

---

## The five questions to have answers for

Prepared answers matter more than code quality here.

1. **"What does this cost per merchant per night?"** Arithmetic triage runs on everyone; only the ~3% clearing the profit threshold reach the LLM. Turns a 40M-call problem into a ~1M-call problem.
2. **"How do you handle a merchant with two weeks of data?"** Borrow priors from similar merchants (category, locality, ticket size) and shrink toward the shop's own data as it accumulates. Hierarchical modelling.
3. **"Whose customer data is this, and who consented?"** The merchant never sees a phone number. Paytm sends on his behalf; he approves a strategy, not a contact list. Consent sits with Paytm's existing terms, every message carries opt-out, DND and frequency caps enforced at the orchestration layer. DPDP-aligned: insight, never identity.
4. **"Why would a merchant trust software to spend his money?"** Capped, reversible, shows its work. Expected profit stated before approval, measured result reported after, and it regularly says nothing is worth doing today.
5. **"Isn't this just a recommendation engine?"** A recommendation engine stops at the suggestion. This one approves, executes, measures against a control group, and updates.

---

## Conventions

- **No em-dashes or en-dashes in any user-facing string, comment or doc.** Use commas, colons or separate sentences. Hyphens inside compound words are fine.
- Merchant-facing copy is Hindi or Hinglish, written by the LLM, never hardcoded English strings translated at runtime.
- Every LLM call returns structured JSON. Parse it safely; never regex the output.
- Log every decision the agent makes, including the ones where it chose to stay silent. The dashboard reads from these logs.
- Guardrails are code, not prompts: margin floor, discount budget, frequency caps and quiet hours are enforced in `simulate.py` and `holdout.py`, not requested politely of the model.
- Merchant-facing text from the LLM uses placeholders, for example `{lapsed_count}` and `{value_at_risk}`. Code fills every number from the simulator before the text is spoken or sent. The model writes the sentence, never the figure inside it.
- Keep secrets in `.env`, never committed.

## Commands

```bash
# setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# generate the merchant ledger
python -m data.generate

# run one nightly cycle end to end
python -m core.run_night --merchant tea_stall_01

# api + dashboard
uvicorn api.main:app --reload
```

---

## Build order (do not skip ahead)

**Thu 17 Sep, 6pm to midnight**
0. **Hour zero setup (see section above).** Accounts, keys, repo, venv. 45 minutes maximum.
1. Sarvam number rented, one real call to your own phone saying one hardcoded Hindi sentence. **Hard stop at 7:30pm:** if it has not rung, build `LocalSoundbox` instead and stop trying.
2. Synthetic data generator.
3. Triage plus simulator. Plain Python, no LLM.

By midnight: one command prints the 12 lapsed customers, the value at risk, and a ranked action list.

**Fri 18 Sep, morning**
LLM candidate generation, holdout assignment, 72h measurement simulation, FastAPI endpoints for every stage so n8n can call them as workflow steps, judge dashboard in plain HTML.

**Fri 18 Sep, afternoon**
n8n Cloud wiring (the full nightly workflow, not a cron), learning curve chart, Cognee campaign memory on a one hour timebox. If Cognee is not working after that hour, fall back to SQLite and describe Cognee as the production memory layer.

**Fri 18 Sep, 8pm**
Backup video of the full flow recorded. This is a hard deadline, not a stretch goal.

**Sat 19 Sep**
Polish, handle whatever on-site twist appears, rehearse the 90 seconds until it is boring.

# STATUS

Build log for Dukaan Dost. Newest step at the top.

## Step 4: local soundbox and API (Fri 18 Sep, early)

Status: **done, 86 tests pass, the whole loop runs offline, Sarvam speech is live.**

### 1. Bug check: the Brief already used the ranked winner

Checked first, as asked. It was **not** a bug. `simulate.rank()` sorts every candidate by
expected profit, fills the accepted list in that order, and sets `recommended` to
`accepted[0]`; `run_night.build_brief()` is handed that object and nothing else. The
model's preferred candidate has never reached the Brief except by being the most
profitable one.

Two tests now pin it down so a later refactor cannot quietly break it. One feeds the
candidates in deliberately wrong order, attach upsell first and winback last, and asserts
the winback still wins. The other asserts `recommended` equals the maximum expected profit
across the accepted list.

One real defect did turn up next to it, worth recording: `_log()` in `api/main.py` took a
positional parameter called `decision` while several callers also passed `decision=` in
their keyword extras, which raised `TypeError: got multiple values`. Renamed to `outcome`.
A second one in the same place: `run_night.log_decision()` binds its log path as a default
argument, evaluated at import, so pointing the path elsewhere was silently ignored and the
tests were asserting against a file nothing was writing to. The API now passes the path
explicitly at call time.

### 2. Offer depth moved into code, and the ranking changed

The LLM now proposes `action_type`, `target_segment`, `offer_item`, a rationale and the
customer message. It no longer proposes an offer level at all, and the prompt tells it not
to hint at one. `simulate.rank()` prices LOW, MEDIUM and HIGH for **every** candidate,
keeps the most profitable level that survives the guardrails and fits what is left of the
discount budget, and refuses the candidate only if all three levels fail.

The full grid comes back on the ranking as `level_grid`, one row per candidate with all
three levels, so the dashboard can show the working rather than the conclusion.

```
candidate                                       LOW     MEDIUM       HIGH   chosen
lapsed_winback_lapsed_regulars_1            148.03     186.62     107.02    MEDIUM
offpeak_fill_offpeak_slot_2                  -4.09x     -8.23x    -24.66x   REFUSED
attach_upsell_anchor_buyers_3              -103.00x   -236.06x   -522.64x   REFUSED
offpeak_fill_offpeak_slot_4                  -4.09x     -8.23x    -24.66x   REFUSED
attach_upsell_anchor_buyers_5              -103.00x   -236.06x   -522.64x   REFUSED
```

**The winback is worth more than it was.** It ran at 107.02 in Step 3 because the model
picked HIGH on a hunch. Priced properly it is worth **186.62 at MEDIUM**, twenty percent
off rather than thirty five. The model's taste in discounts was costing about eighty
rupees a campaign, which is the entire argument for this change in one line.

**Yes, the fifty percent winback now survives, at a shallower level.** Asked plainly, so
answered plainly: the `winback_lapsed_chai_50` fixture is accepted, at MEDIUM, with
identical numbers to the ten percent fixture. It is not that the guardrails got weaker. It
is that a candidate can no longer ask for fifty percent off at all, so the two fixtures now
describe the same action and the deep one has nothing left to distinguish it. The depth is
not the proposer's to choose, whether the proposer is a language model or a hand written
placeholder.

That does retire one Step 2 test. `test_deep_discount_breaks_the_margin_floor` asserted a
candidate asking for fifty percent was refused on the margin floor, and it was checking a
route that no longer exists. It is replaced by three tests that are strictly stronger:

- every depth on the code owned menu already clears the margin floor, so the floor cannot
  be breached by construction rather than by catching a bad proposal
- put an illegal depth on the menu and the grid refuses that level and runs the candidate
  at a legal one, which proves the floor still bites inside the sweep
- the fifty percent fixture comes out at a depth drawn from the menu, below fifty

Worth saying out loud on stage: with a fifty five percent gross margin and a twenty five
percent floor, the deepest legal discount is forty percent, and the menu tops out at thirty
five. The margin floor has stopped being a filter on proposals and become a constraint on
what the system is able to offer. That is a better place for it.

### 3. Sarvam speech findings, read off docs.sarvam.ai on 18 Sep 2026

| | Text to speech | Speech to text |
|---|---|---|
| Endpoint | `POST https://api.sarvam.ai/text-to-speech` | `POST https://api.sarvam.ai/speech-to-text` |
| Body | JSON | `multipart/form-data` |
| Model | **`bulbul:v3`**, legacy `bulbul:v2` | **`saaras:v3`** default, `saaras:v4` latest |
| Response | `{"audios": ["<base64 wav>"]}`, join then decode | `{"transcript": "..."}` |
| Limits | 2,500 characters per request | 30 seconds per request, batch API beyond |
| Rates | 8000, 16000, 22050, 24000 Hz, plus 32000, 44100, 48000 on v3 REST only. Default 24000 | raw PCM needs `input_audio_codec` and must be 16 kHz |
| Formats | WAV out | WAV, MP3, AAC, AIFF, OGG, OPUS, FLAC, MP4, AMR, WMA, **WebM**, auto detected |
| Other | 30 plus speakers, `shubh` default, pace 0.5x to 2.0x | modes transcribe, translate, verbatim, translit, codemix |

The single most useful line in either document is that speech to text auto detects
**WebM**. Browser `MediaRecorder` hands us WebM and nothing else without a fight, so the
microphone blob goes straight up with no transcoding step, no ffmpeg dependency and no
conversion bug to debug at eight in the morning.

`bulbul:v3` rendered the full 260 character Hindi script to a 938 KB WAV on the first try.
Both models are in `.env.example` as `SARVAM_TTS_MODEL`, `SARVAM_TTS_SPEAKER` and
`SARVAM_STT_MODEL`.

### 4. The soundbox

`voice/local_soundbox.py` implements `dial(merchant_id, brief) -> Decision` from
`voice/base.py`. The flow: `dial()` renders the filled Hindi script through Sarvam TTS,
parks a pending call, and returns immediately. The browser page plays the WAV, records four
seconds of microphone audio, posts it back, Sarvam transcribes it and `classify_reply()`
reads it.

**The two buttons are the point.** HAAN and NAHI sit on the page at 27 point type and
produce a `Decision` that is indistinguishable from the spoken one: same fields, same
values, only `modifications.via` differs. There is a test asserting the two paths return
the same shape. They need no microphone permission, no network and no Sarvam credit.

Reading the answer is phrase matching, not a model. Forty plus approval and refusal
phrases in Roman and Devanagari, whole word matched.

- **Unclear reprompts once, then stops.** A second unclear reply ends the call as
  undecided rather than guessing. Guessing here spends a merchant's money.
- **A reply holding both a yes and a no is unclear**, because someone saying "haan nahi
  nahi rehne do" is changing their mind.
- **Except when one is a fragment of the other.** "mat bhejo" contains "bhejo", which is an
  approval phrase. The longer, more specific phrase wins, so "mat bhejo" is a refusal.

### 5. The API

Eight endpoints, each one a step n8n can call on Friday, plus three that drive the page.
Every one validates with pydantic and appends exactly one line to `logs/decisions.jsonl`.
`/campaign/launch` is the mid call endpoint the Sarvam agent tool will hit in Step 5; it
records the approval, returns a campaign id and reports the treated and holdout counts.
`/measure` answers `not_ready` on purpose until Step 5 builds it.

### How to launch the soundbox

```bash
uvicorn api.main:app --reload
curl -s -X POST localhost:8000/call -H "Content-Type: application/json" -d "{}"
# open the soundbox_url it returns, for example
#   http://localhost:8000/soundbox?call_id=call_70c3283ee4
```

Press **Play the call** to hear the Hindi script, then either **Answer with the mic** or
one of the two big buttons. With `LLM_MODE=replay` the whole thing runs with the network
unplugged, because the script text, the candidates and the WAV are all cached.

### Decisions made

1. **A cheaper level is better than no campaign.** When the best level does not fit the
   remaining discount budget, the ranking drops to the most profitable level that does,
   rather than refusing the candidate outright.
2. **Bare "ji" is not an approval.** It was in the list, and it should not have been: a
   shopkeeper saying "ji?" is asking you to repeat yourself, not agreeing to spend money.
3. **`offer_level` stayed on the generated candidate model as an ignored field** so cached
   answers recorded before this step still load instead of failing validation.
4. **The network blocker moved to the transport layer.** `httpx.HTTPTransport` and
   `AsyncHTTPTransport` are patched rather than `httpx.Client.post`, because FastAPI's
   TestClient is itself an httpx client and the old blocker was strangling it.

### A real bug the tests caught, worth knowing

The first version of the reply normaliser kept characters where `character.isalnum()` was
true and turned everything else into a space. Devanagari vowel signs and the anusvara are
combining marks, category Mc and Mn, and **`isalnum()` is false for them**, so every Hindi
word was being torn into pieces and not one Devanagari approval matched. The normaliser now
strips a named punctuation set and keeps everything else. Anything doing string work on
Indic text should assume this trap is present until proved otherwise.

### Not done, by instruction

No real telephony, no holdout assignment, no dispatch, no measurement, no learning curve,
no n8n, no Cognee. `.env` was never opened, printed or edited. Network was limited to
`docs.sarvam.ai` and `api.sarvam.ai`. Nothing pushed.

### The whole loop, offline, no network

```
GET /health                200  ok
    mode=replay llm=sarvam-105b tts=bulbul:v3 stt=saaras:v3 key_present=True
POST /triage               200  ok
    12 lapsed, value at risk 9460, worth a call True
POST /generate             200  ok
    5 candidates from the llm
POST /simulate             200  ok

    candidate                                       LOW     MEDIUM       HIGH   chosen
    lapsed_winback_lapsed_regulars_1            148.03     186.62     107.02    MEDIUM
    offpeak_fill_offpeak_slot_2                  -4.09x     -8.23x    -24.66x   REFUSED
    attach_upsell_anchor_buyers_3              -103.00x   -236.06x   -522.64x   REFUSED
    offpeak_fill_offpeak_slot_4                  -4.09x     -8.23x    -24.66x   REFUSED
    attach_upsell_anchor_buyers_5              -103.00x   -236.06x   -522.64x   REFUSED

POST /call                 200  ok
    call call_70c3283ee4, brief for lapsed_winback_lapsed_regulars_1 at level MEDIUM
    script: नमस्ते Ramesh जी, आपके 12 पुराने रेगुलर कस्टमर आना बंद कर गए हैं, जिससे आपको हर महीने करीब ₹9,460 का नुकसान हो रहा है। अगर हम उनके पास एक 20% chhoot chai pe भेजें तो कैसा रहेगा? हम सिर्फ 10 लोगों को ही मैसेज करेंगे, बाकी 2 को बाहर रखेंगे ताकि हमें पता चले कि ऑफर सच में काम कर रहा है या नहीं। क्या मैं भेज दूँ?
GET /soundbox/state        200  ok
    audio rendered from cache, no network
GET /soundbox              200  ok  (both fallback buttons present)
GET /soundbox/audio        200  ok  (938732 bytes of wav)
POST /soundbox/button      200  ok
    approved via button, transcript 'Haan, bhej do.'
POST /campaign/launch      200  ok
    campaign camp_154001f721, 10 treated and 2 held back
POST /measure              200  ok
    not_ready, as expected until Step 5
GET /decisions             200  ok
    8 decision lines logged: health, triage, generate, simulate, call, voice_reply, campaign_launch, measure

ALL STEPS PASSED OFFLINE, NO NETWORK TOUCHED
```

### Test suite

```
$ python -m pytest tests/ -q
86 passed in 6.77s

$ python -m scripts.check_data
all checks passed
```

**Next: Step 5, holdout assignment, dispatch, measurement and the learning curve.**

---

## Step 3: LLM generation and the call brief (Thu 17 Sep, night)

Status: **done, 40 tests pass, Step 1 and Step 2 checks all still pass. Sarvam is live.**

### Sarvam findings, read off docs.sarvam.ai on 17 Sep 2026

| | |
|---|---|
| Endpoint | `POST https://api.sarvam.ai/v1/chat/completions` |
| Auth header | `api-subscription-key`, confirmed. `Authorization: Bearer` also accepted |
| Auth failure | 403, not 401, exactly as CLAUDE.md warned |
| Model | **`sarvam-105b`**. `sarvam-105b-conversations` is the voice agent variant |
| JSON output | **No `response_format`, no `json_schema`, no JSON mode documented anywhere** |
| Parameters | messages, temperature, top_p, max_tokens, seed, stop, reasoning_effort, wiki_grounding, presence_penalty, frequency_penalty |

Two findings that cost real time and are worth knowing before Friday:

1. **Sarvam-M is deprecated and no longer served.** CLAUDE.md named it as the generation
   model. The docs now say plainly that it has been removed from the Chat Completions API
   and to migrate to `sarvam-105b`. CLAUDE.md is corrected.
2. **The model thinks by default, and the thinking is billed against `max_tokens`.** The
   first live call came back with `content: null`, a populated `reasoning_content` and
   `finish_reason: length`: it had spent the entire token budget deliberating and never
   answered. Sending `reasoning_effort: null` disables reasoning completely and fixes it.
   For prompts that want a fixed JSON shape rather than deliberation this is the right
   setting anyway, and it is faster and cheaper.

Because there is no JSON mode, structure is asked for in the prompt and enforced on our
side: strip code fences by slicing, `json.loads`, validate with pydantic. No regex touches
a model response anywhere in the codebase.

### What was built

- **`core/llm.py`**, new. Sarvam client, sixty second timeout, one retry on timeouts, 429
  and 5xx, immediate stop on 403. `LLM_MODE` is live, record or replay. Successful
  responses are saved to `data/llm_cache/<hash>.json`, keyed on a hash of the exact
  request. The key is read from the environment and never printed, logged, returned in an
  error message or written to the cache, and there is a test that the cache files contain
  neither the key nor the header name.
- **`core/generate.py`**, now real. `resolve_items()`, `propose()`, `call_script()`, the
  number guard, and the fillers that put code's numbers into the model's sentences.
- **`core/holdout.py`**. `HOLDOUT_SHARE = 0.15` and a counting helper. No assignment logic
  yet, that is Step 5.
- **`voice/base.py`**. `Brief`, `BriefNumbers` and `Decision` as pydantic models, plus the
  `dial()` signature that raises NotImplementedError. No backend yet.
- **`core/simulate.py`**, corrected. See below.
- **`core/run_night.py`**, rewired. Generation, simulation, brief, filled Hindi script.
- **`tests/test_generate.py`**, 18 new tests. Every one runs with httpx hard blocked.

### 1. The simulator correctness fix

The old single rate was wrong, and the instruction to split it was right. It shrank zero
returns out of twelve toward a prior that was itself a guess about how people respond to an
offer, so "would have come back anyway" and "came back because we called" were the same
number. The holdout exists precisely to tell those apart, so folding them together made the
measurement in Step 5 unable to correct anything.

Now there are two, and they never touch:

```
baseline_return_rate  0.0500  MEASURED  12 unprompted lapse episodes in this shop's ledger,
                                        0 returned, shrunk toward a 0.08 prior worth 20
                                        observations
offer_uplift          PRIOR             PRIOR_OFFER_UPLIFT = LOW 0.08, MEDIUM 0.14,
                                        HIGH 0.20, additive on top of baseline
```

Every episode behind the baseline is genuinely unprompted, because this shop has never run
a campaign. Incremental returners are `treated_count x offer_uplift`. The discount is paid
on baseline plus incremental, because a coupon does not ask why somebody came back. Only
the treated group is priced at all, since the holdout is never messaged.

Gap shaped actions get gap shaped priors, `PRIOR_OFFPEAK_GAP_CAPTURE` and
`PRIOR_ATTACH_GAP_CAPTURE`, expressed as the share of a **measured** gap an offer closes.
They cannot invent headroom that the shop's own data does not show.

**Two of the three expected outcomes held, one moved. Reporting it rather than tuning it.**

| Expected | Result |
|---|---|
| Winback still ranks first | Yes, and it is the only accepted action |
| The fifty percent winback still refused | Yes, on both the margin floor and negative profit |
| Tuesday stays marginal | Marginal, but it crossed zero: was +3.91, now -8.23, so it is now refused |

The Tuesday flip is a consequence of the fix, not of tuning. Three things moved against it
at once and no constant was touched to make it happen: the old multiplicative uplift
applied to the whole slot baseline, whereas the new additive capture applies only to the
measured gap; the discount is now charged against baseline plus incremental rather than a
multiplied total; and only 85 percent of the segment is priced now that the holdout is
carved out. A campaign that was worth four rupees was always inside the noise, and the
corrected arithmetic simply puts it on the other side of zero. The attach upsell moved the
same way, from +268.96 to -103.00, for the same reason. Both refusals are honest: paying a
discount to everyone who already attaches a snack, in order to win a few more, does lose
money.

### 2. Record and replay

`LLM_MODE=replay` is the demo day insurance policy. With `httpx.Client.post` replaced by a
function that raises, the entire nightly loop runs to completion and prints identical
output, because every model answer comes from `data/llm_cache/`. The cache is committed, so
it works on a fresh clone with no key at all. There is a test for exactly this, and another
proving replay refuses to invent an answer it has not got rather than silently falling back.

### 3. What the model actually produced

**Item resolution: 38 of 41 raw spellings clustered correctly, 92.7 percent.** All eleven
chai variants collapsed to one key, including `CHAI 10`, `tea spl`, `chay` and the
Devanagari `चाय`. The only errors are three coffee names: it split `filter kaapi`, `kaapi`
and `Filter Coffee` away from `coffee`, `cofee` and `कॉफी`. That is arguably the model being
right and our synthetic data being wrong, since filter coffee and instant coffee are
genuinely different drinks in Bengaluru, but it is scored as an error because the ground
truth says one product.

**The model chose a HIGH offer level and the simulator priced it at 107.02 rupees**, where
the hand written LOW placeholder was worth 148.03. The model is not optimising the discount
depth, it is picking a vibe, and code turns the vibe into a percentage. Worth noting for
Friday: having the simulator price all three levels of each proposed action and keep the
best is a small change and a genuine improvement, but it is not what Step 3 asked for and
the numbers above are what the system actually does today.

### 4. The number guard

Implemented with `string.Formatter().parse()`, which hands back literal text and
placeholder names separately, so the check needs no regex. It rejects ASCII digits,
Devanagari digits, the percent sign and the rupee sign anywhere in literal text, and also
rejects placeholders code cannot fill and malformed braces. A tripped guard gets one retry
that tells the model exactly what was wrong, then the candidate is dropped and logged.

The call script has a stricter rule still: all six placeholders are **required**, not merely
allowed. The first script the model wrote omitted `merchant_name` and `treated_count`, which
would have had the agent ask the merchant to approve a campaign without telling him how many
people it would message. The guard caught it, the retry fixed it.

### Decisions made

1. **`action_type` is a free string, not an enum.** The open action space is the "best use
   of AI" argument, and the simulator already refuses what it cannot price. Constraining
   the field would have traded that away for a schema that validates more often.
2. **`offer_item` is resolved against the menu, not trusted.** The model returned "a
   complimentary bun" as a product key. Code now accepts only a key that appears in the
   resolved product list and falls back to the segment anchor otherwise.
3. **`message_template` defaults to empty rather than being a required field.** The model
   dropped it entirely on the first attempt and pydantic rejected the whole batch, which
   threw away four good candidates over one missing field. Now a missing message is a
   candidate level problem that the retry can fix and the guard can drop.
4. **The owner's name is the first word of the shop name.** The ledger stores "Ramesh Tea
   Stall" and has no proprietor field, and "Namaste Ramesh Tea Stall ji" is not a sentence
   anyone would say. `owner_name()` is a documented demo stand in that a real merchant
   profile removes.
5. **`seed` is sent on every request.** Sarvam documents it for repeatable results, which
   makes the record and replay cache keys meaningful rather than a lucky coincidence.
6. **The customer id stands in for the customer name** in the sample messages. Inventing
   names would be inventing data, and the privacy answer is that the merchant never sees
   the identity anyway.

### Verified, not assumed

- Live call to Sarvam succeeded, model `sarvam-105b`, no 403.
- Replay with `httpx.Client.post` raising runs the full loop and exits zero.
- No cache file contains the key or the string `api-subscription-key`.
- `.env` was never opened, printed or edited. Only `.env.example` was written, and it
  carries variable names and the model id, no values.
- Network traffic was limited to `docs.sarvam.ai` and `api.sarvam.ai`.

### Word for word output

```
CALL SCRIPT TEMPLATE, as sarvam-105b wrote it:
नमस्ते {merchant_name} जी, आपके {lapsed_count} पुराने रेगुलर कस्टमर आना बंद कर गए हैं, जिससे आपको हर महीने करीब {value_at_risk} का नुकसान हो रहा है। अगर हम उनके पास एक {offer} भेजें तो कैसा रहेगा? हम सिर्फ {treated_count} लोगों को ही मैसेज करेंगे, बाकी {holdout_count} को बाहर रखेंगे ताकि हमें पता चले कि ऑफर सच में काम कर रहा है या नहीं। क्या मैं भेज दूँ?

CALL SCRIPT FILLED, as the agent speaks it:
नमस्ते Ramesh जी, आपके 12 पुराने रेगुलर कस्टमर आना बंद कर गए हैं, जिससे आपको हर महीने करीब ₹9,460 का नुकसान हो रहा है। अगर हम उनके पास एक 35% chhoot chai pe भेजें तो कैसा रहेगा? हम सिर्फ 10 लोगों को ही मैसेज करेंगे, बाकी 2 को बाहर रखेंगे ताकि हमें पता चले कि ऑफर सच में काम कर रहा है या नहीं। क्या मैं भेज दूँ?

THREE CUSTOMER MESSAGES:
template: नमस्ते {customer_name}, हमें आपकी बहुत याद आ रही है। {shop_name} पर {offer} के साथ आपका स्वागत है।
  C0111  नमस्ते C0111, हमें आपकी बहुत याद आ रही है। Ramesh Tea Stall पर 35% chhoot chai pe के साथ आपका स्वागत है।
  C0142  नमस्ते C0142, हमें आपकी बहुत याद आ रही है। Ramesh Tea Stall पर 35% chhoot chai pe के साथ आपका स्वागत है।
  C0164  नमस्ते C0164, हमें आपकी बहुत याद आ रही है। Ramesh Tea Stall पर 35% chhoot chai pe के साथ आपका स्वागत है।

RANKING
baseline return rate  0.0500  MEASURED  12 unprompted lapse episodes in the ledger, 0 came back on their own, shrunk toward a 0.08 prior worth 20 observations
offer uplift          PRIOR     {"LOW": 0.08, "MEDIUM": 0.14, "HIGH": 0.2}

candidate                                level  baseline   uplift    inc rev   discount    profit
lapsed_winback_lapsed_regulars_high_3    HIGH     0.0500   0.2000     964.89     422.14    107.02  ACCEPTED
offpeak_fill_offpeak_slot_medium_1       MEDIUM   0.1463   0.1281      64.48      27.63     -8.23  REFUSED
offpeak_fill_offpeak_slot_medium_5       MEDIUM   0.1463   0.1281      64.48      27.63     -8.23  REFUSED
attach_upsell_anchor_buyers_low_2        LOW      0.0638   0.0090     244.22     197.16   -103.00  REFUSED
attach_upsell_anchor_buyers_low_4        LOW      0.0638   0.0090     244.22     197.16   -103.00  REFUSED
    offpeak_fill_offpeak_slot_medium_1: negative expected profit: -8.23 rupees over 30 days
    offpeak_fill_offpeak_slot_medium_5: negative expected profit: -8.23 rupees over 30 days
    attach_upsell_anchor_buyers_low_2: negative expected profit: -103.00 rupees over 30 days
    attach_upsell_anchor_buyers_low_4: negative expected profit: -103.00 rupees over 30 days
```

### Test suite

```
$ python -m pytest tests/ -q
........................................                                 [100%]
40 passed in 2.32s

$ python -m scripts.check_data
all checks passed
```

**Next: Step 4, the voice layer.**

---

## Step 2: triage and simulator (Thu 17 Sep, late evening)

Status: **done, 22 tests pass, all Step 1 data checks still pass.**

### What was built

- **`core/ledger.py`**, new. Read only SQLite access shared by triage and the simulator:
  visit history per customer, weekday and hour slot counts, baskets, realised item prices.
  Opens the database in read only mode so the nightly loop physically cannot write to the
  ledger it is reasoning about.
- **`core/triage.py`**. `score(merchant_id, today)` returns the opportunity score, the
  evidence behind it and `worth_a_call`. Pure arithmetic, no LLM, three signals:
  lapsed regulars, off peak gaps, basket affinity gaps.
- **`core/simulate.py`**. Takes structured candidate dicts and returns a ranked list with
  every number that produced the rank. Guardrails in code: margin floor, monthly discount
  budget, positive expected profit required.
- **`core/fixtures.py`**, new. Four hand written placeholder candidates, marked in a box
  comment as placeholders that `core/generate.py` replaces in Step 3.
- **`core/run_night.py`**. `python -m core.run_night --merchant tea_stall_01` prints the
  whole night and appends one JSON line per decision to `logs/decisions.jsonl`.
- **`tests/`**. 22 tests across `test_triage.py` and `test_simulate.py`, with a session
  scoped `conftest.py` so the ledger is read once.
- **CLAUDE.md repo layout** gained the two new modules, `ledger.py` and `fixtures.py`.

### The result that matters

Triage rediscovers **all twelve** ground truth customer ids from transactions alone, and
values them at **9,460 rupees a month** against a ground truth of 9,385, which is 0.6
percent off the 9,400 the demo says out loud. It also finds the dead Tuesday afternoon
(85 percent below the same hours on other days) and the chai snack affinity gap (6.4
percent attach against 10.9 percent on filter coffee) without being told either exists.

The simulator ranks the winback first at 295 rupees expected profit, and refuses the 50
percent discount twice over, once on the margin floor and once on negative profit.

### Decisions made

1. **Lapsed is defined relative to each customer's own rhythm,** not by a fixed number of
   days. A regular visits at least 55 days out of 180 with a typical gap of 3 days or
   less; lapsed means silence for at least 14 days or five times that customer's own gap,
   whichever is longer. This is why the twelve separate cleanly from 130 weekly customers
   who also go quiet for stretches.
2. **The opportunity score is denominated in rupees a month at stake,** not an abstract
   0 to 100. Lapsed value counts in full because it is money the shop already earned and
   is now losing. Off peak and affinity upside are softer, so they enter at 0.20 weight.
3. **Two thresholds have to clear, not one:** 1,500 rupees a month and 3 percent of the
   shop's own revenue. A number that is large in rupees but trivial for the merchant is
   not worth a phone call, and this is what keeps call volume near the 3 percent the cost
   argument depends on.
4. **The return rate is learned from this shop, shrunk toward a prior.** The ledger yields
   12 observed lapse episodes among regulars, none of whom came back on their own. Beta
   shrinkage against a 0.15 prior with weight 20 gives 0.094. This is the prepared answer
   to "how do you handle a merchant with two weeks of data" made executable: no episodes
   means the prior, many episodes means the shop's own data.
5. **Only incremental revenue counts, but every discount is paid.** The control group
   would have converted at the baseline anyway, so its revenue is not ours to claim, while
   the discount goes to everyone who redeems including those returners. That asymmetry is
   the entire reason deep discounts fail here, and it is what the holdout in Step 5 will
   verify.
6. **Priors are in one labelled block in `simulate.py`,** named `PRIORS`, so a judge can
   see exactly which numbers are assumed and which are measured. Every one of them is
   replaced by measured lift once campaigns exist. Nothing in that block came from a
   language model and nothing in it ever will.
7. **Refusals are returned, not dropped.** A rejected candidate comes back with its full
   arithmetic and the reasons it was refused, because "here is what I refused to do and
   why" is a better answer on stage than a short list.
8. **An unknown action type is refused rather than guessed.** When the LLM invents a
   campaign type in Step 3 that the simulator cannot price, it says so instead of
   inventing a number. There is a test for this.
9. **The off peak candidate is barely profitable, at about 4 rupees,** and it is left in
   the ranking rather than tuned upward. Messaging 126 people to recover a small slot
   genuinely is marginal, and a simulator that sometimes says "this is not worth doing"
   is more convincing than one where everything wins.

### Verified, not assumed

- `python -m core.run_night --today 2026-08-20`, a week before the cohort goes quiet,
  correctly prints **NO CALL TONIGHT**: no lapsed regulars, opportunity 419 rupees against
  a 1,500 threshold. The silent night is logged to `decisions.jsonl` too.
- `test_triage_does_not_read_the_ground_truth_file` booby traps `open()` and reruns
  triage, so a read of the answer key raises however it is spelled. Grepping the source
  would have been weaker and, as it happens, would have failed on the docstring saying
  the file is not read.

### Not done, by instruction

No LLM generation, no voice layer, no holdout, no measurement, no API, no dashboard. No
network calls. `.env` untouched, nothing pushed.

### One nightly cycle, end to end

```
Ramesh Tea Stall, night of 2026-09-17
=====================================
  ₹33691 a month over the last 180 days, 2116 transactions a month, average ticket ₹15.93
  397 customers active, 40 of them regulars

Lapsed regulars
===============
  customer   last visit     silent visit days monthly value
  C0111      2026-08-24       24 d        157           ₹815
  C0142      2026-08-25       23 d        158           ₹813
  C0164      2026-08-25       23 d        158           ₹812
  C0268      2026-08-25       23 d        158           ₹812
  C0139      2026-08-24       24 d        157           ₹800
  C0241      2026-08-25       23 d        158           ₹796
  C0228      2026-08-23       25 d        156           ₹794
  C0252      2026-08-23       25 d        156           ₹778
  C0329      2026-08-26       22 d        159           ₹774
  C0073      2026-08-24       24 d        157           ₹773
  C0080      2026-08-25       23 d        158           ₹761
  C0137      2026-08-24       24 d        157           ₹731

  Value at risk: ₹9460 a month across 12 customers.
  How they were found: a regular visits at least 55 days in 180 and typically waits at most 3.0 days, lapsed means silent for at least 14 days or 5 times that customer's own gap

Other signals
=============
  Off peak  Tuesday 14:00 to 17:00, 1.27 transactions against a norm of 8.68, 85 percent down, ₹506 a month
  Affinity  chai attaches an add on 6.4 percent of the time, filter_coffee manages 10.9 percent, ₹1436 a month

Triage decision
===============
  lapsed value at risk      9459.67  x 1.00  =     9459.67
  off peak upside            505.92  x 0.20  =      101.18
  affinity upside           1436.39  x 0.20  =      287.28
  ----------------------------------------------------
  opportunity score         9848.13 rupees a month at stake, 29.2 percent of revenue
  threshold                 1500.00 and 3.0 percent of revenue

  WORTH A CALL. Both thresholds cleared.

Simulator
=========
  Return rate learned from this shop: 12 lapse episodes, 0 came back on their own, shrunk to 0.094 against a 0.15 prior.
  Guardrails: margin floor 25 percent, monthly discount budget ₹1500, positive expected profit required.

Ranked actions, expected profit over 30 days
============================================

  1. Win back the regulars who stopped coming   [winback_lapsed_chai_10]
     offer          10 percent off chai for 7 days
     reach          12 customers messaged
     baseline       9.38 percent respond with no contact
     uplift         x2.46 expected from the message and the offer
     incremental    1.65 responses beyond what would have happened anyway
     revenue        ₹779.00 incremental
     gross profit   ₹428.45 at 55 percent margin
     discount cost  ₹131.11, paid on every redemption including the customers who were coming back anyway
     message cost   ₹1.80
     EXPECTED PROFIT ₹295.54
       based on: 12 lapsed regulars found by triage from the ledger
       based on: 12 organic lapse episodes observed at this shop, 0 returned, shrunk to 0.094 against a 0.15 prior
       based on: cohort is worth 9460 rupees a month, 788 each
       based on: a returner is assumed to spend 60 percent of their old cadence

  2. Get a snack onto the chai order   [attach_snack_with_chai_10]
     offer          10 percent off snack for 28 days
     reach          315 customers messaged
     baseline       6.38 percent respond with no contact
     uplift         x1.57 expected from the message and the offer
     incremental    65.96 responses beyond what would have happened anyway
     revenue        ₹1154.30 incremental
     gross profit   ₹634.86 at 55 percent margin
     discount cost  ₹318.65, paid on every redemption including the customers who were coming back anyway
     message cost   ₹47.25
     EXPECTED PROFIT ₹268.96
       based on: chai attaches an add on 6.4 percent of the time against 10.9 percent on filter_coffee
       based on: the filter_coffee attach rate is the ceiling, this shop has already proved it
       based on: 1820 chai orders expected in the next 30 days
       based on: an add on is worth 17.50 rupees here

  3. Fill the dead Tuesday afternoon   [offpeak_tuesday_afternoon_15]
     offer          15 percent off chai for 28 days
     reach          126 customers messaged
     baseline       14.63 percent respond with no contact
     uplift         x2.03 expected from the message and the offer
     incremental    5.62 responses beyond what would have happened anyway
     revenue        ₹89.52 incremental
     gross profit   ₹49.24 at 55 percent margin
     discount cost  ₹26.43, paid on every redemption including the customers who were coming back anyway
     message cost   ₹18.90
     EXPECTED PROFIT ₹3.91
       based on: Tuesday 14:00 to 17:00 does 1.27 transactions against a norm of 8.68
       based on: a nudge is assumed to recover 15 percent of that measured gap
       based on: 126 customers already shop those hours and can be messaged
       based on: revenue per extra visit is this shop's own average ticket, 15.93

Refused
=======
  Win back the same regulars with a deep discount   [winback_lapsed_chai_50]
     refused: margin floor: a 50 percent discount leaves 10 percent margin, floor is 25
     refused: negative expected profit: -200.81 rupees over 30 days

Recommendation
==============
  Win back the regulars who stopped coming, expected profit ₹295.54 over 30 days.
  Next step, once Step 3 is in: the LLM writes this in Hindi with placeholders,
  the merchant approves it by voice, and a 15 percent holdout decides the truth.

Decision log
============
  Appended to logs\decisions.jsonl
```

### Test suite

```
$ python -m pytest tests/ -q
......................                                                   [100%]
22 passed in 1.43s
```

**Next: Step 3, LLM candidate generation.**

---

## Step 1: scaffold and synthetic ledger (Thu 17 Sep, evening)

Status: **done, all data checks pass.**

### What was built

- **CLAUDE.md corrections.** Build order rewritten around the real calendar (17 Sep 2026
  is a Thursday, not a Wednesday): Thu evening, Fri morning, Fri afternoon, Fri 8pm video
  deadline, Sat polish. Repo layout now lists `core/run_night.py`, `requirements.txt`,
  `STATUS.md`, `scripts/`, `tests/` and `logs/`. Conventions gained the placeholder rule:
  LLM copy carries `{lapsed_count}` and `{value_at_risk}`, code fills every number from
  the simulator.
- **Repo scaffold.** `data/ core/ voice/ memory/ api/ dashboard/ workflows/ scripts/
  tests/ logs/`, each Python package with an `__init__.py` and each module a one line
  docstring stating its job. Every module is a stub except the two below.
- **`.venv`** created with Python 3.12.4. `requirements.txt` installed: pandas, numpy,
  scikit-learn, fastapi, uvicorn, python-dotenv, httpx, pydantic, pytest. No cognee yet,
  that is a Friday afternoon decision on a one hour timebox.
- **`.gitignore`** (`.venv/`, `.env`, `__pycache__/`, `*.db`, `logs/`) and
  **`.env.example`** with variable names only, no values.
- **`data/generate.py`**, run with `python -m data.generate`. Builds 18 months of UPI
  transactions for `tea_stall_01`, a Bengaluru tea stall, seeded at 20260917 so every run
  is identical. Writes `data/dukaan.db` (tables `merchants`, `customers`, `transactions`)
  and `data/ground_truth.json`.
- **`scripts/check_data.py`**, run with `python -m scripts.check_data`. Validates every
  requirement in the CLAUDE.md synthetic data section and exits non-zero on any failure.

### The ledger

400 customers, 34,506 transactions over 36,849 line items, 2025-03-17 to 2026-09-16.
Customer ids are synthetic (`C0137`, `payer0137@okdukaan`). No names, no phone numbers.
Three segments: 40 daily regulars, 130 weekly, 230 occasional.

The twelve lapsed regulars are the demo's whole opening line. They visit at a tuned rate
until late August and then stop completely, with the cohort's lapse date at 2026-08-27,
twenty one days before today. Their combined monthly value lands at **9,385 rupees**,
which is the figure the triage layer has to rediscover on its own.

### Decisions made

1. **The 9,400 target is tuned, not written down.** `data/generate.py` holds a
   `TARGET_COMBINED_MONTHLY` constant and searches for the visit rate that hits it, which
   settled at 1.685 visits per day. The achieved value is recorded in
   `ground_truth.json` for evaluation. Nothing under `core/`, `voice/`, `memory/` or
   `api/` may read either. Triage must compute value at risk from the ledger.
2. **Value at risk has one definition, applied twice and independently.** Spend in the
   180 days ending at the lapse date, divided by six. The generator computes it to tune,
   `check_data.py` recomputes it from SQL. They agree, so the definition is not circular.
3. **Line items, not baskets.** One row per item with a shared `txn_id`, so basket
   affinity ("tea buyers never add a snack") is a join rather than a parse. Snack attach
   on chai orders is 6.07 percent.
4. **The dead Tuesday is a shift in when people come, not how many.** Hour weights for
   14:00 to 16:59 are cut to 0.14 on Tuesdays only, so those visits move to the morning
   and evening rather than vanishing. The slot runs at 1.05 transactions per day against
   an 8.05 baseline on other weekdays, 87 percent down, well past the 50 percent the
   requirement asks for.
5. **Customer ids are shuffled before they are handed out.** The lapsed twelve came out
   as C0001 to C0012 on the first run, which would look staged on the judge dashboard.
   They are now scattered across the range, and each has its own last visit date between
   2026-08-23 and 2026-08-26 instead of all twelve stopping on the same day.
6. **`logs/` is gitignored whole,** so the empty directory is not in the commit. Code
   that writes there creates it. Simpler than a `.gitkeep` with a negation rule.
7. **Six true items, five to eleven raw spellings each,** mixing case, trailing prices,
   misspellings and Devanagari: `chai`, `CHAI 10`, `tea spl`, `chay`, `चाय`. 3,761 lines
   carry a Devanagari name. Resolving these to one product is a real capability to show.
8. **Festival spikes are a visit rate multiplier** on the two dates, 2.4x for Diwali and
   2.2x for Holi, which lands at 2.37x and 2.67x actual volume against an average day.

### Not done, by instruction

No triage, no simulator, no voice layer, no API, no dashboard. No network calls were
made: no Sarvam, no n8n, no LLM. `.env` was never created, opened or read, and no
secret is written anywhere in the repo. Nothing was pushed to a remote.

### check_data output

```
Row counts
----------
   merchants            : 1
   customers            : 400
   transactions         : 34506
   transaction lines    : 36849
   segment occasional     : 230
   segment weekly         : 130
   segment daily_regular  : 40

Date range
----------
   first transaction    : 2025-03-17
   last transaction     : 2026-09-16
   span                 : 548 days

Ticket sizes
------------
   min ticket           : 10
   max ticket           : 60
   average ticket       : 15.89

Time of day
-----------
   06:00     924  ###
   07:00    2707  ##########
   08:00    4145  ################
   09:00    3547  ##############
   10:00    2133  ########
   11:00    1443  #####
   12:00    1441  #####
   13:00    1570  ######
   14:00    1032  ####
   15:00    1039  ####
   16:00    1777  #######
   17:00    3260  #############
   18:00    3855  ###############
   19:00    2924  ###########
   20:00    1838  #######
   21:00     871  ###

Tuesday 2pm to 5pm versus other weekdays
----------------------------------------
   Mon  slot txns   623 over  79 days  =    7.89 per day
   Tue  slot txns    83 over  79 days  =    1.05 per day
   Wed  slot txns   630 over  79 days  =    7.97 per day
   Thu  slot txns   603 over  78 days  =    7.73 per day
   Fri  slot txns   673 over  78 days  =    8.63 per day
   Sat  slot txns   738 over  78 days  =    9.46 per day
   Sun  slot txns   498 over  78 days  =    6.38 per day
   Tuesday 1.05 per day versus other weekday baseline 8.05, ratio 0.130

Snack attach rate on chai orders
--------------------------------
   chai transactions    : 29766
   with a snack         : 1807
   attach rate          : 6.07 percent

Festival days versus average day
--------------------------------
   average day          : 62.9 transactions
   2025-10-20 Diwali  : 149 transactions, 2.37x average
   2026-03-04 Holi    : 168 transactions, 2.67x average

Raw item names per true_item
----------------------------
   chai            11 variants over  29766 lines
      CHAI, CHAI 10, Chai Spl, chai, chai 10, chai spl 12, chay, cutting chai, tea, tea spl, चाय
   filter_coffee    7 variants over   4740 lines
      COFFEE, Filter Coffee, cofee, coffee, filter kaapi, kaapi, कॉफी
   biscuit          6 variants over    605 lines
      BISCUIT, biscuit, biscut 10, biskut, parle g, बिस्कुट
   vada_pav         6 variants over    585 lines
      VADAPAV, Vada Pav 20, vada pav, vada-pav, wada pav, वडा पाव
   samosa           6 variants over    581 lines
      SAMOSA 15, Samosa, samosa, samosa 2pc, smosa, समोसा
   bun_maska        5 variants over    572 lines
      BUN MASKA, bun maska, bun-maska, bunmaska, बन मस्का
   lines with a Devanagari raw name: 3804

Ground truth lapsed cohort (evaluation only)
--------------------------------------------
   lapse date           : 2026-08-27 (21 days before today)
   customer   last visit   monthly spend  visits since lapse
   C0073      2026-08-24          766.00  0
   C0080      2026-08-25          757.50  0
   C0111      2026-08-24          809.00  0
   C0137      2026-08-24          723.67  0
   C0139      2026-08-24          793.67  0
   C0142      2026-08-25          807.00  0
   C0164      2026-08-25          808.33  0
   C0228      2026-08-23          779.00  0
   C0241      2026-08-25          790.50  0
   C0252      2026-08-23          767.67  0
   C0268      2026-08-25          808.83  0
   C0329      2026-08-26          774.17  0
   combined monthly value: 9385.33
   daily regulars still active in the last 7 days: 28

Result
------
   all checks passed
```

**Next: Step 2, triage plus simulator.**

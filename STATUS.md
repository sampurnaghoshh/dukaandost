# STATUS

Build log for Dukaan Dost. Newest step at the top.

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

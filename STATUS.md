# STATUS

Build log for Dukaan Dost. Newest step at the top.

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

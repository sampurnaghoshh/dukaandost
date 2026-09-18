# n8n workflows

The nightly loop lives here. Delete these and the product stops working: the backend is a
set of steps, and this is the thing that decides what runs, in what order, what happens when
the merchant says no, and what happens three days later.

Built for **n8n Cloud 2.40.2**.

## The files

| file | what it is |
|---|---|
| `nightly_parent.json` | Fires at 9pm, fans out over merchants, calls the sub workflow once each |
| `run_one_merchant.json` | The real loop for one shop, including the 72 hour wait |
| `run_one_merchant_demo.json` | Same canvas, manual trigger, 5 second wait so it finishes on stage |
| `call_webhook.json` | Receives Sarvam's post call callback and records the outcome |
| `error_handler.json` | Set as the error workflow on all of the above, logs failures instead of dying quietly |

## Import order

Import the error handler first and the parent last, because each one needs the ids of the
ones below it.

1. **`error_handler.json`**. Copy its workflow id.
2. **`run_one_merchant.json`**. In Settings, set Error Workflow to the error handler.
   Copy its workflow id.
3. **`run_one_merchant_demo.json`**. Same, set the error workflow.
4. **`call_webhook.json`**. Same. Activate it so the webhook URL goes live.
5. **`nightly_parent.json`**. Set the error workflow, then open
   *Run the night for this merchant* and paste the `run_one_merchant` id in place of
   `REPLACE_WITH_RUN_ONE_MERCHANT_WORKFLOW_ID`.

Every workflow ships with `REPLACE_WITH_ERROR_HANDLER_WORKFLOW_ID` in `settings.errorWorkflow`
as a reminder. n8n will not resolve that string on its own.

## The two things to change on demo morning

### 1. The backend URL, in one Set node

The FastAPI service runs on a laptop, so n8n Cloud reaches it through a cloudflared tunnel:

```bash
uvicorn api.main:app --reload
cloudflared tunnel --url http://localhost:8000
```

That prints a `https://something-random.trycloudflare.com` URL. It changes every time the
tunnel restarts.

**Paste it in exactly one place: the `base_url` field of the node named
"Where the backend lives" in `nightly_parent.json`.** The parent passes it down to the sub
workflow, and every HTTP node reads it from there. No other node in `nightly_parent.json` or
`run_one_merchant.json` contains a host, and there is a test that keeps it that way.

In demo mode the equivalent field is on the node named "Start the night for one merchant" in
`run_one_merchant_demo.json`, which is deliberately given the same node name so every
expression downstream is identical.

Both default to `http://localhost:8000`, which is right if you are running n8n locally and
wrong on Cloud.

### 2. The webhook URL that Sarvam calls

`call_webhook.json` has a Webhook node named "Sarvam says the call ended" with the path
`dukaan-dost/call-ended`. **n8n issues the full URL, we do not choose it.** Activate the
workflow, copy the Production URL from the node, and paste it into the Sarvam voice agent as
the post call webhook.

`call_webhook.json` and `error_handler.json` have no parent to inherit `base_url` from, so
each has its host written once in its final HTTP node. If you are not on localhost, change
those two as well. They are the only other places a host appears anywhere in this directory.

## How the call actually pauses the workflow

This is the part worth pointing at. The run does not poll and it does not sleep.

1. "Phone the shopkeeper in Hindi" posts to `/call` and hands over `$execution.resumeUrl`,
   the resume URL of the Wait node sitting right after it.
2. "Wait for the merchant to answer" parks the whole execution. Nothing is consuming
   anything while the phone rings.
3. When the merchant answers, the backend posts the Decision to that resume URL and the
   execution wakes up exactly where it stopped.
4. If nobody answers within three minutes the Wait node resumes anyway, and the three way
   Switch falls through to its no answer branch.

The same mechanism works whether the answer came from real telephony or from the two big
buttons on the local soundbox page, because both settle through the same code path.

## What the canvas is doing

- **A three way Switch, not an IF.** Approved, declined and no answer each get their own
  branch and their own logging node. A merchant who said no and a merchant who never picked
  up are different facts, and the second one gets another call tomorrow.
- **The silent path is drawn.** Most nights triage says no, and the false branch of
  "Is it worth a phone call" goes to a node literally named "No call tonight" followed by a
  call that writes that decision down. Staying quiet is a decision the dashboard shows.
- **The holdout is a Code node on the canvas.** It hashes each customer id the same way
  `core/holdout.py` does, SHA-256 of `campaign id | customer id | seed`, sorts, and holds
  back the lowest fifteen percent. Because it hashes rather than shuffles, the retry on
  "Send the Hindi messages" cannot double treat anybody. The backend re-derives the split
  and reports `matches_core_holdout` so a drift between canvas and code is visible.
- **A 72 hour Wait node.** A three day business process with no scheduler of our own.
- **Retry on fail** on dispatch and measurement, three tries each.
- **An error workflow** that writes the failure into the same decision log the dashboard
  reads, with `onError: continueRegularOutput` so that if the backend is what fell over, the
  error handler does not fail too and hide the original problem.

## Endpoints these workflows call

All of them exist in `api/main.py`, and `tests/test_workflows.py` fails if any URL here stops
resolving to a real route or starts sending a field the pydantic model would reject.

| endpoint | called by |
|---|---|
| `POST /triage` | Is anything wrong at this shop |
| `POST /generate` | Ask the model what to try |
| `POST /simulate` | Price every offer depth |
| `POST /call` | Phone the shopkeeper in Hindi |
| `POST /campaign/dispatch` | Send the Hindi messages |
| `POST /measure` | Measure what actually happened |
| `POST /learning/update` | Write the lesson back to memory |
| `POST /decisions` | The silent night, the decline, the no answer, and the error handler |
| `POST /call/outcome` | Record the outcome in the backend, in `call_webhook.json` |

## Running it on stage

Import and activate the demo workflow, then press Execute Workflow. It runs triage,
generation, pricing, the call, the holdout split, dispatch, a five second wait, measurement
and the writeback, in under a minute, with the judge dashboard open beside it.

The only thing demo mode fakes is the length of the wait, and it fakes it in one node.

## The line to say out loud

> The entire nightly loop is orchestrated in n8n, including a seventy two hour Wait node for
> the measurement and a webhook that receives the call outcome from Sarvam.

## What has not been verified

These files have not been imported into a running n8n Cloud 2.40.2 instance. They are
written to the node schema and type versions that release uses, and `tests/test_workflows.py`
checks everything that can be checked without n8n: valid JSON, unique node names and ids,
every connection pointing at a node that exists, every node reachable, every URL resolving to
a real FastAPI route with a matching method and request shape, and the Code node's constants
matching `core/holdout.py`.

What it cannot check is whether n8n 2.40.2 expects a different `typeVersion` for a given
node. If a node imports with a warning, open it, re-pick the option, and save. Budget ten
minutes for that on Friday rather than discovering it on Saturday morning.

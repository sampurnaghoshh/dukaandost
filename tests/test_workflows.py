"""The n8n workflow JSON: structurally valid, and every URL it calls is a route we serve.

These cannot prove the files import cleanly into n8n Cloud 2.40.2, because nothing here runs
n8n. What they can prove is the part that actually breaks on demo morning: a workflow calling
an endpoint that does not exist, a connection pointing at a node that was renamed, or the
backend URL hardcoded in a second place where nobody thinks to change it.
"""

from __future__ import annotations

import json
import os

import pytest

from api.main import app
from core import holdout, ledger

WORKFLOW_DIR = os.path.join(ledger.REPO_ROOT, "workflows")
BASE_URL_EXPRESSION = "base_url"
PLACEHOLDER_HOST = "http://localhost:8000"

# The workflows that inherit base_url rather than declaring their own.
INHERITS_BASE_URL = {"run_one_merchant.json", "run_one_merchant_demo.json"}


def workflow_files() -> list:
    return sorted(name for name in os.listdir(WORKFLOW_DIR) if name.endswith(".json"))


def load(name: str) -> dict:
    with open(os.path.join(WORKFLOW_DIR, name), encoding="utf-8") as handle:
        return json.load(handle)


def served_paths() -> set:
    paths = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            paths.add(path)
    return paths


def http_nodes(workflow: dict) -> list:
    return [node for node in workflow["nodes"]
            if node["type"] == "n8n-nodes-base.httpRequest"]


def path_of(url: str) -> str:
    """The route path out of an n8n URL expression, whatever precedes it."""
    trimmed = url.lstrip("=").strip()
    if "}}" in trimmed:
        trimmed = trimmed.split("}}")[-1]
    for host in ("http://localhost:8000", "https://", "http://"):
        if trimmed.startswith(host):
            trimmed = trimmed[len(host):]
            if not trimmed.startswith("/"):
                trimmed = "/" + trimmed.split("/", 1)[-1]
            break
    return trimmed.split("?")[0].rstrip("/") or "/"


@pytest.fixture(scope="module")
def workflows() -> dict:
    return {name: load(name) for name in workflow_files()}


# ---------------------------------------------------------------- the files exist


def test_all_five_workflows_are_present(workflows):
    assert set(workflows) == {
        "call_webhook.json",
        "error_handler.json",
        "nightly_parent.json",
        "run_one_merchant.json",
        "run_one_merchant_demo.json",
    }


# ---------------------------------------------------------------- structure


def test_every_workflow_parses_and_has_the_shape_n8n_expects(workflows):
    for name, workflow in workflows.items():
        assert isinstance(workflow.get("name"), str) and workflow["name"], name
        assert isinstance(workflow.get("nodes"), list) and workflow["nodes"], name
        assert isinstance(workflow.get("connections"), dict), name
        for node in workflow["nodes"]:
            for key in ("id", "name", "type", "typeVersion", "position", "parameters"):
                assert key in node, "%s: node %r has no %s" % (name, node.get("name"), key)
            assert node["type"].startswith("n8n-nodes-base."), name
            assert len(node["position"]) == 2, name


def test_node_names_are_unique_within_each_workflow(workflows):
    for name, workflow in workflows.items():
        names = [node["name"] for node in workflow["nodes"]]
        assert len(names) == len(set(names)), "%s has duplicate node names" % name


def test_node_ids_are_unique_within_each_workflow(workflows):
    for name, workflow in workflows.items():
        ids = [node["id"] for node in workflow["nodes"]]
        assert len(ids) == len(set(ids)), "%s has duplicate node ids" % name


def test_every_connection_points_at_a_node_that_exists(workflows):
    for name, workflow in workflows.items():
        known = {node["name"] for node in workflow["nodes"]}
        for source, outputs in workflow["connections"].items():
            assert source in known, "%s: connection from unknown node %r" % (name, source)
            for branch in outputs.get("main", []):
                for link in branch or []:
                    assert link["node"] in known, (
                        "%s: %r points at unknown node %r" % (name, source, link["node"]))


def test_every_node_except_triggers_is_reachable(workflows):
    """A node nobody connects to is a node that silently never runs."""
    triggers = ("Trigger", "trigger", "n8n-nodes-base.webhook")
    for name, workflow in workflows.items():
        targets = {link["node"]
                   for outputs in workflow["connections"].values()
                   for branch in outputs.get("main", [])
                   for link in (branch or [])}
        for node in workflow["nodes"]:
            if any(word in node["type"] for word in triggers):
                continue
            assert node["name"] in targets, (
                "%s: %r is on the canvas but nothing connects to it" % (name, node["name"]))


def test_node_names_read_as_plain_language(workflows):
    """Judges read the canvas before they read anything else."""
    for name, workflow in workflows.items():
        for node in workflow["nodes"]:
            label = node["name"]
            assert label[0].isupper() or label[0].isdigit(), "%s: %r" % (name, label)
            assert "_" not in label, "%s: %r looks like a variable" % (name, label)
            assert len(label.split()) >= 2, "%s: %r is not a sentence" % (name, label)


# ---------------------------------------------------------------- the URLs


def test_every_url_in_every_workflow_resolves_to_a_real_route(workflows):
    known = served_paths()
    checked = 0
    for name, workflow in workflows.items():
        for node in http_nodes(workflow):
            path = path_of(node["parameters"]["url"])
            assert path in known, (
                "%s: node %r calls %s which is not a route this API serves. Known: %s"
                % (name, node["name"], path, sorted(known)))
            checked += 1
    assert checked >= 8, "expected the workflows to call the backend more than this"


def test_every_http_node_posts_where_the_api_expects_a_post(workflows):
    posts = {getattr(route, "path"): getattr(route, "methods")
             for route in app.routes if getattr(route, "methods", None)}
    for name, workflow in workflows.items():
        for node in http_nodes(workflow):
            path = path_of(node["parameters"]["url"])
            method = node["parameters"].get("method", "GET").upper()
            assert method in posts[path], (
                "%s: %r sends %s to %s which only accepts %s"
                % (name, node["name"], method, path, sorted(posts[path])))


def test_the_backend_url_is_written_in_exactly_one_place_per_workflow(workflows):
    """The whole point of the Set node is that demo morning is a one line change."""
    for name, workflow in workflows.items():
        hardcoded = []
        for node in workflow["nodes"]:
            blob = json.dumps(node.get("parameters", {}))
            if PLACEHOLDER_HOST in blob:
                hardcoded.append(node["name"])
        if name in INHERITS_BASE_URL:
            # These read base_url from the node the parent fills, or from their own Set node
            # in demo mode. Either way there is at most one.
            assert len(hardcoded) <= 1, "%s hardcodes the host in %s" % (name, hardcoded)
        else:
            assert len(hardcoded) <= 1, "%s hardcodes the host in %s" % (name, hardcoded)


def test_the_sub_workflow_never_hardcodes_a_host_in_an_http_node(workflows):
    for name in INHERITS_BASE_URL:
        for node in http_nodes(workflows[name]):
            url = node["parameters"]["url"]
            assert BASE_URL_EXPRESSION in url, (
                "%s: %r does not read base_url" % (name, node["name"]))
            assert "http://" not in url and "https://" not in url, (
                "%s: %r hardcodes a host" % (name, node["name"]))


# ---------------------------------------------------------------- the shapes we send


def test_the_bodies_we_post_only_use_fields_the_api_accepts(workflows):
    """Catches a workflow sending a field the pydantic model would reject."""
    from api import main

    models = {
        "/triage": main.MerchantRequest,
        "/generate": main.GenerateRequest,
        "/simulate": main.SimulateRequest,
        "/call": main.CallRequest,
        "/campaign/launch": main.LaunchRequest,
        "/campaign/dispatch": main.DispatchRequest,
        "/measure": main.MeasureRequest,
        "/learning/update": main.LearningUpdateRequest,
        "/decisions": main.LogDecisionRequest,
        "/call/outcome": main.CallOutcomeRequest,
    }
    for name, workflow in workflows.items():
        for node in http_nodes(workflow):
            path = path_of(node["parameters"]["url"])
            model = models.get(path)
            body = node["parameters"].get("jsonBody", "")
            if model is None or "JSON.stringify({" not in body:
                continue
            allowed = set(model.model_fields)
            inner = body.split("JSON.stringify({", 1)[1]
            sent = set()
            depth = 0
            key = ""
            for char in inner:
                if char in "{[(":
                    depth += 1
                elif char in "}])":
                    if depth == 0:
                        break
                    depth -= 1
                elif char == ":" and depth == 0:
                    sent.add(key.strip().strip("'\"").split()[-1])
                    key = ""
                elif char == "," and depth == 0:
                    key = ""
                else:
                    key += char
            unknown = sent - allowed
            assert not unknown, (
                "%s: %r sends %s to %s, which accepts only %s"
                % (name, node["name"], sorted(unknown), path, sorted(allowed)))


# ---------------------------------------------------------------- the n8n features that score


def test_the_switch_branches_three_ways_not_two(workflows):
    workflow = workflows["run_one_merchant.json"]
    switch = [node for node in workflow["nodes"]
              if node["type"] == "n8n-nodes-base.switch"][0]
    rules = switch["parameters"]["rules"]["values"]
    assert len(rules) == 2
    assert switch["parameters"]["options"]["fallbackOutput"] == "extra"
    outputs = workflow["connections"][switch["name"]]["main"]
    assert len(outputs) == 3, "approved, declined and no answer need their own branches"
    assert len({branch[0]["node"] for branch in outputs}) == 3, (
        "declined and no answer must not share a logging node")


def test_the_real_workflow_waits_seventy_two_hours(workflows):
    waits = [node for node in workflows["run_one_merchant.json"]["nodes"]
             if node["type"] == "n8n-nodes-base.wait"]
    measurement = [node for node in waits if node["parameters"].get("unit") == "hours"]
    assert measurement, "the 72 hour wait is the whole long running process argument"
    assert measurement[0]["parameters"]["amount"] == 72


def test_the_demo_workflow_waits_seconds_instead(workflows):
    waits = [node for node in workflows["run_one_merchant_demo.json"]["nodes"]
             if node["type"] == "n8n-nodes-base.wait"]
    measurement = [node for node in waits if node["parameters"].get("unit") == "seconds"]
    assert measurement, "demo mode has to finish inside the pitch"
    assert measurement[0]["parameters"]["amount"] <= 10
    assert not [node for node in waits if node["parameters"].get("unit") == "hours"]


def test_the_demo_workflow_is_otherwise_the_same_shape(workflows):
    """Only the trigger and the wait may differ, or demo mode proves nothing."""
    real = workflows["run_one_merchant.json"]
    demo = workflows["run_one_merchant_demo.json"]
    real_http = {node["name"] for node in http_nodes(real)}
    demo_http = {node["name"] for node in http_nodes(demo)}
    assert real_http == demo_http
    assert len(demo["nodes"]) == len(real["nodes"]) + 1, "demo adds only its Set node"


def test_dispatch_retries_on_failure(workflows):
    node = [n for n in workflows["run_one_merchant.json"]["nodes"]
            if n["name"] == "Send the Hindi messages"][0]
    assert node["retryOnFail"] is True
    assert node["maxTries"] == 3


def test_every_workflow_names_an_error_workflow_except_the_handler(workflows):
    for name, workflow in workflows.items():
        if name == "error_handler.json":
            assert "errorWorkflow" not in workflow.get("settings", {})
            continue
        assert workflow["settings"].get("errorWorkflow"), (
            "%s does not point at the error handler" % name)


def test_the_silent_path_is_on_the_canvas(workflows):
    workflow = workflows["run_one_merchant.json"]
    names = {node["name"] for node in workflow["nodes"]}
    assert "No call tonight" in names
    assert "Write down that we stayed quiet" in names
    branches = workflow["connections"]["Is it worth a phone call"]["main"]
    assert len(branches) == 2, "the IF node needs both a true and a false branch drawn"
    assert branches[1][0]["node"] == "No call tonight"


def test_the_parent_passes_the_base_url_down(workflows):
    parent = workflows["nightly_parent.json"]
    executor = [node for node in parent["nodes"]
                if node["type"] == "n8n-nodes-base.executeWorkflow"][0]
    mapped = executor["parameters"]["workflowInputs"]["value"]
    assert "merchant_id" in mapped and "base_url" in mapped
    assert "Where the backend lives" in mapped["base_url"]


def test_the_holdout_code_node_matches_the_python_constants(workflows):
    """If the canvas and core/holdout.py disagree on the split, the experiment is broken."""
    code = [node for node in workflows["run_one_merchant.json"]["nodes"]
            if node["name"] == "Hold back fifteen percent at random"][0]
    source = code["parameters"]["jsCode"]
    assert "const HOLDOUT_SHARE = %s;" % holdout.HOLDOUT_SHARE in source
    assert "const seed = %d;" % holdout.DEFAULT_SEED in source
    assert "SHA-256" in source, "the split has to hash the same way Python does"
    assert "Math.round" in source, "Python rounds the holdout count, so JavaScript must too"


def test_the_webhook_reads_the_discount_offer_response(workflows):
    code = [node for node in workflows["call_webhook.json"]["nodes"]
            if node["type"] == "n8n-nodes-base.code"][0]
    assert "discount_offer_response" in code["parameters"]["jsCode"]


def test_the_error_handler_cannot_fail_and_hide_the_original_error(workflows):
    node = [n for n in workflows["error_handler.json"]["nodes"]
            if n["name"] == "Write the failure to the decision log"][0]
    assert node["onError"] == "continueRegularOutput"

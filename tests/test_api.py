"""HTTP API."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

SORT_A = "def solve(data):\n    return sorted(data)\n"
SORT_B = (
    "def solve(data):\n"
    "    return sorted(set(data)) if False else sorted(data)\n"
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def run(client, **overrides):
    payload = {
        "algorithm_a": {"name": "A", "code": SORT_A},
        "algorithm_b": {"name": "B", "code": "def solve(data):\n    v=list(data)\n    v.sort()\n    return v\n"},
        "language": "python",
        "protocol": "function",
        "entry_point": "solve",
        "tests": {
            "schema": "int_list",
            "count": 8,
            "size": 12,
            "seed": 5,
            "strategies": ["random", "edge", "duplicates"],
        },
        "comparison_mode": "strict",
        "benchmark": {"enabled": False},
    }
    payload.update(overrides)
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    deadline = time.time() + 120
    while time.time() < deadline:
        job = client.get(f"/api/runs/{run_id}").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.1)
    raise AssertionError("the job never finished")


class TestPages:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "Algorithm Arena" in response.text

    def test_static_assets(self, client):
        for asset in ("styles.css", "app.js", "editor.js", "charts.js"):
            assert client.get(f"/static/{asset}").status_code == 200

    def test_health(self, client):
        payload = client.get("/api/health").json()
        assert payload["status"] == "ok"
        assert payload["sandbox"]["level"] in ("namespace_nobody", "namespace", "none")
        assert payload["default_limits"]["memory_mb"] > 0


class TestReference:
    def test_schemas_and_strategies(self, client):
        payload = client.get("/api/reference").json()
        assert len(payload["schemas"]) >= 8
        assert {s["id"] for s in payload["strategies"]} >= {
            "random", "edge", "boundary", "duplicates", "empty", "large", "user"
        }
        assert len(payload["examples"]) == 6
        assert payload["languages"] == [{"id": "python", "label": "Python", "enabled": True}]

    def test_every_example_is_complete(self, client):
        for example in client.get("/api/reference").json()["examples"]:
            assert {"id", "title", "blurb", "category", "tests", "benchmark"} <= set(example)

    def test_example_detail_includes_code(self, client):
        listing = client.get("/api/reference").json()["examples"]
        for entry in listing:
            detail = client.get(f"/api/examples/{entry['id']}").json()
            assert "def solve" in detail["code_a"]
            assert "def solve" in detail["code_b"]
            assert detail["tests"]["schema"] in {s["id"] for s in client.get("/api/reference").json()["schemas"]}

    def test_unknown_example(self, client):
        assert client.get("/api/examples/nope").status_code == 404


class TestValidation:
    def test_empty_algorithm_rejected(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": ""}, "algorithm_b": {"code": "def solve(x):\n    return x\n"}})
        assert response.status_code == 400
        assert "empty" in response.json()["detail"]

    def test_identical_code_rejected(self, client):
        code = "def solve(x):\n    return x\n"
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": code}, "algorithm_b": {"code": code}})
        assert response.status_code == 400
        assert "identical" in response.json()["detail"]

    def test_unsupported_language_rejected(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": "def solve(x):\n    return x\n"},
            "algorithm_b": {"code": "def solve(y):\n    return y\n"},
            "language": "rust"})
        assert response.status_code == 422
        assert "Python" in str(response.json()["detail"])

    def test_wrong_entry_point_warns(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": "x = 1\n"},
            "algorithm_b": {"code": "def nope(y):\n    return y\n"},
            "entry_point": "missing"})
        assert response.status_code == 400
        assert "missing" in response.json()["detail"]

    def test_bad_entry_point_name_rejected(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": "def solve(x):\n    return x\n"},
            "algorithm_b": {"code": "def solve(y):\n    return y\n"},
            "entry_point": "not an identifier"})
        assert response.status_code == 422

    def test_unknown_strategy_rejected(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": "def solve(x):\n    return x\n"},
            "algorithm_b": {"code": "def solve(y):\n    return y\n"},
            "tests": {"strategies": ["telepathy"]}})
        assert response.status_code == 422
        assert "telepathy" in str(response.json()["detail"])

    def test_out_of_range_limit_rejected(self, client):
        response = client.post("/api/runs", json={
            "algorithm_a": {"code": "def solve(x):\n    return x\n"},
            "algorithm_b": {"code": "def solve(y):\n    return y\n"},
            "limits": {"memory_mb": 999999}})
        assert response.status_code == 422

    def test_unknown_run_id(self, client):
        assert client.get("/api/runs/nope").status_code == 404


class TestRunLifecycle:
    def test_equivalent_algorithms(self, client):
        job = run(client)
        report = job["result"]
        assert report["verdict"]["status"] == "empirically_equivalent"
        assert report["correctness"]["failed"] == 0
        assert report["correctness"]["passed"] == report["correctness"]["total"]
        assert "empirical evidence" in report["verdict"]["statement"]
        assert "does not mathematically prove" in report["verdict"]["statement"]

    def test_different_algorithms(self, client):
        job = run(client, algorithm_b={"name": "B", "code": "def solve(data):\n    return sorted(set(data))\n"})
        report = job["result"]
        assert report["verdict"]["status"] == "different"
        assert report["correctness"]["failed"] > 0
        assert report["correctness"]["mismatches"]
        assert report["performance"] is None, "no benchmark when outputs disagree"

    def test_progress_is_reported(self, client):
        payload = {
            "algorithm_a": {"code": SORT_A},
            "algorithm_b": {"code": "def solve(data):\n    out = list(data)\n    out.sort()\n    return out\n"},
            "tests": {"schema": "int_list", "count": 5, "strategies": ["random"]},
            "benchmark": {"enabled": False},
        }
        run_id = client.post("/api/runs", json=payload).json()["run_id"]
        stages = set()
        deadline = time.time() + 60
        while time.time() < deadline:
            job = client.get(f"/api/runs/{run_id}").json()
            stages.add(job["stage"])
            if job["status"] == "done":
                break
            time.sleep(0.05)
        assert "running" in stages or "generating" in stages
        assert 0.0 <= job["progress"] <= 1.0

    def test_benchmark_runs_when_equivalent(self, client):
        job = run(client, benchmark={"enabled": True, "iterations": 3, "warmup_iterations": 1,
                                     "max_tests": 10})
        performance = job["result"]["performance"]
        assert performance is not None
        assert performance["iterations"] == 3
        assert performance["timing"]["a"]["count"] == 3
        assert performance["timing"]["b"]["count"] == 3
        assert len(performance["methodology"]) >= 5
        assert performance["memory"]["basis"] in ("added", "peak")

    def test_user_test_cases(self, client):
        job = run(client, tests={"schema": "int_list", "strategies": ["user"],
                                 "user_cases": "[3, 1, 2]\n[]\n[1, 1, 1]"})
        report = job["result"]
        assert report["correctness"]["total"] == 3
        assert report["test_generation"]["per_strategy"]["user"] == 3

    def test_malformed_user_cases_report_the_line(self, client):
        job = run(client, tests={"schema": "int_list", "strategies": ["user"],
                                 "user_cases": "[1, 2]\nnot json\n[3]"})
        assert job["status"] == "error"
        assert "line 2" in job["error"]

    def test_stdio_protocol(self, client):
        a = "import sys, json\nprint(json.dumps(sorted(json.load(sys.stdin)['input'])))\n"
        b = "import sys, json\nv = json.load(sys.stdin)['input']\nprint(json.dumps(sorted(v)))\n"
        job = run(client, protocol="stdio", algorithm_a={"code": a}, algorithm_b={"code": b})
        assert job["result"]["verdict"]["status"] == "empirically_equivalent"
        assert job["result"]["protocol"] == "stdio"

    def test_reference_scores_accuracy(self, client):
        reference = "import math\ndef solve(data):\n    return float(round(math.sqrt(float(data)), 6))\n"
        job = run(client,
                  algorithm_a={"code": "def solve(data):\n    v=float(data)\n    g=v/2 or 1.0\n"
                                        "    for _ in range(50):\n        g=0.5*(g+v/g)\n    return g\n"},
                  algorithm_b={"code": reference},
                  reference_code=reference,
                  tests={"schema": "float", "count": 6, "min_value": 1, "max_value": 100,
                         "strategies": ["random"]},
                  comparison_mode="float", rel_tolerance=1e-6, abs_tolerance=1e-9)
        accuracy = job["result"]["accuracy"]
        assert accuracy is not None
        assert accuracy["a"]["compared"] > 0
        assert accuracy["a"]["max_abs_error"] is not None
        assert accuracy["a"]["rmse"] is not None

    def test_report_contains_the_sandbox_facts(self, client):
        report = run(client)["result"]
        sandbox = report["sandbox"]
        assert sandbox["capabilities"]["level"]
        assert sandbox["limits"]["memory_mb"] > 0
        assert "network_isolated" in sandbox["capabilities"]
        assert "filesystem_restricted" in sandbox["capabilities"]

    def test_seed_is_reported_for_reproducibility(self, client):
        report = run(client)["result"]
        assert report["test_generation"]["seed"] == 5
        assert report["test_generation"]["per_strategy"]["random"] == 8

    def test_deletion_clears_the_result(self, client):
        job = run(client)
        run_id = job["run_id"]
        assert client.delete(f"/api/runs/{run_id}").json()["deleted"] is True
        assert client.get(f"/api/runs/{run_id}").json().get("result") is None

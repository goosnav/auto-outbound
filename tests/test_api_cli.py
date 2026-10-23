import json
import time

import pytest
from auto_outbound.api import create_app
from auto_outbound.cli import app as cli
from auto_outbound.service import Service
from fastapi.testclient import TestClient
from typer.testing import CliRunner


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Service(tmp_path), testing=True)) as client:
        yield client


def headers(client):
    return {"X-Session-Token": client.get("/api/session").json()["token"]}


def test_actual_ui_assets_and_health_are_served(client):
    assert client.get("/").status_code == 200
    assert "text/css" in client.get("/static/app.css").headers["content-type"]
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health/ready").json()["ready"]
    assert client.get("/api/openapi.json").json()["info"]["title"] == "Auto Outbound"


def test_mutations_need_session_and_same_origin(client):
    assert client.post("/api/demo").status_code == 403
    assert client.post("/api/demo", headers=headers(client) | {"Origin": "https://attacker.example"}).status_code == 403
    assert client.get("/api/session", headers={"Host": "attacker.example"}).status_code == 403
    assert client.get("/api/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/demo", headers=headers(client)).status_code == 200


def test_api_demo_job_review_export_roundtrip(client):
    token = headers(client)
    campaign = client.post("/api/demo", headers=token).json()["campaign"]
    job = client.post(f"/api/campaigns/{campaign['id']}/jobs", json={"stage": "pipeline"}, headers=token).json()["job"]
    for _ in range(100):
        result = client.get(f"/api/jobs/{job['id']}").json()
        if result["job"]["status"] not in {"running", "queued"}:
            break
        time.sleep(0.05)
    assert result["job"]["status"] == "completed"
    detail = client.get(f"/api/campaigns/{campaign['id']}").json()
    assert detail["counts"]["drafted"] == 4
    lead = detail["leads"][0]
    approved = client.patch(f"/api/leads/{lead['id']}", json={"status": "approved"}, headers=token)
    assert approved.status_code == 200
    assert not approved.json()["ready_to_send"]
    assert client.get(f"/api/campaigns/{campaign['id']}/export?format=csv").status_code == 200
    assert client.get(f"/api/campaigns/{campaign['id']}/export?scope=approved").status_code == 400


def test_document_and_contact_import_via_real_multipart(client):
    token = headers(client)
    campaign = client.post("/api/campaigns", json={"name": "Import"}, headers=token).json()
    root = f"/api/campaigns/{campaign['id']}"
    response = client.post(root + "/documents", files={"file": ("offer.md", b"We make reviewable intake tools.", "text/plain")}, headers=token)
    assert response.status_code == 200
    doc = response.json()
    response = client.post(root + "/import", files={"file": ("leads.csv", b"company,website\nFixture,https://fixture.example\n", "text/csv")}, headers=token)
    assert response.json()["imported"] == 1
    assert client.delete(root + "/documents/" + doc["id"], headers=token).status_code == 200


def test_validation_errors_do_not_echo_offer_inputs(client):
    response = client.post("/api/campaigns", json={"name": "", "offer": "private offer text"}, headers=headers(client))
    assert response.status_code == 422
    assert "private offer text" not in response.text
    assert response.json()["error"]["code"] == "INPUT-INVALID"


def test_cli_json_is_clean_and_shares_persistence(tmp_path):
    runner = CliRunner()
    result = runner.invoke(cli, ["--data-dir", str(tmp_path), "demo", "--run", "--json"])
    assert result.exit_code == 0, result.output
    job = json.loads(result.stdout)["job"]
    assert job["status"] == "completed"
    assert Service(tmp_path).campaign_detail(job["campaign_id"])["counts"]["drafted"] == 4
    path = tmp_path / "research.csv"
    exported = runner.invoke(cli, ["--data-dir", str(tmp_path), "export", job["campaign_id"], "--output", str(path), "--json"])
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["bytes"] > 1000
    assert path.exists()


def test_cli_errors_have_stable_exit_and_json(tmp_path):
    result = CliRunner().invoke(cli, ["--data-dir", str(tmp_path), "campaign", "show", "missing", "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "NOT-FOUND"

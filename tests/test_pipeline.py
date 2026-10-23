from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from auto_outbound import demo, pipeline
from auto_outbound.config import AppError
from auto_outbound.models import LeadSeed, Model
from auto_outbound.pipeline import Context, Jobs, Stopped
from auto_outbound.providers import AgentResult, ProviderError
from auto_outbound.service import Service


class Payload(Model):
    ok: bool


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "inspect_providers",
        lambda: [
            {"id": name, "available": True, "message": ""}
            for name in ("claude", "codex")
        ],
    )
    count = [0]

    def create(*, settings=None, leads=2, offer="Review quote requests."):
        count[0] += 1
        service = Service(tmp_path / str(count[0]))
        campaign = service.create_campaign(
            {
                "name": "Regression campaign",
                "icp": {"business_type": "Machine shops"},
                "offer": offer,
                "settings": {"provider": "claude", "concurrency": 1, "max_cost_usd": 20}
                | (settings or {}),
            }
        )
        for index in range(leads):
            website = f"https://company{index}.example.com"
            service.upsert_seed(
                campaign["id"],
                LeadSeed(
                    company=f"Company {index}",
                    website=website,
                    email=f"team@company{index}.example.com",
                    email_source=website + "/contact",
                    source_urls=[website],
                ),
            )
        return service, campaign

    return create


@pytest.fixture
def install_runner(monkeypatch):
    def install(callback):
        calls = []
        lock = threading.Lock()

        class Runner:
            def __init__(self, provider, **kwargs):
                self.provider, self.options = provider, kwargs

            def run(self, prompt, schema, *, web, cancel):
                with lock:
                    record = {
                        "index": len(calls),
                        "prompt": prompt,
                        "schema": schema["title"],
                        "web": web,
                        "options": self.options,
                    }
                    calls.append(record)
                return callback(record, cancel)

        monkeypatch.setattr(pipeline, "AgentRunner", Runner)
        return calls

    return install


def queued_context(service, campaign):
    manager = Jobs(service)
    manager.launch = lambda job_id: None
    job = manager.create(campaign["id"], "research")
    return manager, Context(service, job["id"])


def research_data(service, record):
    lead = next(
        lead
        for lead in service.store.list("leads")
        if lead["website"] in record["prompt"]
    )
    return AgentResult(
        demo.research_result(lead), input_tokens=100, output_tokens=50, cost_usd=0.1
    )


def test_concurrent_requests_never_exceed_call_limit(make_service, install_runner):
    service, campaign = make_service(settings={"max_agent_calls": 3, "concurrency": 4})
    _, ctx = queued_context(service, campaign)

    def complete(record, cancel):
        time.sleep(0.03)
        assert not cancel.is_set()
        return AgentResult({"ok": True}, cost_usd=0.1)

    calls = install_runner(complete)

    def request(_):
        try:
            return ctx.call("bounded", Payload)
        except Stopped:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(20)))
    job = service.store.get("jobs", ctx.job_id)
    assert len(calls) == job["agent_calls"] == 3
    assert sum(result is not None for result in results) == 3
    assert job["active_calls"] == job["reserved_cost"] == 0
    assert job["cost_usd"] == pytest.approx(0.3)
    assert ctx.end_status == "budget_exhausted"


def test_cost_reservation_waits_without_cancelling_inflight_work(
    make_service, install_runner
):
    service, campaign = make_service(settings={"max_cost_usd": 1})
    _, ctx = queued_context(service, campaign)
    started, release = threading.Event(), threading.Event()

    def complete(record, cancel):
        if record["index"] == 0:
            started.set()
            assert release.wait(2)
        assert not cancel.is_set()
        return AgentResult({"ok": True}, cost_usd=0.2)

    calls = install_runner(complete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ctx.call, "first", Payload)
        assert started.wait(1)
        second = pool.submit(ctx.call, "second", Payload)
        time.sleep(0.12)
        assert len(calls) == 1
        assert not ctx.stop.is_set()
        release.set()
        assert first.result(timeout=2) == second.result(timeout=2) == {"ok": True}
    assert [call["options"]["max_cost_usd"] for call in calls] == pytest.approx(
        [1, 0.8]
    )
    job = service.store.get("jobs", ctx.job_id)
    assert job["cost_usd"] == pytest.approx(0.4)
    assert job["cost_unreported"] == job["reserved_cost"] == 0


def test_missing_cost_consumes_conservative_allowance(make_service, install_runner):
    service, campaign = make_service(settings={"max_cost_usd": 1})
    _, ctx = queued_context(service, campaign)
    calls = install_runner(lambda record, cancel: AgentResult({"ok": True}))
    assert ctx.call("first", Payload) == {"ok": True}
    with pytest.raises(Stopped):
        ctx.call("second", Payload)
    job = service.store.get("jobs", ctx.job_id)
    assert len(calls) == 1
    assert job["cost_usd"] is None
    assert job["cost_unreported"] == 1
    assert job["cost_unreported_calls"] == 1
    assert job["active_calls"] == job["reserved_cost"] == 0


@pytest.mark.parametrize(
    "code", ["network_error", "invalid_response", "missing_result"]
)
def test_retryable_provider_codes_count_each_attempt_and_unknown_cost(
    make_service, install_runner, code
):
    service, campaign = make_service()
    _, ctx = queued_context(service, campaign)

    def complete(record, cancel):
        if record["index"] == 0:
            raise ProviderError(code, "Retry once.")
        return AgentResult({"ok": True}, cost_usd=0.2)

    calls = install_runner(complete)
    assert ctx.call("retry", Payload) == {"ok": True}
    job = service.store.get("jobs", ctx.job_id)
    assert len(calls) == job["agent_calls"] == 2
    assert job["cost_usd"] == pytest.approx(0.2)
    assert job["cost_unreported"] == 2


@pytest.mark.parametrize(
    "code",
    [
        "auth_required",
        "not_installed",
        "unsupported_cli",
        "native_executable_required",
        "rate_limited",
        "launch_failed",
    ],
)
def test_fatal_provider_error_stops_campaign_before_other_leads(
    make_service, install_runner, code
):
    service, campaign = make_service(leads=8)

    def fail(record, cancel):
        raise ProviderError(code, "Provider needs setup.")

    calls = install_runner(fail)
    job = Jobs(service).create(campaign["id"], "research", background=False)
    assert job["status"] == "failed"
    assert "Provider needs setup" in job["error"]
    assert len(calls) == job["agent_calls"] == 1
    assert job["active_calls"] == job["reserved_cost"] == 0


def test_failed_company_marks_job_failed_and_resume_reuses_success(
    make_service, install_runner
):
    service, campaign = make_service()
    should_fail = [True]

    def complete(record, cancel):
        if "Company 1" in record["prompt"] and should_fail[0]:
            raise ProviderError("provider_failed", "Company request failed.")
        return research_data(service, record)

    calls = install_runner(complete)
    manager = Jobs(service)
    first = manager.create(campaign["id"], "research", background=False)
    assert first["status"] == "failed"
    assert first["run_error_count"] == 1
    assert first["stage_index"] == first["stage_count"] == 1
    assert len(calls) == 2
    should_fail[0] = False
    resumed = manager.control(first["id"], "resume", background=False)
    assert resumed["status"] == "completed"
    assert resumed["run_error_count"] == 0
    assert resumed["error_count"] == 1
    assert len(calls) == resumed["agent_calls"] == 3
    assert all(
        lead["research_status"] == "complete" for lead in service.store.list("leads")
    )


def test_pause_after_provider_result_saves_it_without_paid_duplicate(
    make_service, install_runner
):
    service, campaign = make_service()
    manager = Jobs(service)

    def complete(record, cancel):
        result = research_data(service, record)
        if record["index"] == 0:
            next(iter(manager.contexts.values())).halt("paused")
        return result

    calls = install_runner(complete)
    paused = manager.create(campaign["id"], "research", background=False)
    assert paused["status"] == "paused"
    assert len(calls) == 1
    assert (
        sum(
            lead["research_status"] == "complete"
            for lead in service.store.list("leads")
        )
        == 1
    )
    finished = manager.control(paused["id"], "resume", background=False)
    assert finished["status"] == "completed"
    assert len(calls) == finished["agent_calls"] == 2


def test_cancel_interrupts_active_call_and_retains_unreported_cost(
    make_service, install_runner
):
    service, campaign = make_service(settings={"max_cost_usd": 1})
    started = threading.Event()

    def wait_for_cancel(record, cancel):
        started.set()
        assert cancel.wait(2)
        raise ProviderError("cancelled", "Stopped.")

    calls = install_runner(wait_for_cancel)
    manager = Jobs(service)
    job = manager.create(campaign["id"], "research")
    assert started.wait(1)
    manager.control(job["id"], "cancel")
    manager.threads[job["id"]].join(timeout=2)
    assert not manager.threads[job["id"]].is_alive()
    final = service.store.get("jobs", job["id"])
    assert final["status"] == "cancelled"
    assert final["cost_unreported"] == 1
    assert final["cost_unreported_calls"] == len(calls) == 1
    assert final["reserved_cost"] == final["active_calls"] == 0
    with pytest.raises(AppError):
        manager.control(job["id"], "resume")


def test_two_managers_cannot_resume_same_job_twice(make_service, monkeypatch):
    service, campaign = make_service()
    _, ctx = queued_context(service, campaign)
    service.store.mutate_job(
        ctx.job_id, lambda job: job.update(status="paused", worker_pid=None)
    )
    managers = [Jobs(Service(service.store.root)), Jobs(Service(service.store.root))]
    barrier, launches = threading.Barrier(2), []
    for manager in managers:
        monkeypatch.setattr(
            manager.service, "assert_idle", lambda campaign_id: barrier.wait(timeout=2)
        )
        monkeypatch.setattr(manager, "launch", lambda job_id: launches.append(job_id))

    def resume(manager):
        try:
            return manager.control(ctx.job_id, "resume")["status"]
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        states = list(pool.map(resume, managers))
    assert sorted(states) == ["JOB-STATE", "queued"]
    assert launches == [ctx.job_id]


def test_dead_worker_recovery_preserves_reserved_allowance(make_service, monkeypatch):
    service, campaign = make_service()
    manager, ctx = queued_context(service, campaign)
    service.store.mutate_job(
        ctx.job_id,
        lambda job: job.update(
            status="running",
            worker_pid=99999999,
            active_calls=1,
            reserved_cost=1.5,
            cost_unreported=0.2,
            cost_unreported_calls=1,
        ),
    )

    def dead(pid, signal):
        raise ProcessLookupError()

    monkeypatch.setattr(pipeline.os, "kill", dead)
    manager.recover_interrupted()
    job = service.store.get("jobs", ctx.job_id)
    assert job["status"] == "paused"
    assert job["cost_unreported"] == pytest.approx(1.7)
    assert job["cost_unreported_calls"] == 2
    assert job["active_calls"] == job["reserved_cost"] == 0


def test_resume_during_previous_thread_cleanup_does_not_orphan_job(
    make_service, install_runner
):
    service, campaign = make_service(leads=1)
    _, ctx = queued_context(service, campaign)
    service.store.mutate_job(
        ctx.job_id, lambda job: job.update(status="paused", worker_pid=None)
    )
    release = threading.Event()
    previous = threading.Thread(target=lambda: release.wait(2), daemon=True)
    previous.start()
    manager = Jobs(service)
    manager.threads[ctx.job_id] = previous
    calls = install_runner(lambda record, cancel: research_data(service, record))
    queued = manager.control(ctx.job_id, "resume")
    assert queued["status"] == "queued"
    assert calls == []
    release.set()
    manager.threads[ctx.job_id].join(timeout=2)
    assert service.store.get("jobs", ctx.job_id)["status"] == "completed"
    assert len(calls) == 1


def test_two_managers_cannot_claim_one_queued_job_twice(make_service, install_runner):
    service, campaign = make_service(leads=1)
    _, ctx = queued_context(service, campaign)
    managers = [Jobs(Service(service.store.root)), Jobs(Service(service.store.root))]
    calls = install_runner(lambda record, cancel: research_data(service, record))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda manager: manager.run(ctx.job_id), managers))
    assert len(calls) == 1
    assert service.store.get("jobs", ctx.job_id)["status"] == "completed"


def test_input_loading_failure_does_not_leave_permanently_running_job(
    make_service, monkeypatch
):
    service, campaign = make_service()

    def fail_context(*args):
        raise ValueError("Private failing input details")

    monkeypatch.setattr(pipeline, "Context", fail_context)
    job = Jobs(service).create(campaign["id"], "research", background=False)
    assert job["status"] == "failed"
    assert job["agent_calls"] == 0
    assert job["worker_pid"] is None
    assert "Private failing input" not in job["error"]


def test_offer_brief_reads_document_end_once_and_is_cached(
    make_service, install_runner
):
    service, campaign = make_service(leads=1, offer="")
    tail = "EXCLUSION: never promise a production schedule."
    service.upload_document(
        campaign["id"], "offer.txt", ("Process review details. " * 500 + tail).encode()
    )

    def complete(record, cancel):
        if record["schema"] == "OfferBrief":
            assert tail in record["prompt"]
            assert record["web"] is False
            return AgentResult(
                {"brief": "Offer: Process review. " + tail}, cost_usd=0.1
            )
        assert "Offer: Process review." in record["prompt"]
        assert tail in record["prompt"]
        return research_data(service, record)

    calls = install_runner(complete)
    manager = Jobs(service)
    first = manager.create(campaign["id"], "research", background=False)
    assert first["status"] == "completed"
    assert [call["schema"] for call in calls] == ["OfferBrief", "ResearchResult"]
    saved = service.store.get("campaigns", campaign["id"])
    assert tail in saved["offer_brief"]
    assert saved["offer_brief_fingerprint"]
    second = manager.create(campaign["id"], "research", background=False)
    assert second["status"] == "completed"
    assert second["agent_calls"] == 0
    service.update_campaign(campaign["id"], {"offer": "Only review intake."})
    third = manager.create(campaign["id"], "research", background=False)
    assert third["status"] == "completed"
    assert third["agent_calls"] == 2
    assert [call["schema"] for call in calls].count("OfferBrief") == 2


def test_offer_brief_failure_prevents_generic_company_drafts(
    make_service, install_runner
):
    service, campaign = make_service(leads=1, offer="")
    service.upload_document(
        campaign["id"], "offer.txt", b"Only this document defines the offer."
    )

    def fail(record, cancel):
        raise ProviderError("provider_failed", "Offer could not be summarized.")

    calls = install_runner(fail)
    job = Jobs(service).create(campaign["id"], "research", background=False)
    assert job["status"] == "failed"
    assert [call["schema"] for call in calls] == ["OfferBrief"]
    assert service.store.list("leads")[0]["research_status"] == "pending"


@pytest.mark.parametrize("change", ["icp", "offer", "documents", "model", "pages"])
def test_standalone_draft_blocks_stale_research_and_full_pipeline_refreshes(
    make_service, install_runner, monkeypatch, change
):
    service, campaign = make_service(leads=1, settings={"target_leads": 1})
    monkeypatch.setattr(pipeline, "email_dns_status", lambda email: "mx_found")
    monkeypatch.setattr(
        pipeline, "fetch_public_text", lambda *args, **kwargs: "Public contact page"
    )

    def complete(record, cancel):
        if record["schema"] == "OfferBrief":
            return AgentResult(
                {"brief": "Review quote requests; no guaranteed savings."}, cost_usd=0.1
            )
        if record["schema"] == "SequenceResult":
            return AgentResult(
                demo.sequence_result(service.store.list("leads")[0]), cost_usd=0.1
            )
        return research_data(service, record)

    calls = install_runner(complete)
    manager = Jobs(service)
    assert (
        manager.create(campaign["id"], "research", background=False)["status"]
        == "completed"
    )
    if change == "documents":
        service.upload_document(campaign["id"], "scope.txt", b"No guaranteed savings.")
    else:
        patches = {
            "icp": {"icp": {"location": "A different region"}},
            "offer": {"offer": "Review purchasing instead of quoting."},
            "model": {"settings": {"model": "a-different-model"}},
            "pages": {"settings": {"research_pages": 6}},
        }
        service.update_campaign(campaign["id"], patches[change])
    stale = manager.create(campaign["id"], "draft", background=False)
    assert stale["status"] == "failed"
    assert stale["agent_calls"] == 0
    assert len(calls) == 1
    assert any(
        "RESEARCH-STALE" in event["message"]
        for event in service.store.events(stale["id"])
    )
    refreshed = manager.create(campaign["id"], "pipeline", background=False)
    assert refreshed["status"] == "completed"
    assert refreshed["agent_calls"] == (3 if change == "documents" else 2)
    assert [call["schema"] for call in calls].count("ResearchResult") == 2
    assert len(service.store.list("leads")[0]["drafts"]) == 3


def test_demo_pipeline_stays_offline_and_has_nine_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "AgentRunner",
        lambda *args, **kwargs: pytest.fail("No real agent for demo"),
    )
    monkeypatch.setattr(
        pipeline,
        "fetch_public_text",
        lambda *args, **kwargs: pytest.fail("No web request for demo"),
    )
    service = Service(tmp_path)
    campaign = service.create_demo()
    job = Jobs(service).create(campaign["id"], background=False)
    assert job["status"] == "completed"
    assert job["agent_calls"] == 9
    assert job["stage_index"] == job["stage_count"] == 4
    assert job["cost_unreported_calls"] == 0
    assert all(len(lead["drafts"]) == 3 for lead in service.store.list("leads"))

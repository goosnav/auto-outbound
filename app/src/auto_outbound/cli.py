"""Scriptable interface to the same services used by the browser."""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .config import AppError
from .pipeline import Jobs
from .providers import inspect_providers
from .service import Service

app = typer.Typer(help="Find businesses. Research each one. Review and export outbound drafts.", no_args_is_help=True, pretty_exceptions_enable=False)
campaigns = typer.Typer(help="Create and configure campaigns.", no_args_is_help=True)
job_commands = typer.Typer(help="Inspect durable jobs and their events.", no_args_is_help=True)
suppressions = typer.Typer(help="Global do-not-contact list.", no_args_is_help=True)
app.add_typer(campaigns, name="campaign")
app.add_typer(job_commands, name="jobs")
app.add_typer(suppressions, name="suppress")
console = Console()
errors = Console(stderr=True)
Json = Annotated[bool, typer.Option("--json", help="Machine-readable JSON on stdout.")]


def guarded(fn):
    @functools.wraps(fn)
    def call(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (AppError, ValidationError, OSError, ValueError) as exc:
            code = exc.code if isinstance(exc, AppError) else "INPUT-INVALID"
            message = exc.message if isinstance(exc, AppError) else "Check the arguments, file contents, and path permissions."
            if kwargs.get("json_output"):
                typer.echo(json.dumps({"error": {"code": code, "message": message}}))
            else:
                errors.print(f"[red]{code}[/red] {message}")
            raise typer.Exit(2) from None
    return call


def output(value, json_output: bool):
    if json_output:
        typer.echo(json.dumps(value, ensure_ascii=False))
    else:
        console.print_json(data=value)


@app.callback()
def root(data_dir: Annotated[Path | None, typer.Option("--data-dir", help="Use a separate local workspace.")] = None):
    if data_dir:
        os.environ["AUTO_OUTBOUND_DATA"] = str(data_dir.expanduser().resolve())


@app.command()
@guarded
def gui(port: Annotated[int, typer.Option(min=0, max=65535)] = 0, no_browser: bool = False):
    """Open the local browser app. Port 0 chooses a free port."""
    from .start import serve
    serve(port, open_browser=not no_browser)


@app.command()
@guarded
def doctor(json_output: Json = False):
    """Check CLI installations without invoking a model or reading credentials."""
    service = Service()
    output({"version": __version__, "data_directory": str(service.store.root), "providers": inspect_providers(), "note": "Availability is not an authentication test. Sign in to the chosen CLI first."}, json_output)


@campaigns.command("create")
@guarded
def create_campaign(
    name: Annotated[str, typer.Option(help="Campaign name.")],
    icp: Annotated[str, typer.Option(help="Business/customer type.")],
    location: str = "",
    size: str = "",
    role: Annotated[list[str] | None, typer.Option(help="Repeat for each target role.")] = None,
    must_have: str = "",
    exclude: str = "",
    signals: str = "",
    offer: str = "",
    offer_file: Annotated[list[Path] | None, typer.Option(help="Repeat for up to four supporting documents.")] = None,
    provider: str = "codex",
    model: str = "",
    target: int = 25,
    min_fit: int = 60,
    max_calls: int = 100,
    minutes: int = 90,
    concurrency: int = 2,
    max_cost: float = 10,
    json_output: Json = False,
):
    """Create a bounded campaign; it does not start research."""
    service = Service()
    campaign = service.create_campaign({"name": name, "icp": {"business_type": icp, "location": location, "size": size, "roles": role or ["Owner", "Operations"], "must_have": must_have, "exclude": exclude, "signals": signals}, "offer": offer, "settings": {"provider": provider, "model": model, "target_leads": target, "min_fit": min_fit, "max_agent_calls": max_calls, "max_minutes": minutes, "concurrency": concurrency, "max_cost_usd": max_cost}})
    for path in offer_file or []:
        service.upload_document(campaign["id"], path.name, path.read_bytes())
    output(campaign, json_output)


@campaigns.command("list")
@guarded
def list_campaigns(json_output: Json = False):
    rows = Service().store.list("campaigns")
    if json_output:
        output({"campaigns": rows}, True)
        return
    table = Table("ID", "Campaign", "Agent", "Target")
    for row in rows:
        table.add_row(row["id"], row["name"], row["settings"]["provider"], str(row["settings"]["target_leads"]))
    console.print(table)


@campaigns.command("show")
@guarded
def show_campaign(campaign_id: str, json_output: Json = False):
    output(Service().campaign_detail(campaign_id), json_output)


@campaigns.command("configure")
@guarded
def configure_campaign(campaign_id: str, file: Annotated[Path, typer.Option(help="JSON object with campaign fields to update.")], json_output: Json = False):
    """Update ICP, sender, offer or limits from JSON. Pause an active job first."""
    output(Service().update_campaign(campaign_id, json.loads(file.read_text(encoding="utf-8"))), json_output)


@app.command("offer")
@guarded
def set_offer(campaign_id: str, text: str = "", file: Annotated[list[Path] | None, typer.Option()] = None, json_output: Json = False):
    """Replace offer text and/or add supporting PDF, DOCX, TXT or MD files."""
    service = Service()
    if not text and not file:
        raise AppError("OFFER-EMPTY", "Provide --text or --file.")
    if text:
        service.update_campaign(campaign_id, {"offer": text})
    documents = [service.upload_document(campaign_id, path.name, path.read_bytes()) for path in file or []]
    output({"campaign_id": campaign_id, "documents": documents, "updated": True}, json_output)


@app.command("import-leads")
@guarded
def import_leads(campaign_id: str, file: Path, json_output: Json = False):
    """Import CSV/XLSX. Company and website are required; duplicates merge."""
    output(Service().import_leads(campaign_id, file.name, file.read_bytes()), json_output)


def watch(manager: Jobs, job_id: str, json_output: bool):
    try:
        with Progress(TextColumn("{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"), TimeElapsedColumn(), disable=json_output) as progress:
            task = progress.add_task("Starting", total=1)
            while True:
                job = manager.store.get("jobs", job_id)
                progress.update(task, description=f"{job['current_stage'] or 'starting'} · {job['current_item']}", completed=job["completed"], total=max(job["total"], 1))
                if job["status"] not in {"queued", "running"}:
                    break
                time.sleep(0.25)
    except KeyboardInterrupt:
        manager.control(job_id, "pause")
        while manager.store.get("jobs", job_id)["status"] in {"running", "queued"}:
            time.sleep(0.1)
        job = manager.store.get("jobs", job_id)
    output({"job": job, "events": manager.store.events(job_id)}, json_output)
    if job["status"] != "completed":
        raise typer.Exit({"failed": 3, "budget_exhausted": 4, "paused": 5, "cancelled": 5}.get(job["status"], 3))


@app.command("run")
@guarded
def run(campaign_id: str, stage: str = "pipeline", json_output: Json = False):
    """Run report, pipeline, discover, research, verify, or draft. Ctrl-C pauses safely."""
    manager = Jobs(Service())
    manager.recover_interrupted()
    job = manager.create(campaign_id, stage)
    watch(manager, job["id"], json_output)


@app.command("resume")
@guarded
def resume(job_id: str, json_output: Json = False):
    """Resume saved work, retaining call/usage counters and successful results."""
    manager = Jobs(Service())
    manager.recover_interrupted()
    manager.control(job_id, "resume")
    watch(manager, job_id, json_output)


@app.command("pause")
@guarded
def pause(job_id: str, json_output: Json = False):
    output({"job": Jobs(Service()).control(job_id, "pause")}, json_output)


@app.command("cancel")
@guarded
def cancel(job_id: str, json_output: Json = False):
    output({"job": Jobs(Service()).control(job_id, "cancel")}, json_output)


@job_commands.command("list")
@guarded
def list_jobs(campaign_id: Annotated[str | None, typer.Option("--campaign")] = None, json_output: Json = False):
    output({"jobs": Service().store.list("jobs", campaign_id)}, json_output)


@job_commands.command("show")
@guarded
def show_job(job_id: str, json_output: Json = False):
    store = Service().store
    output({"job": store.get("jobs", job_id), "events": store.events(job_id)}, json_output)


@app.command("leads")
@guarded
def list_leads(campaign_id: str, status: str = "", min_fit: int = 0, json_output: Json = False):
    leads = Service().campaign_detail(campaign_id)["leads"]
    leads = [lead for lead in leads if lead["fit_score"] >= min_fit and (not status or lead["status"] == status)]
    if json_output:
        output({"leads": leads}, True)
        return
    table = Table("ID", "Company", "Fit", "Email status", "Review")
    for lead in leads:
        table.add_row(lead["id"], lead["company"], str(lead["fit_score"]), lead["email_status"], lead["status"])
    console.print(table)


@app.command("review")
@guarded
def review(lead_id: str, status: Annotated[str, typer.Option(help="approved, rejected, suppressed, or drafted")], note: str = "", json_output: Json = False):
    """Record human review. Approval never overrides suppression or export checks."""
    output(Service().update_lead(lead_id, {"status": status, "review_notes": note}), json_output)


@app.command("edit-lead")
@guarded
def edit_lead(lead_id: str, file: Annotated[Path, typer.Option()], json_output: Json = False):
    """Apply contact corrections or edited drafts from a JSON object."""
    output(Service().update_lead(lead_id, json.loads(file.read_text(encoding="utf-8"))), json_output)


@app.command("export")
@guarded
def export_campaign(campaign_id: str, output_path: Annotated[Path, typer.Option("--output", "-o")], format: str = "csv", scope: str = "all", force: bool = False, json_output: Json = False):
    """Export all research, ready approved emails, or a sourced calling list."""
    if output_path.exists() and not force:
        raise AppError("FILE-EXISTS", "Output exists. Choose another path or use --force.")
    raw = Service().export(campaign_id, format, scope)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(raw)
    output({"path": str(output_path.resolve()), "format": format, "scope": scope, "bytes": len(raw)}, json_output)


@app.command("verify-import")
@guarded
def import_verification(campaign_id: str, file: Path, source: str = "", json_output: Json = False):
    """Import verifier CSV: email,status,verification_source,verification_at."""
    output(Service().import_verification(campaign_id, file.name, file.read_bytes(), source), json_output)


@app.command("outcomes-import")
@guarded
def import_outcomes(campaign_id: str, file: Path, json_output: Json = False):
    """Import sender/CRM CSV: email,outcome,at. Bounces/opt-outs suppress globally."""
    output(Service().import_outcomes(campaign_id, file.name, file.read_bytes()), json_output)


@suppressions.command("add")
@guarded
def suppress(email: str = "", domain: str = "", reason: str = "Do not contact", json_output: Json = False):
    if bool(email) == bool(domain):
        raise AppError("SUPPRESSION-TARGET", "Provide exactly one of --email or --domain.")
    output(Service().suppress({"kind": "email" if email else "domain", "value": email or domain, "reason": reason}), json_output)


@suppressions.command("list")
@guarded
def list_suppressions(json_output: Json = False):
    output({"suppressions": Service().store.suppressions()}, json_output)


@suppressions.command("import")
@guarded
def import_suppressions(file: Path, json_output: Json = False):
    output(Service().import_suppressions(file.name, file.read_bytes()), json_output)


@app.command("demo")
@guarded
def create_demo(run_pipeline: Annotated[bool, typer.Option("--run")] = False, json_output: Json = False):
    """Create a clearly fictional offline campaign; optionally run all stages."""
    service = Service()
    campaign = service.create_demo()
    if run_pipeline:
        manager = Jobs(service)
        job = manager.create(campaign["id"])
        watch(manager, job["id"], json_output)
    else:
        output({"campaign": campaign}, json_output)


@app.command("backup")
@guarded
def backup(output_path: Annotated[Path, typer.Option("--output", "-o")], json_output: Json = False):
    """Create a consistent SQLite backup containing campaigns, docs, and outcomes."""
    if output_path.exists():
        raise AppError("FILE-EXISTS", "Choose a new backup path.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    store = Service().store
    with store.connect() as source, sqlite3.connect(output_path) as target:
        source.backup(target)
    output({"path": str(output_path.resolve()), "bytes": output_path.stat().st_size}, json_output)


if __name__ == "__main__":
    app()

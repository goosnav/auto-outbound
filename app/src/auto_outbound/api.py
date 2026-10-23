"""Loopback-only API; state changes require an in-memory per-launch token."""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__
from .config import AppError, static_root
from .files import MAX_FILE
from .models import CampaignInput, JobRequest, SuppressionInput
from .pipeline import Jobs
from .providers import inspect_providers
from .service import Service


def create_app(service: Service | None = None, *, on_quit=None, testing: bool = False) -> FastAPI:
    service = service or Service()
    jobs = Jobs(service)
    session = secrets.token_urlsafe(32)
    provider_cache = {"at": 0, "value": []}

    @asynccontextmanager
    async def lifespan(app):
        jobs.recover_interrupted()
        app.state.ready = True
        yield
        app.state.ready = False
        jobs.shutdown()

    app = FastAPI(title="Auto Outbound", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.service, app.state.jobs, app.state.ready = service, jobs, False

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        host = request.headers.get("host", "")
        host_name = host.split(":", 1)[0]
        allowed = {"127.0.0.1", "localhost"} | ({"testserver"} if testing else set())
        if host_name not in allowed:
            return JSONResponse({"error": {"code": "LOCAL-HOST", "message": "Open the local app address."}}, status_code=403)
        origin = request.headers.get("origin")
        if (origin is not None and origin != f"http://{host}") or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"error": {"code": "LOCAL-ORIGIN", "message": "Cross-site requests are not allowed."}}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-session-token", ""), session):
                return JSONResponse({"error": {"code": "SESSION", "message": "Reload the app to restore this session."}}, status_code=403)
        length = request.headers.get("content-length", "0")
        try:
            if int(length) > MAX_FILE + 1024 * 1024:
                raise ValueError
        except ValueError:
            return JSONResponse({"error": {"code": "FILE-SIZE", "message": "Request is too large."}}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        if request.url.path != "/api/docs":
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        # Never reflect raw inputs (which can contain offer text) into error output.
        errors = exc.errors()
        field = ".".join(str(part) for part in errors[0].get("loc", [])) if errors else "input"
        return JSONResponse({"error": {"code": "INPUT-INVALID", "message": f"Check {field or 'the input'} and try again."}}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected(request, exc):
        return JSONResponse({"error": {"code": "APP-ERROR", "message": "The operation failed. Saved work is intact. Try again."}}, status_code=500)

    @app.get("/health/ready")
    def ready():
        return {"ready": app.state.ready, "app": "auto-outbound", "version": __version__}

    @app.get("/api/session")
    def get_session():
        if time.monotonic() - provider_cache["at"] > 20:
            provider_cache["value"] = inspect_providers()
            provider_cache["at"] = time.monotonic()
        return {"token": session, "version": __version__, "providers": provider_cache["value"], "data_directory": str(service.store.root)}

    @app.get("/api/campaigns")
    def campaigns():
        return {"campaigns": sorted(service.store.list("campaigns"), key=lambda c: c["updated_at"], reverse=True)}

    @app.post("/api/campaigns")
    def new_campaign(body: CampaignInput):
        return service.create_campaign(body.model_dump())

    @app.post("/api/demo")
    def new_demo():
        return {"campaign": service.create_demo()}

    @app.get("/api/campaigns/{campaign_id}")
    def campaign(campaign_id: str):
        return service.campaign_detail(campaign_id)

    @app.patch("/api/campaigns/{campaign_id}")
    def update_campaign(campaign_id: str, body: dict):
        return service.update_campaign(campaign_id, body)

    @app.patch("/api/leads/{lead_id}")
    def update_lead(lead_id: str, body: dict):
        return service.update_lead(lead_id, body)

    async def uploaded(file: UploadFile):
        try:
            raw = await file.read(MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise AppError("FILE-SIZE", "Files must be 8 MB or smaller.", 413)
            return file.filename or "upload", raw
        finally:
            await file.close()

    @app.post("/api/campaigns/{campaign_id}/documents")
    async def document(campaign_id: str, file: Annotated[UploadFile, File()]):
        name, raw = await uploaded(file)
        return await run_in_threadpool(service.upload_document, campaign_id, name, raw)

    @app.delete("/api/campaigns/{campaign_id}/documents/{document_id}")
    def remove_document(campaign_id: str, document_id: str):
        service.delete_document(campaign_id, document_id)
        return {"ok": True}

    @app.post("/api/campaigns/{campaign_id}/import")
    async def import_leads(campaign_id: str, file: Annotated[UploadFile, File()]):
        name, raw = await uploaded(file)
        return await run_in_threadpool(service.import_leads, campaign_id, name, raw)

    @app.post("/api/campaigns/{campaign_id}/verification/import")
    async def import_verification(campaign_id: str, file: Annotated[UploadFile, File()], source: Annotated[str, Form()] = ""):
        name, raw = await uploaded(file)
        return await run_in_threadpool(service.import_verification, campaign_id, name, raw, source)

    @app.post("/api/campaigns/{campaign_id}/outcomes/import")
    async def import_outcomes(campaign_id: str, file: Annotated[UploadFile, File()]):
        name, raw = await uploaded(file)
        return await run_in_threadpool(service.import_outcomes, campaign_id, name, raw)

    @app.post("/api/campaigns/{campaign_id}/jobs")
    def start_job(campaign_id: str, body: JobRequest):
        return {"job": jobs.create(campaign_id, body.stage)}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return {"job": service.store.get("jobs", job_id), "events": service.store.events(job_id)}

    @app.post("/api/jobs/{job_id}/{action}")
    def control_job(job_id: str, action: str):
        return {"job": jobs.control(job_id, action)}

    @app.get("/api/campaigns/{campaign_id}/export")
    def export(campaign_id: str, format: str = "csv", scope: str = "all"):
        raw = service.export(campaign_id, format, scope)
        mime = "text/csv; charset=utf-8" if format == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return Response(raw, media_type=mime, headers={"Content-Disposition": f'attachment; filename="outbound-{campaign_id}-{scope}.{format}"'})

    @app.get("/api/suppressions")
    def suppressions():
        return {"suppressions": service.store.suppressions()}

    @app.post("/api/suppressions")
    def add_suppression(body: SuppressionInput):
        return service.suppress(body.model_dump())

    @app.post("/api/suppressions/import")
    async def import_suppressions(file: Annotated[UploadFile, File()]):
        name, raw = await uploaded(file)
        return await run_in_threadpool(service.import_suppressions, name, raw)

    @app.post("/api/quit")
    def quit_app():
        if on_quit:
            threading.Timer(0.2, on_quit).start()
        else:
            jobs.shutdown()
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=static_root()), name="static")
    app.mount("/", StaticFiles(directory=static_root(), html=True), name="ui")
    return app

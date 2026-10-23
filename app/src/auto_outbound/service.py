"""Shared application service used by HTTP, CLI and background workers."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from . import files
from .config import AppError, data_root
from .models import CampaignInput, Lead, LeadSeed, SuppressionInput, domain_of, now
from .quality import ready_reasons, sequence_issues
from .research import email_syntax
from .storage import Store


def ident() -> str:
    return uuid.uuid4().hex[:16]


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Service:
    def __init__(self, root: Path | None = None):
        self.store = Store(root or data_root())

    def create_campaign(self, raw: dict) -> dict:
        model = CampaignInput.model_validate(raw)
        campaign = model.model_dump()
        if campaign["settings"]["provider"] == "demo":
            campaign["demo"] = True
        elif campaign["demo"]:
            raise AppError("DEMO-PROVIDER", "Demo campaigns use the demo provider.")
        campaign.update(id=ident(), created_at=now(), updated_at=now(), schema_version=1)
        self.store.save("campaigns", campaign)
        return campaign

    def assert_idle(self, campaign_id: str):
        if any(j["status"] in {"running", "queued"} for j in self.store.list("jobs", campaign_id)):
            raise AppError("JOB-ACTIVE", "Pause the current job before editing this campaign.", 409)

    def update_campaign(self, campaign_id: str, patch: dict) -> dict:
        self.assert_idle(campaign_id)
        old = self.store.get("campaigns", campaign_id)
        fields = set(CampaignInput.model_fields)
        if set(patch) - fields:
            raise AppError("FIELD-UNKNOWN", "This campaign update includes an unknown field.")
        merged = {key: old[key] for key in fields}
        for key, value in patch.items():
            if key in {"settings", "icp", "sender"} and isinstance(value, dict):
                merged[key] = merged[key] | value
            else:
                merged[key] = value
        parsed = CampaignInput.model_validate(merged).model_dump()
        if parsed["demo"] != old["demo"] or (parsed["settings"]["provider"] == "demo") != old["demo"]:
            raise AppError("DEMO-ISOLATION", "Create a separate campaign to switch between demo and real data.")
        changed_content = any(parsed[key] != old[key] for key in ("offer", "icp"))
        sender_changed = parsed["sender"] != old["sender"]
        campaign = old | parsed | {"updated_at": now()}
        self.store.save("campaigns", campaign)
        if changed_content or sender_changed:
            for lead in self.store.list("leads", campaign_id):
                if lead["status"] in {"suppressed", "rejected"}:
                    continue
                if changed_content:
                    lead.update(drafts=[], draft_fingerprint="", qa_issues=[])
                lead["status"] = "researched" if lead["research_status"] == "complete" else "new"
                lead["updated_at"] = now()
                self.store.save("leads", lead)
        return campaign

    def is_suppressed(self, lead: dict, entries: list[dict] | None = None) -> bool:
        email, domain = lead.get("email", "").casefold(), lead.get("domain", "").casefold()
        for entry in self.store.suppressions() if entries is None else entries:
            if entry["kind"] == "email" and email == entry["value"]:
                return True
            if entry["kind"] == "domain" and (domain == entry["value"] or domain.endswith("." + entry["value"])):
                return True
        return False

    def lead_context(self, campaign_id: str) -> tuple[list[dict], dict[str, str], dict[str, str]]:
        """Read shared export guards once, even for a thousand-row campaign."""
        domains, emails = {}, {}
        for other in self.store.list("leads"):
            if other["campaign_id"] != campaign_id and other.get("outcome"):
                domains.setdefault(other["domain"], other["outcome"])
                if other["email"]:
                    emails.setdefault(other["email"].casefold(), other["outcome"])
        return self.store.suppressions(), domains, emails

    def decorate(self, campaign: dict, lead: dict, context: tuple | None = None) -> dict:
        lead = lead.copy()
        suppressions, prior_domains, prior_emails = context if context is not None else self.lead_context(campaign["id"])
        blockers = ready_reasons(campaign, lead, self.is_suppressed(lead, suppressions))
        prior = prior_domains.get(lead["domain"]) or prior_emails.get(lead["email"].casefold())
        lead["prior_activity"] = lead.get("outcome") or prior or ""
        if lead.get("outcome"):
            blockers.append("Prior activity: " + lead["outcome"])
        if prior:
            blockers.append("Prior activity in another campaign: " + prior)
        if lead.get("fit_score", 0) < campaign["settings"]["min_fit"]:
            blockers.append("Below minimum fit")
        if not lead.get("email_source"):
            blockers.append("Email source needed")
        lead["export_blockers"], lead["ready_to_send"] = blockers, not blockers
        return lead

    def campaign_detail(self, campaign_id: str) -> dict:
        campaign = self.store.get("campaigns", campaign_id)
        context = self.lead_context(campaign_id)
        leads = [self.decorate(campaign, lead, context) for lead in self.store.list("leads", campaign_id)]
        leads.sort(key=lambda lead: (-lead["fit_score"], lead["company"].lower()))
        counts = {
            "total": len(leads),
            "qualified": sum(lead["fit_score"] >= campaign["settings"]["min_fit"] and lead["research_status"] == "complete" for lead in leads),
            "researched": sum(lead["research_status"] == "complete" for lead in leads),
            "drafted": sum(bool(lead["drafts"]) for lead in leads),
            "approved": sum(lead["status"] == "approved" for lead in leads),
            "ready": sum(lead["ready_to_send"] for lead in leads),
            "suppressed": sum(self.is_suppressed(lead, context[0]) or lead["status"] == "suppressed" for lead in leads),
            "verified": sum(lead["email_status"] == "verified" for lead in leads),
            "phone": sum(bool(lead["phone"]) for lead in leads),
        }
        jobs = sorted(self.store.list("jobs", campaign_id), key=lambda j: j["created_at"], reverse=True)
        outcomes = {}
        for lead in leads:
            if lead["outcome"]:
                outcomes[lead["outcome"]] = outcomes.get(lead["outcome"], 0) + 1
        return {"campaign": campaign, "leads": leads, "jobs": jobs, "counts": counts, "documents": self.store.documents(campaign_id), "outcomes": outcomes}

    def upsert_seed(self, campaign_id: str, seed: LeadSeed) -> tuple[dict, bool]:
        domain = domain_of(seed.website)
        if not domain:
            raise AppError("LEAD-WEBSITE", "A lead needs its official company website.")
        payload = seed.model_dump()
        if payload["email"]:
            payload["email"] = payload["email"].casefold()
        if payload["email"] and not email_syntax(payload["email"]):
            payload["email"] = ""
            payload["email_source"] = ""
        # Multiple contacts can be retained as notes; the sending row is one business/domain.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM leads WHERE campaign_id=? AND domain=?", (campaign_id, domain)).fetchone()
            if row:
                lead = json.loads(row[0])
                for key, value in payload.items():
                    if value and not lead.get(key):
                        lead[key] = value
                lead["source_urls"] = list(dict.fromkeys(lead["source_urls"] + payload["source_urls"]))[:20]
                lead["updated_at"] = now()
                created = False
            else:
                lead = Lead(**payload, id=ident(), campaign_id=campaign_id, domain=domain, created_at=now(), updated_at=now(), email_status="published" if payload["email"] and payload["email_source"] else "unknown" if payload["email"] else "missing").model_dump()
                other = db.execute("SELECT data FROM leads WHERE domain=? AND campaign_id<>? LIMIT 1", (domain, campaign_id)).fetchone()
                if other:
                    lead["risks"].append("Already exists in another campaign. Check prior contact before export.")
                created = True
            self.store.save_in(db, "leads", lead)
        if self.is_suppressed(lead):
            lead.update(status="suppressed", email_status="suppressed")
            self.store.save("leads", lead)
        return lead, created

    def update_lead(self, lead_id: str, patch: dict) -> dict:
        lead = self.store.get("leads", lead_id)
        self.assert_idle(lead["campaign_id"])
        allowed = {"status", "drafts", "review_notes", "email", "phone", "contact_name", "contact_role", "email_status", "verification_source", "verification_at", "email_source", "phone_source", "contact_source"}
        if set(patch) - allowed:
            raise AppError("FIELD-UNKNOWN", "This lead update includes an unknown field.")
        changed = any(lead.get(key) != value for key, value in patch.items() if key not in {"status", "review_notes"})
        if changed and lead["status"] == "approved" and patch.get("status") != "approved":
            lead["status"] = "drafted" if lead["drafts"] else "researched"
        if "email" in patch:
            patch["email"] = patch["email"].strip().casefold()
            if patch["email"] and not email_syntax(patch["email"]):
                raise AppError("EMAIL-INVALID", "Enter a valid work email address.")
            if patch["email"] != lead["email"]:
                lead.update(email_status="unknown", verification_source="", verification_at="", email_source="", status="drafted" if lead["drafts"] else "new")
        edited = Lead.model_validate(lead | patch | {"updated_at": now()}).model_dump()
        edited["qa_issues"] = sequence_issues(edited) if edited["drafts"] else []
        if edited["email_status"] == "verified" and (not edited["verification_source"] or not edited["verification_at"]):
            raise AppError("EMAIL-EVIDENCE", "Record the mailbox verifier and check date first.")
        if edited["email_status"] == "verified":
            self.check_verification_date(edited["verification_at"])
        if edited["status"] == "approved":
            if self.is_suppressed(edited) or lead["status"] == "suppressed":
                raise AppError("LEAD-SUPPRESSED", "This contact is suppressed.")
            if edited["qa_issues"] or not edited["drafts"]:
                raise AppError("DRAFT-QA", "Resolve the sequence checks before approval.")
        if edited["status"] == "suppressed":
            self.suppress({"kind": "email" if edited["email"] else "domain", "value": edited["email"] or edited["domain"], "reason": edited["review_notes"] or "Suppressed during review"})
            edited["email_status"] = "suppressed"
        self.store.save("leads", edited)
        return self.decorate(self.store.get("campaigns", lead["campaign_id"]), edited)

    def suppress(self, raw: dict) -> dict:
        entry = SuppressionInput.model_validate(raw).model_dump()
        value = entry["value"].casefold().strip()
        if entry["kind"] == "email":
            if not email_syntax(value):
                raise AppError("EMAIL-INVALID", "Use a complete email address.")
        else:
            value = domain_of(value)
            if not value or "." not in value:
                raise AppError("DOMAIN-INVALID", "Use a company domain such as example.com.")
        entry.update(value=value, created_at=now())
        with self.store.connect() as db:
            db.execute("INSERT INTO suppressions VALUES (?,?,?,?) ON CONFLICT(kind,value) DO UPDATE SET reason=excluded.reason", (entry["kind"], value, entry["reason"], entry["created_at"]))
        # The independent suppression table is always rechecked before draft/export.
        suppressions = self.store.suppressions()
        for lead in self.store.list("leads"):
            if self.is_suppressed(lead, suppressions):
                lead.update(status="suppressed", email_status="suppressed", updated_at=now())
                self.store.save("leads", lead)
        return entry

    def upload_document(self, campaign_id: str, name: str, raw: bytes) -> dict:
        self.assert_idle(campaign_id)
        self.store.get("campaigns", campaign_id)
        docs = self.store.documents(campaign_id, include_text=True)
        digest = hashlib.sha256(raw).hexdigest()
        match = next((doc for doc in docs if doc["sha256"] == digest), None)
        if match:
            return {"id": match["id"], "name": match["name"], "characters": len(match["text"]), "duplicate": True}
        text = files.document_text(name, raw)
        if len(docs) >= 4 or sum(len(doc["text"]) for doc in docs) + len(text) > 60000:
            raise AppError("OFFER-SIZE", "Use up to four offer documents, totaling 60,000 characters.")
        doc_id = ident()
        safe_name = Path(name.replace("\\", "/")).name[:200]
        with self.store.connect() as db:
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?)", (doc_id, campaign_id, safe_name, text, digest, now()))
        self.invalidate_drafts(campaign_id)
        return {"id": doc_id, "name": safe_name, "characters": len(text)}

    def delete_document(self, campaign_id: str, doc_id: str):
        self.assert_idle(campaign_id)
        with self.store.connect() as db:
            changed = db.execute("DELETE FROM documents WHERE id=? AND campaign_id=?", (doc_id, campaign_id)).rowcount
        if not changed:
            raise AppError("NOT-FOUND", "Document not found.", 404)
        self.invalidate_drafts(campaign_id)

    def invalidate_drafts(self, campaign_id: str):
        for lead in self.store.list("leads", campaign_id):
            lead.update(draft_fingerprint="", drafts=[], qa_issues=[], updated_at=now())
            if lead["status"] not in {"rejected", "suppressed"}:
                lead["status"] = "researched" if lead["research_status"] == "complete" else "new"
            self.store.save("leads", lead)

    def import_leads(self, campaign_id: str, name: str, raw: bytes) -> dict:
        self.assert_idle(campaign_id)
        self.store.get("campaigns", campaign_id)
        rows = files.read_rows(name, raw)
        aliases = {"company_name": "company", "business_name": "company", "organization": "company", "company_website": "website", "url": "website", "domain": "website", "email_address": "email", "work_email": "email", "phone_number": "phone", "business_phone": "phone", "full_name": "contact_name", "job_title": "contact_role", "title": "contact_role", "city": "location", "linkedin": "linkedin_url", "employee_count": "size"}
        count, duplicates, errors = 0, 0, []
        for i, row in enumerate(rows, 2):
            values = {aliases.get(k, k): value for k, value in row.items() if value}
            if "contact_name" not in values and (values.get("first_name") or values.get("last_name")):
                values["contact_name"] = " ".join(filter(None, (values.get("first_name"), values.get("last_name"))))
            fields = {k: value for k, value in values.items() if k in LeadSeed.model_fields}
            for key in ("source_urls", "social_urls"):
                value = fields.get(key, "")
                if value:
                    try:
                        fields[key] = json.loads(value) if value.startswith("[") else [p.strip() for p in value.split(";") if p.strip()]
                    except ValueError:
                        fields[key] = []
            try:
                _, created = self.upsert_seed(campaign_id, LeadSeed.model_validate(fields))
                count += int(created)
                duplicates += int(not created)
            except (ValueError, ValidationError, AppError):
                if len(errors) < 50:
                    errors.append({"row": i, "message": "Check company, official website, and contact field formats."})
        return {"imported": count, "duplicates": duplicates, "rejected": len(rows) - count - duplicates, "errors": errors}

    def import_verification(self, campaign_id: str, name: str, raw: bytes, source: str = "") -> dict:
        self.assert_idle(campaign_id)
        self.store.get("campaigns", campaign_id)
        rows = files.read_rows(name, raw)
        leads = self.leads_by_email(campaign_id)
        mapped = {"valid": "verified", "deliverable": "verified", "verified": "verified", "invalid": "invalid", "undeliverable": "invalid", "catch_all": "catch_all", "accept_all": "catch_all", "risky": "risky", "unknown": "unknown"}
        updated, skipped = 0, 0
        for row in rows:
            matches = leads.get((row.get("email") or row.get("email_address") or "").casefold(), [])
            status = mapped.get(files.header(row.get("status") or row.get("email_status") or row.get("result")))
            provider = row.get("verification_source") or row.get("provider") or source
            stamp = row.get("verification_at") or row.get("checked_at")
            if not matches or not status or not provider or not stamp:
                skipped += 1
                continue
            try:
                self.check_verification_date(stamp)
            except AppError:
                skipped += 1
                continue
            for lead in matches:
                if self.is_suppressed(lead):
                    skipped += 1
                    continue
                lead.update(email_status=status, verification_source=provider[:300], verification_at=stamp[:100], updated_at=now())
                self.store.save("leads", lead)
                updated += 1
            if status == "invalid":
                self.suppress({"kind": "email", "value": matches[0]["email"], "reason": "Verifier reported invalid"})
        return {"updated": updated, "skipped": skipped}

    def leads_by_email(self, campaign_id: str) -> dict[str, list[dict]]:
        grouped = {}
        for lead in self.store.list("leads", campaign_id):
            if lead["email"]:
                grouped.setdefault(lead["email"].casefold(), []).append(lead)
        return grouped

    @staticmethod
    def check_verification_date(stamp: str):
        try:
            checked = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if checked.tzinfo is None:
                checked = checked.replace(tzinfo=UTC)
            if (checked - datetime.now(UTC)).total_seconds() > 300:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise AppError("EMAIL-DATE", "Use an actual verification date in ISO format, such as 2026-10-23.") from exc

    def import_outcomes(self, campaign_id: str, name: str, raw: bytes) -> dict:
        self.assert_idle(campaign_id)
        self.store.get("campaigns", campaign_id)
        rows = files.read_rows(name, raw)
        leads = self.leads_by_email(campaign_id)
        allowed = {"sent", "replied", "positive", "booked", "won", "lost", "bounced", "unsubscribed", "not_interested"}
        count, skipped = 0, 0
        for row in rows:
            matches = leads.get((row.get("email") or "").casefold(), [])
            outcome = files.header(row.get("outcome") or row.get("status"))
            if not matches or outcome not in allowed:
                skipped += 1
                continue
            for lead in matches:
                lead.update(outcome=outcome, outcome_at=(row.get("at") or now())[:100], updated_at=now())
                self.store.save("leads", lead)
                count += 1
            if outcome in {"bounced", "unsubscribed", "not_interested"}:
                self.suppress({"kind": "email", "value": matches[0]["email"], "reason": outcome.replace("_", " ")})
        return {"updated": count, "skipped": skipped}

    def import_suppressions(self, name: str, raw: bytes) -> dict:
        count, skipped = 0, 0
        for row in files.read_rows(name, raw):
            kind = row.get("kind") or ("email" if row.get("email") else "domain")
            value = row.get("value") or row.get("email") or row.get("domain") or ""
            try:
                self.suppress({"kind": kind, "value": value, "reason": row.get("reason") or "Imported suppression"})
                count += 1
            except (AppError, ValidationError):
                skipped += 1
        return {"imported": count, "skipped": skipped}

    def export(self, campaign_id: str, format: str = "csv", scope: str = "all") -> bytes:
        detail = self.campaign_detail(campaign_id)
        leads = detail["leads"]
        if scope in {"approved", "ready"}:
            # Several businesses can share a decision-maker's mailbox. Keep the
            # highest-fit approved row once in a sending handoff, not one per domain.
            unique = {}
            for lead in leads:
                if lead["ready_to_send"]:
                    unique.setdefault(lead["email"].casefold(), lead)
            leads = list(unique.values())
        elif scope == "callable":
            suppressions = self.store.suppressions()
            leads = [lead for lead in leads if not detail["campaign"]["demo"] and lead["phone"] and lead["phone_source"] and lead["status"] not in {"suppressed", "rejected"} and not self.is_suppressed(lead, suppressions) and not lead["prior_activity"]]
        elif scope != "all":
            raise AppError("EXPORT-SCOPE", "Choose all, approved, or callable leads.")
        if scope != "all" and not leads:
            raise AppError("EXPORT-EMPTY", "No eligible leads yet. Review export blockers or export all research.")
        return files.spreadsheet(detail["campaign"], leads, format)

    def create_demo(self) -> dict:
        return self.create_campaign({"name": "Precision shops · Demo", "demo": True, "icp": {"business_type": "Independent precision machine shops", "location": "Southern California", "size": "10–75 people", "roles": ["Owner", "Operations"], "must_have": "Accepts drawing-based quote requests", "signals": "Public RFQ form"}, "offer": "A small tool that turns emailed quote requests into a review queue and flags missing drawing, material, quantity, and delivery details. The shop retains all pricing and order approvals. Start with a short process review. No claimed savings or existing client results.", "sender": {"name": "Alex Example", "company": "Example Engineering · Demo", "email": "alex@example.invalid", "postal_address": "Fictional demo address — replace before real use"}, "settings": {"provider": "demo", "target_leads": 4, "discovery_queries": 2, "max_agent_calls": 20, "max_minutes": 10}})

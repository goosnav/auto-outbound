"""Versioned inputs and provider-independent records."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def public_url(value: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value):
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None:
        raise ValueError("Use an http or https website address.")
    if len(value) > 2048:
        raise ValueError("Website address is too long.")
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, parts.query, ""))


def domain_of(value: str) -> str:
    try:
        host = urlsplit(public_url(value)).hostname or ""
        return host.lower().removeprefix("www.").encode("idna").decode("ascii")
    except (ValueError, UnicodeError):
        return ""


class ICP(Model):
    business_type: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=1000)
    size: str = Field(default="", max_length=500)
    roles: list[str] = Field(default_factory=lambda: ["Owner", "Operations"], max_length=12)
    must_have: str = Field(default="", max_length=3000)
    exclude: str = Field(default="", max_length=3000)
    signals: str = Field(default="", max_length=3000)


class Sender(Model):
    name: str = Field(default="", max_length=200)
    company: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=254)
    postal_address: str = Field(default="", max_length=500)


class Settings(Model):
    provider: Literal["codex", "claude", "demo"] = "codex"
    model: str = Field(default="", max_length=120)
    target_leads: int = Field(default=25, ge=1, le=1000)
    min_fit: int = Field(default=60, ge=0, le=100)
    discovery_queries: int = Field(default=4, ge=1, le=40)
    max_agent_calls: int = Field(default=100, ge=1, le=5000)
    max_minutes: int = Field(default=90, ge=1, le=1440)
    concurrency: int = Field(default=2, ge=1, le=4)
    agent_timeout_seconds: int = Field(default=180, ge=15, le=900)
    max_turns: int = Field(default=8, ge=2, le=40)
    max_cost_usd: float = Field(default=10, ge=0.1, le=1000)
    research_pages: int = Field(default=4, ge=1, le=10)


class CampaignInput(Model):
    name: str = Field(min_length=1, max_length=120)
    icp: ICP = Field(default_factory=ICP)
    offer: str = Field(default="", max_length=30000)
    sender: Sender = Field(default_factory=Sender)
    settings: Settings = Field(default_factory=Settings)
    demo: bool = False


class Evidence(Model):
    url: str = Field(max_length=2048)
    title: str = Field(default="", max_length=300)
    quote: str = Field(default="", max_length=600)
    field: str = Field(default="company", max_length=80)
    checked_at: str = ""

    _url = field_validator("url")(public_url)


class LeadSeed(Model):
    company: str = Field(min_length=1, max_length=250)
    website: str = Field(min_length=1, max_length=2048)
    location: str = Field(default="", max_length=500)
    industry: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=2000)
    size: str = Field(default="", max_length=200)
    contact_name: str = Field(default="", max_length=200)
    contact_role: str = Field(default="", max_length=200)
    contact_source: str = Field(default="", max_length=2048)
    email: str = Field(default="", max_length=254)
    email_source: str = Field(default="", max_length=2048)
    phone: str = Field(default="", max_length=100)
    phone_source: str = Field(default="", max_length=2048)
    linkedin_url: str = Field(default="", max_length=2048)
    social_urls: list[str] = Field(default_factory=list, max_length=12)
    source_urls: list[str] = Field(default_factory=list, max_length=20)

    _urls = field_validator("website", "email_source", "phone_source", "contact_source", "linkedin_url")(public_url)

    @field_validator("source_urls", "social_urls")
    @classmethod
    def valid_urls(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(public_url(x) for x in values if x))


class DiscoveryResult(Model):
    companies: list[LeadSeed] = Field(max_length=25)
    coverage_notes: str = Field(max_length=2000)


class ResearchResult(LeadSeed):
    evidence: list[Evidence] = Field(min_length=1, max_length=20)
    fit_score: int = Field(ge=0, le=100)
    fit_reasons: list[str] = Field(max_length=8)
    risks: list[str] = Field(max_length=12)
    pain_hypothesis: str = Field(max_length=1500)
    personalization: str = Field(max_length=1500)
    research_summary: str = Field(max_length=3000)


class Draft(Model):
    step: int = Field(ge=1, le=3)
    delay_days: int = Field(ge=0, le=60)
    subject: str = Field(max_length=200)
    body: str = Field(max_length=3000)
    source_urls: list[str] = Field(default_factory=list, max_length=12)
    angle: str = Field(default="", max_length=500)

    _urls = field_validator("source_urls")(LeadSeed.valid_urls.__func__)


class SequenceResult(Model):
    drafts: list[Draft] = Field(min_length=3, max_length=3)


class Lead(LeadSeed):
    id: str
    campaign_id: str
    domain: str
    created_at: str
    updated_at: str
    evidence: list[Evidence] = Field(default_factory=list)
    fit_score: int = 0
    fit_reasons: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    pain_hypothesis: str = ""
    personalization: str = ""
    research_summary: str = ""
    research_status: Literal["pending", "complete", "error"] = "pending"
    research_fingerprint: str = ""
    draft_fingerprint: str = ""
    email_status: Literal["missing", "published", "mx_found", "no_mx", "unknown", "invalid", "verified", "suppressed", "catch_all", "risky"] = "missing"
    email_checked_at: str = ""
    verification_source: str = ""
    verification_at: str = ""
    status: Literal["new", "researched", "drafted", "approved", "rejected", "suppressed"] = "new"
    drafts: list[Draft] = Field(default_factory=list)
    qa_issues: list[str] = Field(default_factory=list)
    review_notes: str = ""
    outcome: str = ""
    outcome_at: str = ""


class JobRequest(Model):
    stage: Literal["pipeline", "discover", "research", "draft", "verify"] = "pipeline"


class OfferBrief(Model):
    """A bounded summary derived from the complete accepted offer materials."""

    brief: str = Field(min_length=1, max_length=6000)


class SuppressionInput(Model):
    kind: Literal["email", "domain"]
    value: str = Field(min_length=1, max_length=254)
    reason: str = Field(default="Do not contact", max_length=500)


def strict_schema(model: type[BaseModel]) -> dict:
    """Codex requires every object property to be required, including defaults."""
    schema = model.model_json_schema()

    def walk(obj):
        if isinstance(obj, dict):
            obj.pop("default", None)
            if obj.get("type") == "object":
                obj["additionalProperties"] = False
                obj["required"] = list(obj.get("properties", {}))
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(schema)
    return schema

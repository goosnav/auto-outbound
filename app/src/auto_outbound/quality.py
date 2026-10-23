from __future__ import annotations

import re
from datetime import UTC, datetime

from .research import email_syntax


def words(text: str) -> list[str]:
    return re.findall(r"\b[\w]+(?:['’-][\w]+)*\b", text)


def sequence_issues(lead: dict) -> list[str]:
    drafts = lead.get("drafts", [])
    issues = []
    if len(drafts) != 3 or sorted(d.get("step") for d in drafts) != [1, 2, 3]:
        return ["A complete sequence needs emails 1, 2, and 3."]
    evidence_urls = set(lead.get("source_urls", [])) | {e["url"] for e in lead.get("evidence", [])}
    bodies = set()
    for d in drafts:
        prefix = f"Email {d['step']}: "
        subject, body = d.get("subject", ""), d.get("body", "")
        if not 1 <= len(words(subject)) <= 5:
            issues.append(prefix + "use a subject of 1–5 words.")
        if re.match(r"\s*(re|fw|fwd)\s*:", subject, re.I):
            issues.append(prefix + "remove the simulated reply/forward prefix.")
        if "\n" in subject or "\r" in subject:
            issues.append(prefix + "subject must be one line.")
        limit = 90 if d["step"] == 1 else 65
        if not body or len(words(body)) > limit:
            issues.append(prefix + f"keep the body within {limit} words.")
        if any(len(words(sentence)) > 25 for sentence in re.split(r"[.!?]\s+", body)):
            issues.append(prefix + "shorten sentences over 25 words.")
        if body.count("?") > 1:
            issues.append(prefix + "ask one question.")
        if re.search(r"\{\{|\[\s*(name|company|insert|first.?name|your|offer)\b|<[^>]+>", body, re.I):
            issues.append(prefix + "replace the placeholder or HTML.")
        if re.search(r"\b(game.changer|revolutioni[sz]e|unlock your|synergy|touching base|just bumping|circle back|guaranteed|10x)\b", body, re.I):
            issues.append(prefix + "remove hype or empty follow-up language.")
        if len(re.findall(r"https?://", body)) > 1:
            issues.append(prefix + "use at most one link.")
        if not d.get("source_urls") or any(url not in evidence_urls for url in d.get("source_urls", [])):
            issues.append(prefix + "attach supporting dossier sources.")
        normalized = re.sub(r"\s+", " ", body).casefold()
        if normalized in bodies:
            issues.append(prefix + "add a distinct follow-up.")
        bodies.add(normalized)
    return issues


def sender_footer(sender: dict) -> str:
    identity = " · ".join(s for s in (sender.get("name"), sender.get("company")) if s)
    lines = [identity, sender.get("email", ""), sender.get("postal_address", ""), 'Business development. To stop follow-ups, reply "no thanks".']
    return "\n".join(x for x in lines if x)


def ready_reasons(campaign: dict, lead: dict, suppressed: bool = False) -> list[str]:
    reasons = []
    if campaign.get("demo"):
        reasons.append("Demo data")
    if suppressed or lead["status"] == "suppressed":
        reasons.append("Suppressed")
    if lead["status"] != "approved":
        reasons.append("Awaiting approval")
    if not email_syntax(lead.get("email", "")):
        reasons.append("Missing or invalid email")
    if lead.get("email_status") != "verified" or not lead.get("verification_source") or not lead.get("verification_at"):
        reasons.append("Mailbox verification needed")
    else:
        try:
            stamp = datetime.fromisoformat(lead["verification_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - stamp).total_seconds()
            if age < -300 or age > 30 * 86400:
                reasons.append("Refresh mailbox verification (30 days)")
        except (ValueError, TypeError):
            reasons.append("Invalid verification date")
    if not all(campaign["sender"].get(k) for k in ("name", "company", "email", "postal_address")):
        reasons.append("Complete sender identity and postal address")
    elif not email_syntax(campaign["sender"]["email"]):
        reasons.append("Invalid sender email")
    reasons.extend(sequence_issues(lead))
    return reasons

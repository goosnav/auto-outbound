"""Offline fictional fixtures. These records can never enter a sending export."""

from .models import now

NAMES = ["Northline Precision", "Mesa Toolworks", "Vector Fabrication", "Arcwell Components"]
SERVICES = ["five-axis machining", "prototype tooling", "sheet-metal assemblies", "short-run components"]


def discovery_result(limit: int, existing: list[str]) -> dict:
    companies = []
    for i, name in enumerate(NAMES):
        domain = name.split()[0].lower() + ".example"
        if domain in existing:
            continue
        url = "https://" + domain
        companies.append({"company": name + " (Demo)", "website": url, "location": ["Torrance, CA", "Long Beach, CA", "Anaheim, CA", "Gardena, CA"][i], "industry": "Precision manufacturing", "description": "Fictional sample company. " + SERVICES[i].capitalize() + ".", "size": "25–50 staff (fictional)", "email": "quotes@" + domain, "email_source": url + "/contact", "phone": f"+1 202-555-014{i}", "phone_source": url + "/contact", "contact_name": ["Jordan", "Casey", "Taylor", "Morgan"][i] + " Example", "contact_role": "Operations manager (fictional)", "contact_source": url + "/team", "source_urls": [url + "/capabilities", url + "/contact"]})
    return {"companies": companies[:limit], "coverage_notes": "Four fictional companies. No web research or real contacts."}


def research_result(lead: dict) -> dict:
    from .models import LeadSeed

    base = {key: lead[key] for key in LeadSeed.model_fields}
    index = next((i for i, name in enumerate(NAMES) if name in lead["company"]), 0)
    base.update(evidence=[{"url": lead["website"] + "/capabilities", "title": "Fictional capabilities page", "quote": f"We accept drawing-based requests for {SERVICES[index]}.", "field": "company", "checked_at": now()}, {"url": lead["email_source"], "title": "Fictional contact page", "quote": lead["email"], "field": "email", "checked_at": now()}], fit_score=[88, 79, 72, 64][index], fit_reasons=["Industry: 30/30", "Geography: 20/20", "Size: 10/15", "Relevant RFQ workflow: 15/20", "Relevant contact: 13/15", "Demonstration score; not real research."], risks=["FICTIONAL DEMO. Do not contact.", "Intake workload and budget are unknown."], pain_hypothesis="Does checking incoming quote requests take avoidable manual work?", personalization=f"The fictional capabilities page lists {SERVICES[index]} and drawing-based RFQs.", research_summary=f"Fictional {SERVICES[index]} shop with a published RFQ contact. The public workflow would be worth asking about; the presence of a problem is unverified.")
    return base


def sequence_result(lead: dict) -> dict:
    index = next((i for i, name in enumerate(NAMES) if name in lead["company"]), 0)
    greeting = lead["contact_name"].split()[0] if lead["contact_name"] else "there"
    return {"drafts": [
        {"step": 1, "delay_days": 0, "subject": "Drawing-based quote requests", "body": f"Hi {greeting},\n\nYour capabilities page lists {SERVICES[index]} and drawing-based quote requests. I build small tools that turn incoming requests into a review queue and flag missing details. Your team keeps pricing and order approval.\n\nDoes checking those requests take time that would be useful to reduce?", "source_urls": [lead["website"] + "/capabilities"], "angle": "Connect the published RFQ workflow to a simple discovery question."},
        {"step": 2, "delay_days": 4, "subject": "Missing RFQ details", "body": f"Hi {greeting},\n\nOne possible starting point is checking whether each request includes a drawing, material, quantity, and delivery date. It can fit around existing quoting tools.\n\nWould a short review of that step be useful?", "source_urls": [lead["website"] + "/capabilities"], "angle": "Offer a concrete, limited first step."},
        {"step": 3, "delay_days": 9, "subject": "Close the loop", "body": f"Hi {greeting},\n\nI'll leave this here. If quote intake already works well, there may be nothing to change.\n\nIs this worth revisiting, or should I close the loop? A no is fine.", "source_urls": [lead["website"] + "/capabilities"], "angle": "Invite a decision without pressure."},
    ]}

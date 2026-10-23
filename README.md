# Auto Outbound

**Find relevant businesses. Research each one. Review three short emails. Export.**

A local outbound-preparation app from Goosnav Labs. Bring an installed, authenticated **Codex** or **Claude Code** CLI. Use the browser workspace or the same workflow from the command line. Your campaigns and offers stay in a local SQLite database; the selected AI provider receives the task inputs needed for research and drafting.

There is no sending transport. Export the reviewed work to your email platform or a calling list.

![Auto Outbound lead workspace with real progress, research status, and a fictional demo list](docs/screenshots/leads.png)

*Fictional demo data. [Sequence review](docs/screenshots/review.png) · [Mobile workspace](docs/screenshots/mobile.png).*

## Start

### Source checkout

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
git clone https://github.com/goosnav/auto-outbound.git
cd auto-outbound
uv run --project app --no-dev python app/run.py gui
```

The app opens on a free loopback port. Python and locked dependencies are handled by uv. For a complete offline demonstration, choose **Try the demo**. The four companies and all contact details are explicitly fictional and cannot enter a ready-to-send export.

For live research, install and sign in to [Codex](https://developers.openai.com/codex/cli/) or [Claude Code](https://code.claude.com/docs/en/overview) first. The app checks required CLI capabilities. An older CLI produces an update instruction rather than weakening its tool restrictions. Provider subscriptions, usage limits, and any API charges are separate from this app.

```sh
uv run --project app --no-dev python app/run.py doctor --json
```

If a desktop launcher cannot find your CLI, use `AUTO_OUTBOUND_CODEX_BIN` or `AUTO_OUTBOUND_CLAUDE_BIN` with the native executable's absolute path. Windows `.cmd`/`.bat` wrappers are deliberately unsupported; point to the underlying `.exe`.

### Native ZIP

The repository includes a reproducible builder for one ZIP with macOS universal, Windows x64/ARM64, and Linux x86_64/ARM64 launcher images. Native launchers open a setup page and provision a pinned Python runtime without requiring Python or Node on the customer machine. Codex/Claude still needs its own installation and login.

All five launcher images have been built. The Linux x86_64 AppImage has passed a headless browser smoke test, managed runtime setup, second-launch reuse, restart persistence, and GUI Quit. **macOS, Windows, and ARM desktop operation remain unverified.** These are unsigned development builds, not a cross-platform accepted release. [Installation and build instructions](docs/INSTALL.md).

Use GitHub **Actions → Build native ZIP → Run workflow** to build a downloadable workflow artifact. The workflow does not publish a release.

## Workflow

1. **Audience.** Set business type, geography, size, decision-maker roles, required attributes, exclusions, and useful buying signals. Set the target count and minimum fit.
2. **Offer.** Describe the outcome you can deliver and your actual proof/constraints. Add up to four TXT, Markdown, PDF, DOCX, or CSV documents. Scanned PDFs need OCR first. Long offers/documents are reduced to a cached brief using the complete accepted text.
3. **Find.** Independent discovery batches search public sources. Domain normalization merges duplicate companies. Missing contacts remain missing. Discovery stops at the target, the configured batch limit, or repeated low yield.
4. **Research.** Each company gets a separate agent call and a saved dossier: capabilities, appropriate business contacts, evidence excerpts, fit reasoning, unknowns, and an explicitly tentative pain hypothesis.
5. **Check.** The app checks email syntax and DNS routing, and attempts bounded independent reads of cited pages. It records inaccessible sources and unmatched evidence. It never calls MX success “mailbox verified.”
6. **Draft.** A separate company-specific call writes all three emails from its saved dossier. If the audience, offer materials, provider/model, or research-page limit changes, run Research again before Draft. No repeated browsing per follow-up. Subjects are 1–5 words; first emails are capped at 90 words, follow-ups at 65. Copy checks flag long sentences, placeholders, unsupported source URLs, duplicate follow-ups, hype, and simulated reply prefixes.
7. **Review and export.** Edit copy, inspect sources, approve/reject, and record mailbox verification. Export full research, a ready-to-send list, or a sourced calling list. Import sender/CRM outcomes to prevent repeat contact and track replies, meetings, and wins.

The writing approach is **plain English inspired by Simplified Technical English**. It does not claim formal ASD-STE100 compliance. This app does not implement the standard's complete controlled vocabulary and rule set.

## What a lead contains

| Group | Fields |
|---|---|
| Business | Name, official website/domain, location, industry, size, description |
| Contact | Name, role, published work email, business phone, LinkedIn/social links |
| Evidence | Separate contact source URLs, supporting excerpts, research dates, source-check gaps |
| Fit | Score with reasons, risks, observed workflow, tentative pain hypothesis |
| Sequence | Three subjects/bodies, distinct angles, supporting sources, proposed days 0/4/9 |
| Review | Approval, copy checks, notes, suppression, export blockers |
| Verification | Published/MX/verified/invalid/catch-all/risky/unknown, verifier and timestamp |
| Outcomes | Sent, replied, positive, booked, won, lost, bounced, unsubscribed, not interested |

One lead row represents one company domain and one primary contact. Sending exports deduplicate shared mailboxes and keep the highest-fit approved record; verifier and outcome imports update every matching company. This version does not model a multi-contact account hierarchy. It does not invent headcount, budget authority, pain, or contact details to fill columns.

## Controls and persistence

- Explicit maximum agent calls, elapsed running time, per-call timeout, and concurrency (1–4; default 2).
- Claude turn limits and dollar allowances. **Claude's reported cost and budget estimate are not a guaranteed billing ceiling.** Unknown cost after failed/cancelled calls retains a conservative allowance; the UI separates this from reported spend. Codex does not expose an equivalent native dollar cap, so it is bounded by calls, time, and output size instead.
- At most one retry for specified malformed/transient failures, within the same call budget.
- Successful discovery batches, research, and drafts are saved independently. Pause/resume and crash recovery reuse successful work. Input changes invalidate the relevant cached result and approval.
- Actual counts and stage progress. Unknown usage displays as unavailable, not zero.
- Global email/domain suppression survives campaigns and takes precedence over approval. Sender outcomes block repeated export; negative outcomes create suppression.
- SQLite WAL transactions, bounded file imports, spreadsheet formula neutralization, per-launch session token, same-origin checks, public-only source fetching, and isolated agent working directories.
- No API credentials stored by this app, no analytics, no hosted account, no background outreach.

Data defaults to `AutoOutbound` under your operating system's application-data directory. `--data-dir` or `AUTO_OUTBOUND_DATA` selects another workspace. See [data and recovery](docs/INSTALL.md#data-and-recovery). Avoid committing databases, offers, contact lists, or exports.

## CLI

Use `uv run --project app --no-dev python app/run.py` as the command prefix below. All operational commands support `--json`; the GUI calls the same service layer.

```sh
# Define a campaign. This does not start an agent.
uv run --project app --no-dev python app/run.py campaign create \
  --name "Aerospace machine shops" \
  --icp "Independent machine shops making aerospace components" \
  --location "Southern California" \
  --role Owner --role Operations \
  --must-have "Drawing-based quote requests" \
  --offer-file examples/offer.md \
  --provider codex --target 25 --max-calls 80 --json

# Use the returned campaign ID.
uv run --project app --no-dev python app/run.py run CAMPAIGN_ID --stage pipeline
uv run --project app --no-dev python app/run.py leads CAMPAIGN_ID --min-fit 70
uv run --project app --no-dev python app/run.py export CAMPAIGN_ID \
  --format xlsx --scope all --output exports/research.xlsx
```

| Command | Purpose |
|---|---|
| `campaign list / show / configure` | Inspect or update campaign settings with JSON |
| `offer ID --text ... --file ...` | Update offer text and documents |
| `import-leads ID FILE` | Import CSV/XLSX; company and website required |
| `run ID --stage discover/research/verify/draft/pipeline` | Run only the work needed |
| `pause JOB_ID`, `resume JOB_ID`, `cancel JOB_ID` | Control saved jobs; Ctrl-C pauses |
| `jobs list / show` | Progress, events, usage, failures |
| `leads ID --json` | Full records and export blockers |
| `edit-lead ID --file patch.json` | Edit contact/copy or verification evidence |
| `review LEAD_ID --status approved` | Record human review |
| `verify-import ID FILE --source "Verifier"` | Import dated external mailbox checks |
| `outcomes-import ID FILE` | Import sending/CRM results |
| `suppress add --email ...` or `--domain ...` | Global exclusion |
| `suppress import FILE` | Load existing customers, opt-outs, and bad addresses |
| `export ID --scope all/approved/callable --output FILE` | CSV/XLSX handoff |
| `backup --output FILE` | Consistent SQLite backup |
| `demo --run --json` | Offline, end-to-end fictional example |

Exit codes: `0` completed; `2` invalid input/setup; `3` failed job; `4` exhausted job budget; `5` paused/cancelled. JSON errors have `error.code` and `error.message`. CLI workers run in the foreground; use your process supervisor for unattended execution.

## Sending handoff

**Full research** includes all rows and their status. It is not a sending list. **Ready to send** (`--scope approved`) requires human approval, recent mailbox verification (30 days), complete sender identity/address, adequate fit, usable copy, a contact source, no suppression, and no prior imported activity. A sourced phone list uses `--scope callable`.

Use [the CSV templates](examples/) for verifier results, outcomes, and suppression. Keep `footer` attached to every email in your sender. Apply the exported subjects/bodies as per-lead variables. Follow-up delays are suggestions measured from the first send, not a sending schedule managed here.

Your sending platform must authenticate the sending domain, control volume, manage unsubscribe headers, and stop sequences on replies, bounces, or opt-outs. Configure SPF/DKIM/DMARC and monitor actual delivery. [Gmail sender guidance](https://support.google.com/mail/answer/81126?hl=en) and [FTC commercial-email guidance](https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business) explain the relevant requirements; the app does not certify a campaign's compliance.

## Tests and architecture

```sh
uv sync --project app --locked
uv run --project app python -m pytest -c app/pyproject.toml tests
uv run --project app ruff check --config app/pyproject.toml app/src tests
```

`models` and `quality` define the records and invariants; `service` owns workflows; `storage` persists them; `pipeline` runs bounded jobs; `providers` isolates the CLI processes; `api` and `cli` are adapters. Static HTML/CSS/JavaScript ships without a frontend build or runtime CDN. [HTTP contract](docs/API.md).

Tests exercise actual SQLite and API workflows, CLI parity, subprocess contracts, resumability, budgets, suppression, verifier imports, source-fetch restrictions, and spreadsheet exports. Browser smoke tests exercised the real app with fictional data. Live authenticated model output quality has **not** been benchmarked in this build; provider tests use executable fixtures. Review an initial small live cohort before increasing the target.

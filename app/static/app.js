"use strict";

(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const clamp = (n, min, max) => Math.max(min, Math.min(max, Number(n) || 0));
  const words = (value) => String(value || "").trim().split(/\s+/).filter(Boolean).length;
  const readable = (value) => String(value || "unknown").replace(/_/g, " ");
  const safeURL = (value) => {
    try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) ? url.href : ""; }
    catch { return ""; }
  };
  const hostname = (value) => {
    try { return new URL(value).hostname.replace(/^www\./, ""); }
    catch { return String(value || ""); }
  };
  const date = (value) => {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  };
  const icon = (name) => {
    const paths = {
      search: '<circle cx="8.5" cy="8.5" r="5.5"/><path d="m13 13 4 4"/>',
      list: '<rect x="3" y="3" width="14" height="14" rx="2"/><path d="M7 7h6M7 10h6M7 13h4"/>',
      upload: '<path d="M10 13V3m-4 4 4-4 4 4M3 13v4h14v-4"/>',
      close: '<path d="m5 5 10 10M15 5 5 15"/>',
      check: '<path d="m4 10 4 4 8-8"/>',
      arrow: '<path d="M3 10h13m-5-5 5 5-5 5"/>',
      mail: '<rect x="2" y="4" width="16" height="12" rx="2"/><path d="m3 5 7 6 7-6"/>',
      export: '<path d="M10 2v10m-4-4 4 4 4-4M3 13v4h14v-4"/>',
      external: '<path d="M11 3h6v6m0-6-8 8M7 4H3v13h13v-4"/>',
    };
    return `<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.list}</svg>`;
  };
  const state = {
    session: null, campaigns: [], activeId: null, detail: null, view: "leads",
    search: "", status: "all", quality: "all", selected: new Set(),
    job: null, events: [], logOpen: false, pollTimer: null, polling: false,
    dismissedJobs: new Set(), busy: 0, exportScope: "all",
    lead: null, leadWorking: null, leadTab: "overview", leadDirty: false,
    suppressions: [], configDirty: false,
  };
  const defaults = {
    icp: {business_type:"",location:"",size:"",roles:[],must_have:"",exclude:"",signals:""},
    sender: {name:"",company:"",email:"",postal_address:""},
    settings: {provider:"codex",model:"",target_leads:25,min_fit:60,discovery_queries:4,max_agent_calls:100,max_minutes:90,concurrency:2,agent_timeout_seconds:180,max_turns:8,max_cost_usd:10,research_pages:4},
  };
  const labels = {missing:"No address",published:"Published",mx_found:"MX found",no_mx:"No mail server",unknown:"Not verified",invalid:"Invalid",verified:"Mailbox verified",suppressed:"Do not contact",catch_all:"Catch-all",risky:"Risky"};
  const tips = {published:"Found in a public source. Mailbox delivery has not been verified.",mx_found:"The domain accepts mail. This does not verify the individual mailbox.",verified:"Mailbox verification was recorded with a named source.",unknown:"Delivery has not been verified.",no_mx:"No usable mail exchange was found.",missing:"No email address is available.",invalid:"The address failed validation.",suppressed:"This contact must not be exported for sending.",catch_all:"The domain accepts arbitrary addresses. This individual mailbox is not verified.",risky:"The verifier flagged this address as risky."};
  const statusTone = (status) => ["approved", "verified", "completed"].includes(status) ? "good" : ["suppressed", "rejected", "failed", "invalid", "no_mx", "risky"].includes(status) ? "bad" : ["drafted", "running", "published", "paused", "budget_exhausted", "catch_all"].includes(status) ? "accent" : "";
  const badge = (text, tone = "", title = "") => `<span class="badge ${escape(tone)}"${title ? ` title="${escape(title)}"` : ""}>${escape(text)}</span>`;
  const isDemo = (campaign = state.detail?.campaign) => Boolean(campaign?.demo || campaign?.settings?.provider === "demo");
  const isActive = (job) => Boolean(job && ["queued", "running"].includes(job.status));
  const campaign = () => state.detail?.campaign;
  const leadList = () => state.detail?.leads || [];
  const counts = () => {
    const leads = leadList();
    return {
      total: leads.length,
      researched: leads.filter((l) => l.research_current ?? l.research_status === "complete").length,
      stale: leads.filter((l) => l.research_status === "complete" && l.research_current === false).length,
      drafted: leads.filter((l) => l.drafts?.length).length,
      approved: leads.filter((l) => l.status === "approved").length,
      ready: leads.filter((l) => l.ready_to_send).length,
      verified: leads.filter((l) => l.email_status === "verified").length,
      missing: leads.filter((l) => !l.email).length,
      phone: leads.filter((l) => l.phone).length,
      suppressed: state.detail?.counts?.suppressed ?? leads.filter((l) => l.status === "suppressed").length,
    };
  };

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.session?.token) headers.set("X-Session-Token", state.session.token);
    if (options.body && !(options.body instanceof FormData) && typeof options.body !== "string") {
      headers.set("Content-Type", "application/json");
      options.body = JSON.stringify(options.body);
    }
    let response;
    try { response = await fetch(path, {...options, headers, cache:"no-store", credentials:"same-origin"}); }
    catch { throw new Error("Cannot reach Auto Outbound. Reopen the app, then retry."); }
    if (!response.ok) {
      let error;
      try { error = await response.json(); } catch { error = {}; }
      const detail = error.error?.message || error.message || (typeof error.detail === "string" ? error.detail : "");
      throw new Error(detail || `Request failed (${response.status}).`);
    }
    if (options.download) return response;
    if (response.status === 204) return {};
    return response.json();
  }
  async function busy(label, fn, button) {
    state.busy += 1;
    $("#request-status").textContent = label;
    if (button) button.disabled = true;
    try { return await fn(); }
    catch (error) { toast(error.message || "Something went wrong. Try again.", true); return null; }
    finally {
      state.busy -= 1;
      if (!state.busy) $("#request-status").textContent = "";
      if (button?.isConnected) button.disabled = false;
    }
  }
  function toast(message, error = false) {
    $$("#toast-region .toast:not(.error)").forEach((item)=>item.remove());
    while($("#toast-region").children.length>=2)$("#toast-region").firstElementChild.remove();
    const item = document.createElement("div");
    item.className = `toast${error ? " error" : ""}`;
    if(error)item.setAttribute("role","alert");
    item.innerHTML = `<span>${escape(message)}</span><button class="toast-close" type="button" aria-label="Dismiss notification">×</button>`;
    $("button", item).addEventListener("click", () => item.remove());
    $("#toast-region").append(item);
    setTimeout(() => item.remove(), error ? 12000 : 6500);
  }
  function openDialog(id) {
    const dialog = $(id);
    if (!dialog.open) dialog.showModal();
    return dialog;
  }
  function closeDialog(element) {
    const dialog=element.closest("dialog");
    if(dialog?.id==="lead-dialog"&&state.leadDirty&&!window.confirm("Close without saving edits?"))return;
    dialog?.close();
  }
  function providerOptions(selected, editing = false) {
    const choices=editing?(selected==="demo"?["demo"]:["codex","claude"]):["codex","claude","demo"];
    return choices.map((id) => {
      const provider = state.session?.providers?.find((p) => p.id === id);
      const name = id === "codex" ? "Codex" : id === "claude" ? "Claude Code" : "Demo · fictional data";
      return `<option value="${id}"${selected === id ? " selected" : ""}>${name}${id !== "demo" && !provider?.available ? " · setup needed" : ""}</option>`;
    }).join("");
  }
  function providerNotice(selected) {
    const provider = state.session?.providers?.find((p) => p.id === selected);
    if (selected === "demo") return `<div class="notice">Demo uses fictional companies and contacts.</div>`;
    if (provider?.available) return `<div class="settings-row"><span class="status-dot"></span>${escape(selected === "codex" ? "Codex" : "Claude Code")} detected<span class="text-muted">${escape(provider.version || "")}</span></div>`;
    return `<div class="notice neutral"><span>Install and sign in to ${selected === "claude" ? "Claude Code" : "Codex"} to run research.</span><button class="button small quiet" type="button" data-action="help">Setup ↗</button></div>`;
  }

  function renderSidebar() {
    const nav=$("#campaign-list"), priorScroll=nav.scrollLeft, changed=nav.dataset.active!==state.activeId;
    $("#campaign-list").innerHTML = state.campaigns.length ? state.campaigns.map((c) => `<button type="button" class="campaign-link${state.activeId === c.id ? " active" : ""}" data-action="select-campaign" data-id="${escape(c.id)}"${state.activeId === c.id ? ' aria-current="page"' : ""}><span class="campaign-icon" aria-hidden="true">${isDemo(c) ? "D" : "↗"}</span><span class="name">${escape(c.name)}</span></button>`).join("") : '<span class="sidebar-empty">Your saved lists appear here.</span>';
    nav.dataset.active=state.activeId||"";
    const selected=$(".campaign-link.active",nav);
    nav.scrollLeft=changed&&selected&&matchMedia("(max-width:620px)").matches?selected.offsetLeft-nav.offsetLeft:priorScroll;
  }
  function renderEmpty() {
    return `<section class="empty-workspace"><h1>No lists</h1><div class="inline-actions"><button class="button" type="button" data-action="demo">Open sample</button></div></section>`;
  }

  function navigation() {
    return `<nav class="nav-tabs" aria-label="Lead list workflow">${[["campaign","Setup"],["leads","Leads"],["export","Report"]].map(([key,label])=>`<button class="nav-tab${state.view===key?" active":""}" data-action="view" data-view="${key}" type="button"${state.view===key?' aria-current="page"':""}>${label}</button>`).join("")}</nav>`;
  }

  function heading() {
    const runActions=`<div class="heading-actions"><select id="run-stage" aria-label="Choose workflow stage"><option value="report">Find & research</option><option value="discover">Find companies</option><option value="research">Research existing list</option><option value="verify">Check contacts</option><option value="pipeline">Research + email drafts</option><option value="draft"${state.view==="review"?" selected":""}>Email drafts</option></select><button class="button primary" type="button" data-action="run"${isActive(state.job)?" disabled":""}>${state.view==="review"?"Draft":"Generate"} ${icon("arrow")}</button></div>`;
    return `<div class="page-heading"><div class="list-title"><h1>${escape(campaign()?.name||"Leads")}</h1>${isDemo()?badge("Sample","demo"):""}</div>${state.view==="campaign"?'<button class="button primary" type="submit" form="campaign-form">Save</button>':["leads","review"].includes(state.view)?runActions:""}</div>`;
  }

  function renderMetrics() {
    const region = $("#metrics-region");
    if (!region) return;
    const n = counts();
    region.innerHTML = `<div class="metrics-strip">${[["Companies",n.total],["Researched",n.researched],["With email",n.total-n.missing],["With phone",n.phone]].map(([label,num]) => `<div class="metric"><span class="metric-value">${num}</span><span class="metric-name">${label}</span></div>`).join("")}</div>`;
    $$('[data-action="export"][data-scope="all"]').forEach((button)=>button.disabled=!n.total);
  }
  function leadPanel() {
    return `<div id="metrics-region"></div><section class="table-panel" aria-label="Lead list"><div class="table-toolbar"><div class="search-control">${icon("search")}<input id="lead-search" type="search" placeholder="Search" aria-label="Search companies or contacts" value="${escape(state.search)}"></div><div class="toolbar-filters"><select id="status-filter" aria-label="Filter lead status">${[["all","All statuses"],["new","New"],["researched","Researched"],["drafted","Drafted"],["approved","Approved"],["rejected","Rejected"],["suppressed","Do not contact"]].map(([value,label])=>`<option value="${value}"${state.status===value?" selected":""}>${label}</option>`).join("")}</select><select id="quality-filter" aria-label="Filter fit and contacts">${[["all","All fit scores"],["60","Fit 60+"],["80","Fit 80+"],["missing","Missing email"],["unverified","Unverified email"]].map(([value,label])=>`<option value="${value}"${state.quality===value?" selected":""}>${label}</option>`).join("")}</select></div><div class="inline-actions"><button class="button small" type="button" data-action="import">${icon("upload")} Import</button><button class="button small" type="button" data-action="export" data-format="csv" data-scope="all"${!counts().total?" disabled":""}>${icon("export")} CSV</button><button class="button small" type="button" data-action="export" data-format="xlsx" data-scope="all"${!counts().total?" disabled":""}>Excel</button></div></div><div id="leads-table-region"></div></section>`;
  }
  function filteredLeads() {
    const term = state.search.toLowerCase().trim();
    return leadList().filter((lead) => {
      if (state.view === "review" && !lead.drafts?.length) return false;
      if (term && ![lead.company,lead.website,lead.domain,lead.contact_name,lead.email,lead.location].some((value)=>String(value||"").toLowerCase().includes(term))) return false;
      if (state.status !== "all" && lead.status !== state.status) return false;
      if (["60","80"].includes(state.quality) && Number(lead.fit_score) < Number(state.quality)) return false;
      if (state.quality === "missing" && lead.email) return false;
      if (state.quality === "unverified" && (!lead.email || lead.email_status === "verified")) return false;
      return true;
    }).sort((a,b) => Number(b.fit_score||0)-Number(a.fit_score||0) || String(a.company).localeCompare(String(b.company)));
  }
  function renderLeadTable() {
    const region = $("#leads-table-region");
    if (!region) return;
    const leads = filteredLeads();
    const bulk = state.selected.size ? `<div class="bulk-toolbar"><span>${state.selected.size} selected</span><button class="button small" type="button" data-action="bulk-approve">Approve</button><button class="button small quiet" type="button" data-action="bulk-reject">Reject</button><button class="button small quiet" type="button" data-action="clear-selection">Clear</button></div>` : "";
    if (!leads.length) {
      const hasAny = leadList().length > 0;
      const title = state.view === "review" && !counts().drafted ? "No sequences yet." : hasAny ? "No matching leads." : "No leads yet.";
      const explanation = state.view === "review" && !counts().drafted ? "Run research and drafting, then review the result here." : hasAny ? "Try a different filter or search." : "Generate leads or import a CSV or Excel file.";
      region.innerHTML = `${bulk}<div class="empty-table"><div class="empty-icon">${icon(state.view==="review"?"mail":"list")}</div><h3>${title}</h3><p>${explanation}</p><div class="inline-actions">${hasAny && state.view!=="review" ? '<button class="button small" type="button" data-action="reset-filters">Reset filters</button>' : `<button class="button primary" type="button" data-action="run"${isActive(state.job)?" disabled":""}>${state.view==="review"?"Prepare sequences":"Generate"} ${icon("arrow")}</button><button class="button" type="button" data-action="import">Import a list</button>`}</div></div>`;
      return;
    }
    const allChecked = leads.every((l)=>state.selected.has(l.id));
    region.innerHTML = `${bulk}<div class="table-wrap"><table><thead><tr><th class="check-cell"><input id="select-all" type="checkbox" aria-label="Select all visible leads"${allChecked?" checked":""}></th><th>COMPANY</th><th>CONTACT</th><th>FIT</th><th>STATUS</th><th aria-label="Open lead"></th></tr></thead><tbody>${leads.map((lead)=>{
      const score = clamp(lead.fit_score,0,100);
      const scored = lead.research_current!==false && (score>0 || lead.research_status==="complete");
      return `<tr data-lead-id="${escape(lead.id)}" class="${state.selected.has(lead.id)?"selected":""}"><td class="check-cell"><input type="checkbox" data-select-lead="${escape(lead.id)}" aria-label="Select ${escape(lead.company)}"${state.selected.has(lead.id)?" checked":""}></td><td><button class="company-name" type="button" data-action="open-lead" data-id="${escape(lead.id)}">${escape(lead.company || "Unnamed company")}</button><span class="cell-sub">${escape(lead.domain || hostname(lead.website) || lead.location || "Website not found")}</span><span class="mobile-lead-meta">${badge(lead.research_status==="complete"&&lead.research_current===false?"Refresh research":readable(lead.status||"new"),statusTone(lead.status))}<span>Fit ${scored?score:"—"}</span></span></td><td><span>${escape(lead.contact_name || lead.contact_role || "Contact not found")}</span><span class="cell-line"><span class="email-indicator ${statusTone(lead.email_status)}" title="${escape(tips[lead.email_status]||tips.unknown)}"></span><span class="cell-sub" title="${escape(tips[lead.email_status]||tips.unknown)}">${escape(lead.email || lead.phone || "No contact details")}</span></span></td><td><span class="fit-score ${score>=80?"high":""}">${scored?score:"—"}<span class="fit-track" aria-hidden="true"><span style="width:${score}%"></span></span></span></td><td>${badge(lead.research_status==="complete"&&lead.research_current===false?"Refresh research":readable(lead.status||"new"),statusTone(lead.status))}</td><td class="chevron-cell" aria-hidden="true">↗</td></tr>`;
    }).join("")}</tbody></table></div><div class="table-bottom"><span>${leads.length} of ${leadList().length} companies</span></div>`;
  }
  function inputField(label, name, value, options = {}) {
    const {type="text",placeholder="",min,max,help="",required=false,step,rows} = options;
    const id = `field-${name.replace(/\./g,"-")}`;
    const attributes = `id="${id}" name="${name}"${required?" required":""}${placeholder?` placeholder="${escape(placeholder)}"`:""}${min!==undefined?` min="${min}"`:""}${max!==undefined?` max="${max}"`:""}${step!==undefined?` step="${step}"`:""}`;
    return `<div class="field"><label for="${id}">${label}</label>${type==="textarea"?`<textarea ${attributes}${rows?` rows="${rows}"`:""}>${escape(value)}</textarea>`:`<input type="${type}" ${attributes} value="${escape(value)}">`}${help?`<small>${help}</small>`:""}</div>`;
  }
  function campaignForm() {
    const c=campaign(), icp={...defaults.icp,...c.icp}, sender={...defaults.sender,...c.sender}, settings={...defaults.settings,...c.settings};
    return `<form id="campaign-form"><div class="setup-grid"><section class="panel"><div class="panel-header"><div><h2>Audience</h2></div></div><div class="panel-body">${inputField("Name","name",c.name,{required:true})}${inputField("Business type","icp.business_type",icp.business_type,{required:true,placeholder:"Machine shops serving aerospace",type:"textarea",rows:2})}<div class="field-row">${inputField("Location","icp.location",icp.location,{placeholder:"Los Angeles, CA"})}${inputField("Company size <span class=\"optional\">optional</span>","icp.size",icp.size,{placeholder:"10–75 employees"})}</div>${inputField("Contact roles","icp.roles",Array.isArray(icp.roles)?icp.roles.join(", "):icp.roles,{placeholder:"Owner, operations director"})}<details class="details-block"${icp.must_have||icp.exclude||icp.signals?" open":""}><summary>Qualifying signals & exclusions</summary>${inputField("Must have","icp.must_have",icp.must_have,{type:"textarea",placeholder:"CNC machining; local decision maker",rows:2})}${inputField("Exclude","icp.exclude",icp.exclude,{placeholder:"Staffing firms, large national chains"})}${inputField("Signals to look for","icp.signals",icp.signals,{type:"textarea",placeholder:"RFQ upload form; growing operations team",rows:2})}</details></div></section><section class="panel"><div class="panel-header"><div><h2>Context</h2></div></div><div class="panel-body"><div class="field"><label for="offer-text">Offer</label><textarea id="offer-text" name="offer" class="offer-input" placeholder="The problem you solve, the result you deliver, and any proof you can support.">${escape(c.offer||"")}</textarea></div><button class="document-drop" id="document-drop" type="button" data-action="upload-documents"><span class="upload-icon">${icon("upload")}</span><span><strong>Add supporting documents</strong><small>PDF, DOCX, Markdown, text, or CSV · up to 4 files</small></span></button><div class="documents-list">${(state.detail.documents||[]).map((doc)=>`<div class="document-item"><span aria-hidden="true">↳</span><strong>${escape(doc.name||doc.filename||"Document")}</strong><span>${doc.characters?`${Number(doc.characters).toLocaleString()} chars`:date(doc.created_at)||"Added"}</span>${doc.id?`<button class="document-remove" type="button" data-action="remove-document" data-id="${escape(doc.id)}" aria-label="Remove ${escape(doc.name||doc.filename||"document")}">×</button>`:""}</div>`).join("")}</div><details class="details-block"${sender.name?" open":""}><summary>Sender details</summary><div class="field-row">${inputField("Name","sender.name",sender.name,{placeholder:"Your name"})}${inputField("Business","sender.company",sender.company,{placeholder:"Your company"})}</div>${inputField("Reply email","sender.email",sender.email,{type:"email",placeholder:"you@yourcompany.com"})}${inputField("Business postal address","sender.postal_address",sender.postal_address,{placeholder:"For your email footer"})}</details></div></section><section class="panel span-all"><div class="panel-header"><div><h2>Research</h2></div></div><div class="panel-body"><div class="field-row four"><div class="field"><label for="provider-select">Agent</label><select id="provider-select" name="settings.provider">${providerOptions(settings.provider,true)}</select></div>${inputField("Lead target","settings.target_leads",settings.target_leads,{type:"number",min:1,max:1000,required:true})}${inputField("Minimum fit / 100","settings.min_fit",settings.min_fit,{type:"number",min:0,max:100,required:true})}${inputField("Agent call limit","settings.max_agent_calls",settings.max_agent_calls,{type:"number",min:1,max:5000,required:true})}</div><div id="provider-notice">${providerNotice(settings.provider)}</div><details class="details-block"><summary>Model, research & usage limits</summary><div class="field-row four">${inputField("Model <span class=\"optional\">optional</span>","settings.model",settings.model,{placeholder:"Use CLI default"})}${inputField("Search batches","settings.discovery_queries",settings.discovery_queries,{type:"number",min:1,max:40})}${inputField("Pages per company","settings.research_pages",settings.research_pages,{type:"number",min:1,max:10})}${inputField("Concurrent agents","settings.concurrency",settings.concurrency,{type:"number",min:1,max:4})}</div><div class="field-row four">${inputField("Job limit · minutes","settings.max_minutes",settings.max_minutes,{type:"number",min:1,max:1440})}${inputField("Call timeout · seconds","settings.agent_timeout_seconds",settings.agent_timeout_seconds,{type:"number",min:15,max:900})}${inputField("Claude turn limit","settings.max_turns",settings.max_turns,{type:"number",min:2,max:40})}${inputField("Claude budget · USD","settings.max_cost_usd",settings.max_cost_usd,{type:"number",min:0.1,max:1000,step:0.01})}</div><p>Calls and time are bounded. Provider billing and subscription limits still apply.</p></details></div></section></div><div class="save-bar"><span id="campaign-save-state"></span><button class="button primary" type="submit">Save</button></div></form>`;
  }
  function exportView() {
    const n=counts();
    if(isDemo())state.exportScope="all";
    const leads=leadList().filter((lead)=>state.exportScope==="all"||state.exportScope==="approved"&&lead.ready_to_send||state.exportScope==="callable"&&!isDemo()&&lead.phone&&lead.phone_source&&!["suppressed","rejected"].includes(lead.status)&&!lead.prior_activity);
    return `<div class="report-controls"><fieldset class="export-options"><legend class="sr-only">Export scope</legend>${[["all",`All companies (${n.total})`],["callable","Sourced phone list"],["approved",`Approved email (${isDemo()?0:n.ready})`]].map(([value,label])=>`<label class="export-choice"><input type="radio" name="export-scope" value="${value}"${state.exportScope===value?" checked":""}${isDemo()&&value!=="all"?" disabled":""}><span>${label}</span></label>`).join("")}</fieldset><div class="export-buttons"><button class="button primary" type="button" data-action="export" data-format="csv"${!leads.length?" disabled":""}>${icon("export")} Download CSV</button><button class="button" type="button" data-action="export" data-format="xlsx"${!leads.length?" disabled":""}>Excel</button></div></div>${isActive(state.job)?'<p class="notice neutral">Research is active. This export contains the results saved so far.</p>':""}${!leads.length?'<p class="notice neutral">No eligible records.</p>':`<div class="table-wrap"><table><thead><tr><th>COMPANY</th><th>EMAIL</th><th>PHONE</th><th>RESEARCH</th></tr></thead><tbody>${leads.slice(0,10).map((lead)=>`<tr><td>${escape(lead.company)}</td><td>${escape(lead.email||"—")}</td><td>${escape(lead.phone||"—")}</td><td>${lead.research_current?"Current":lead.research_status==="complete"?"Needs refresh":"Pending"}</td></tr>`).join("")}</tbody></table></div><p class="table-bottom">${leads.length} records${leads.length>10?" · first 10 shown":""}</p>`}<details class="details-block"><summary>Email handoff</summary><p>Approved email exports require reviewed drafts, recent mailbox verification, sender details, and no exclusions or prior activity.</p><div class="inline-actions"><button class="button small" type="button" data-action="view" data-view="review">Review drafts</button><button class="button small" type="button" data-action="import-verification">Import verification</button><button class="button small" type="button" data-action="import-outcomes">Import results</button></div></details>`;
  }

  function renderMain() {
    if (state.view === "suppressions") { renderSuppressions(); return; }
    if (!campaign()) { $("#main").innerHTML = renderEmpty(); return; }
    const staleNotice = counts().stale ? '<div class="notice neutral"><span>Brief changed. Run Research existing list to refresh saved findings.</span></div>' : "";
    $("#main").innerHTML = `${heading()}${navigation()}${staleNotice}${state.view==="campaign"?campaignForm():state.view==="export"?exportView():leadPanel()}`;
    if (["leads","review"].includes(state.view)) { renderMetrics(); renderLeadTable(); }
  }
  function selectJob() {
    const jobs = state.detail?.jobs || [];
    return jobs.find((job)=>["queued","running"].includes(job.status)) || jobs.find((job)=>["paused","budget_exhausted"].includes(job.status)) || jobs[0] || null;
  }
  function renderJob() {
    const job=state.job;
    if (!job || state.dismissedJobs.has(job.id)) { $("#job-region").innerHTML=""; return; }
    const active=isActive(job), status=readable(job.status), total=Math.max(0,Number(job.total)||0), completed=clamp(job.completed,0,total);
    if(job.status==="completed"&&!state.logOpen){
      $("#job-region").innerHTML="";
      $$('[data-action="run"]').forEach((button)=>button.disabled=false);
      return;
    }
    const progress=total ? Math.round(completed/total*100) : 0;
    const stages={report:"Lead list & research",pipeline:"Research & email drafts",discover:"Finding companies",research:"Researching companies",verify:"Checking email domains",draft:"Writing sequences"};
    const title=stages[job.current_stage] || stages[job.stage] || "Preparing leads";
    const canResume=["paused","budget_exhausted","failed"].includes(job.status);
    const controls=active?'<button class="button quiet small" type="button" data-action="job-pause">Pause</button><button class="button quiet small" type="button" data-action="job-cancel">Cancel</button>':canResume?'<button class="button small" type="button" data-action="job-resume">Resume</button><button class="button quiet small" type="button" data-action="job-cancel">Cancel</button>':'<button class="button quiet small" type="button" data-action="dismiss-job" aria-label="Dismiss completed job">×</button>';
    const cost=Number.isFinite(job.cost_usd)?`$${job.cost_usd.toFixed(2)}`:"—";
    $("#job-region").innerHTML=`<section class="job-panel${active?" is-running":""}" aria-label="Workflow progress"><div class="job-topline"><div class="job-heading"><span class="status-dot" aria-hidden="true"></span><strong>${escape(title)}</strong>${badge(status,statusTone(job.status))}${job.current_item?`<span class="job-detail-text" title="${escape(job.current_item)}">${escape(job.current_item)}</span>`:""}</div><div class="job-controls">${controls}</div></div><div class="job-progress-row"><progress${total?` value="${progress}"`:job.status==="completed"?' value="100"':""} max="100" aria-label="Current stage progress"></progress><span class="job-count">${total?`${completed} / ${total}`:active?"…":"—"}</span></div><div class="job-meta"><span>Calls<strong>${Number(job.agent_calls)||0} / ${Number(job.max_agent_calls)||campaign()?.settings?.max_agent_calls||"—"}</strong></span><span class="optional-meta">Tokens<strong>${job.input_tokens==null&&job.output_tokens==null?"—":((Number(job.input_tokens)||0)+(Number(job.output_tokens)||0)).toLocaleString()}</strong></span><span class="optional-meta">Reported cost<strong>${cost}</strong></span>${Number(job.cost_unreported)>0?`<span class="unreported-cost" title="Reserved against the Claude budget when a call ends without reporting cost. Actual spend is unknown.">Unreported allowance <strong>$${Number(job.cost_unreported).toFixed(2)}</strong> held</span>`:""}<button class="logs-toggle" type="button" data-action="job-logs" aria-expanded="${state.logOpen}">${state.logOpen?"Hide activity":"View activity"}</button></div>${job.error?`<div class="job-error">${escape(typeof job.error==="object"?job.error.message||JSON.stringify(job.error):job.error)}</div>`:""}${state.logOpen?`<div class="job-log" role="log" aria-label="Job activity">${state.events.length?state.events.slice(-80).map((event)=>`<div class="job-log-line ${escape(event.level||"")}"><time>${escape(event.at?new Date(event.at).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit",second:"2-digit"}):"")}</time><span>${escape(event.message)}</span></div>`).join(""):'<span class="text-small text-muted">Waiting for the first event.</span>'}</div>`:""}</section>`;
    $$('[data-action="run"]').forEach((button)=>button.disabled=active);
  }

  async function refreshCampaign({full=false}={}) {
    const id=state.activeId;
    if (!id) return;
    const detail=await api(`/api/campaigns/${encodeURIComponent(id)}`);
    if (id!==state.activeId) return;
    state.detail=detail;
    const index=state.campaigns.findIndex((c)=>c.id===id);
    if (index>=0) state.campaigns[index]=detail.campaign;
    const valid=new Set(leadList().map((l)=>l.id));
    state.selected=new Set([...state.selected].filter((item)=>valid.has(item)));
    const nextJob=selectJob();
    if (nextJob?.id!==state.job?.id) state.events=[];
    state.job=nextJob;
    renderSidebar();
    renderJob();
    if (full) renderMain();
    else if (["leads","review"].includes(state.view)) {renderMetrics();renderLeadTable();}
    else if (state.view==="export") renderMain();
    schedulePoll();
  }
  async function loadCampaign(id) {
    clearTimeout(state.pollTimer);
    state.activeId=id; state.search=""; state.status="all"; state.quality="all"; state.selected.clear();state.job=null;state.events=[];state.configDirty=false;
    try { localStorage.setItem("auto-outbound-campaign",id); } catch {}
    await refreshCampaign({full:true});
    if(isDemo()){state.exportScope="all";}
  }
  function schedulePoll() {
    clearTimeout(state.pollTimer);
    if (state.job && ["queued","running","paused"].includes(state.job.status)) state.pollTimer=setTimeout(poll, isActive(state.job)?1500:4000);
  }
  async function poll() {
    if (state.polling || !state.job) return;
    state.polling=true;
    const id=state.job.id;
    try {
      const result=await api(`/api/jobs/${encodeURIComponent(id)}`);
      if (id!==state.job?.id) return;
      state.job=result.job;state.events=result.events||[];
      await refreshCampaign();
      $("#connection-dot").classList.remove("offline");
      $("#connection-label").textContent="";
    } catch {
      $("#connection-dot").classList.add("offline");
      $("#connection-label").textContent="Reconnecting";
    } finally {state.polling=false;schedulePoll();}
  }
  async function startRun(button) {
    if (!campaign() || isActive(state.job)) return;
    const stage=$("#run-stage")?.value || (state.view==="review"?"draft":"report");
    if (state.configDirty && $("#campaign-form")) {
      const form=$("#campaign-form");
      if(!form.reportValidity())return;
      const saved=await saveCampaign(form);
      if(!saved)return;
    }
    const provider=state.session?.providers?.find((p)=>p.id===campaign().settings?.provider);
    if (!isDemo() && !provider?.available && stage!=="verify") {
      toast(`Set up ${campaign().settings?.provider==="claude"?"Claude Code":"Codex"} first.`,true);openHelp();return;
    }
    await busy("Starting workflow",async()=>{
      const result=await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/jobs`,{method:"POST",body:{stage}});
      state.job=result.job;state.events=[];
      state.dismissedJobs.delete(state.job.id);
      await refreshCampaign();renderJob();schedulePoll();toast("Research started.");
    },button);
  }
  async function jobAction(action,button) {
    if (!state.job) return;
    await busy(`${action==="resume"?"Resuming":action==="pause"?"Pausing":"Stopping"} workflow`,async()=>{
      const result=await api(`/api/jobs/${encodeURIComponent(state.job.id)}/${action}`,{method:"POST",body:{}});
      state.job=result.job;renderJob();schedulePoll();
      toast(action==="pause"?"Pause requested. Progress is saved.":action==="resume"?"Workflow resumed.":"Cancellation requested.");
    },button);
  }

  function openNewCampaign() {
    const available=state.session?.providers?.find((p)=>p.available && p.id!=="demo");
    const provider=available?.id || "codex";
    $("#campaign-dialog").innerHTML=`<div class="dialog-header"><div><h2 id="campaign-dialog-title">New list</h2></div><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close new campaign">${icon("close")}</button></div><form id="new-campaign-form"><div class="dialog-body">${inputField("Name","name","",{required:true,placeholder:"Southern California machine shops"})}<div class="field-row">${inputField("Business type","icp.business_type","",{required:true,placeholder:"Aerospace machine shops"})}${inputField("Location","icp.location","",{placeholder:"Los Angeles, CA"})}</div><div class="field-row"><div class="field"><label for="new-provider">Agent</label><select name="settings.provider" id="new-provider">${providerOptions(provider)}</select></div>${inputField("Lead target","settings.target_leads",25,{type:"number",min:1,max:1000,required:true})}</div><details class="details-block"><summary>Context & qualifications</summary>${inputField("Offer <span class=\"optional\">optional · for fit and email drafts</span>","offer","",{type:"textarea",placeholder:"What you do, who it helps, and the result you deliver.",rows:3})}${inputField("Contact roles","icp.roles","",{placeholder:"Owner, operations director"})}${inputField("Must have","icp.must_have","",{placeholder:"CNC machining; aerospace customers"})}${inputField("Exclude","icp.exclude","",{placeholder:"Staffing firms, large national chains"})}</details></div><div class="dialog-footer"><button class="button quiet" type="button" data-action="close-dialog">Cancel</button><button class="button primary" type="submit">Create list</button></div></form>`;
    const dialog=$("#campaign-dialog");
    $$("[id]",dialog).forEach((input)=>{
      if(!input.id.startsWith("field-"))return;
      const prior=input.id;input.id=`new-${prior}`;
      $$('label',dialog).filter((label)=>label.htmlFor===prior).forEach((label)=>label.htmlFor=input.id);
    });
    openDialog("#campaign-dialog");
  }
  function readCampaignForm(form,base={}) {
    const payload={name:base.name||"",icp:{...defaults.icp,...base.icp},sender:{...defaults.sender,...base.sender},settings:{...defaults.settings,...base.settings},offer:base.offer||"",demo:base.demo||false};
    for (const [key,value] of new FormData(form).entries()) {
      const [group,field]=key.split(".");
      if (field) {
        if (group==="settings" && typeof defaults.settings[field]==="number") payload.settings[field]=Number(value);
        else if (group==="icp" && field==="roles") payload.icp.roles=String(value).split(",").map((v)=>v.trim()).filter(Boolean);
        else if (payload[group]) payload[group][field]=String(value).trim();
      } else payload[key]=String(value).trim();
    }
    payload.demo=payload.settings.provider==="demo";
    return payload;
  }
  async function createCampaign(form,button) {
    await busy("Creating campaign",async()=>{
      const payload=readCampaignForm(form);
      const result=await api("/api/campaigns",{method:"POST",body:payload});
      const created=result.campaign||result;
      state.campaigns.unshift(created);state.view="leads";
      await loadCampaign(created.id);$("#campaign-dialog").close();toast("Lead list created.");
    },button);
  }
  async function saveCampaign(form,button) {
    return busy("Saving campaign",async()=>{
      const payload=readCampaignForm(form,campaign());
      await api(`/api/campaigns/${encodeURIComponent(state.activeId)}`,{method:"PATCH",body:payload});
      state.configDirty=false;
      await refreshCampaign({full:true});toast("Research brief saved.");
      return true;
    },button);
  }
  async function demo(button) {
    await busy("Opening demo",async()=>{
      const result=await api("/api/demo",{method:"POST",body:{}});
      const created=result.campaign||result;
      state.campaigns.unshift(created);state.view="leads";
      await loadCampaign(created.id);
      const run=await api(`/api/campaigns/${encodeURIComponent(created.id)}/jobs`,{method:"POST",body:{stage:"report"}});
      state.job=run.job;await refreshCampaign();renderJob();schedulePoll();
      toast("Demo started. All companies and contacts are fictional.");
    },button);
  }
  async function uploadDocuments(files) {
    if (!state.activeId || !files.length) return;
    if(files.length+(state.detail.documents?.length||0)>4){toast("Use up to four supporting documents per campaign.",true);return;}
    if (state.configDirty && $("#campaign-form")) {
      if(!$("#campaign-form").reportValidity())return;
      const saved=await saveCampaign($("#campaign-form"));
      if (!saved) return;
    }
    await busy("Reading documents",async()=>{
      let uploaded=0;
      for (const file of files) {
        const form=new FormData();form.append("file",file);
        await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/documents`,{method:"POST",body:form});uploaded++;
      }
      await refreshCampaign({full:true});toast(`${uploaded} document${uploaded===1?"":"s"} added.`);
    });
  }
  async function importLeads(file) {
    if (!file || !state.activeId) return;
    await busy("Importing leads",async()=>{
      const form=new FormData();form.append("file",file);
      const result=await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/import`,{method:"POST",body:form});
      state.view="leads";await refreshCampaign({full:true});
      toast(`${result.imported??0} leads imported${result.duplicates?`; ${result.duplicates} duplicates skipped`:""}${result.rejected?`; ${result.rejected} invalid rows`:""}.`,Boolean(result.rejected));
    });
  }
  function openImportMenu() {
    if(!state.activeId){toast("Create a campaign first.");return;}
    $("#small-dialog").innerHTML=`<div class="dialog-header"><h2 id="small-dialog-title">Import</h2><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close import menu">${icon("close")}</button></div><div class="dialog-body import-choices"><button class="import-choice" type="button" data-action="import-leads">${icon("list")}<span><strong>Lead list</strong><small>Companies and contacts · CSV or Excel</small></span><span aria-hidden="true">↗</span></button><button class="import-choice" type="button" data-action="import-verification">${icon("check")}<span><strong>Mailbox verification</strong><small>Results from your verifier · CSV</small></span><span aria-hidden="true">↗</span></button><button class="import-choice" type="button" data-action="import-outcomes">${icon("mail")}<span><strong>Sending results</strong><small>Replies, meetings, bounces, and opt-outs · CSV</small></span><span aria-hidden="true">↗</span></button></div>`;
    openDialog("#small-dialog");
  }
  function openVerificationImport() {
    if(!state.activeId)return;
    $("#small-dialog").innerHTML=`<div class="dialog-header"><h2 id="small-dialog-title">Mailbox verification</h2><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close verification import">${icon("close")}</button></div><form id="verification-form"><div class="dialog-body"><p>Match verified addresses to the leads in this campaign.</p><div class="field"><label for="verification-file">Results CSV</label><input type="file" id="verification-file" name="file" accept=".csv" required></div>${inputField("Verifier <span class=\"optional\">if absent from CSV</span>","source","",{placeholder:"Verification provider name"})}<details class="details-block"><summary>Column format</summary><pre class="help-command">email,status,verification_source,verification_at</pre><p>Use verified, valid, or deliverable for successful results. Keep catch_all, invalid, risky, and unknown distinct. Include the verification date.</p></details></div><div class="dialog-footer"><button class="button quiet" type="button" data-action="close-dialog">Cancel</button><button class="button primary" type="submit">Import results</button></div></form>`;
    openDialog("#small-dialog");
  }
  async function importVerification(form,button) {
    await busy("Importing verification",async()=>{
      const result=await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/verification/import`,{method:"POST",body:new FormData(form)});
      await refreshCampaign({full:true});$("#small-dialog").close();
      toast(`${result.updated??result.imported??"Verification"} results imported${result.skipped?`; ${result.skipped} skipped`:""}.`);
    },button);
  }
  function openOutcomeImport() {
    if(!state.activeId)return;
    $("#small-dialog").innerHTML=`<div class="dialog-header"><h2 id="small-dialog-title">Sending results</h2><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close results import">${icon("close")}</button></div><div class="dialog-body"><p>Record prior contact and stop repeat outreach.</p><pre class="help-command">email,outcome,at</pre><p>Outcomes: sent, replied, positive, booked, won, lost, bounced, unsubscribed, or not_interested.</p><p>Bounces and opt-outs are added to your exclusions.</p></div><div class="dialog-footer"><button class="button quiet" type="button" data-action="close-dialog">Cancel</button><button class="button primary" type="button" data-action="choose-outcomes">Choose CSV</button></div>`;
    openDialog("#small-dialog");
  }
  async function importOutcomes(file) {
    if(!file||!state.activeId)return;
    await busy("Importing results",async()=>{
      const form=new FormData();form.append("file",file);
      const result=await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/outcomes/import`,{method:"POST",body:form});
      await refreshCampaign({full:true});$("#small-dialog").close();
      toast(`${result.updated??result.imported??"Sending"} results imported${result.skipped?`; ${result.skipped} skipped`:""}.`);
    });
  }
  async function removeDocument(id,button) {
    if (state.configDirty && $("#campaign-form")) {
      const form=$("#campaign-form");
      if(!form.reportValidity())return;
      const saved=await saveCampaign(form);
      if(!saved)return;
    }
    await busy("Removing document",async()=>{
      await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/documents/${encodeURIComponent(id)}`,{method:"DELETE"});
      await refreshCampaign({full:true});toast("Document removed.");
    },button);
  }

  function sourceLink(url,label) {
    const href=safeURL(url);
    return href?`<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${escape(label||hostname(href))} ↗</a>`:"";
  }
  function evidenceMarkup(lead) {
    const evidence=lead.evidence||[];
    const urls=[...new Set([...(lead.source_urls||[]),lead.email_source,lead.phone_source,lead.contact_source].filter(Boolean))];
    return `${evidence.length?`<div class="evidence-list">${evidence.map((item)=>`<article class="evidence-card"><div class="source-title">${sourceLink(item.url,item.title||hostname(item.url))||'<span>Source unavailable</span>'}${item.field?badge(readable(item.field)):""}</div>${item.quote?`<blockquote>${escape(item.quote)}</blockquote>`:""}<div class="source-meta"><span>${escape(hostname(item.url))}</span>${item.checked_at?`<span>Checked ${escape(date(item.checked_at))}</span>`:""}</div></article>`).join("")}</div>`:'<div class="notice neutral">No evidence captured yet. Run research for this list.</div>'}${urls.length?`<div class="lead-section" style="margin-top:24px"><h3>Additional sources</h3><div class="source-link-list">${urls.map((url)=>sourceLink(url)).join("")}</div></div>`:""}`;
  }
  function sequencesMarkup(lead) {
    if (!lead.drafts?.length) return '<div class="empty-table"><div class="empty-icon">'+icon("mail")+'</div><h3>No sequence yet.</h3><p>Add an offer, then run research and drafting.</p></div>';
    return `${lead.drafts.map((draft,index)=>`<article class="email-card"><div class="email-card-heading"><div class="email-step"><span class="step-number">0${index+1}</span>${index===0?"First message":index===1?"Useful follow-up":"Final follow-up"}</div><label class="email-delay">Day <input type="number" min="0" max="60" data-draft-index="${index}" data-draft-field="delay_days" value="${Number(draft.delay_days)||0}" aria-label="Email ${index+1} day"></label></div><div class="email-edit-area"><div class="field"><div class="label-row"><label for="subject-${index}">Subject</label><span id="subject-count-${index}" class="word-counter${words(draft.subject)>5?" over":""}">${words(draft.subject)} / 5 words</span></div><input id="subject-${index}" data-draft-index="${index}" data-draft-field="subject" value="${escape(draft.subject)}" maxlength="200"></div><div class="field"><div class="label-row"><label for="body-${index}">Email body</label><span id="body-count-${index}" class="word-counter${words(draft.body)>(index===0?90:65)?" over":""}">${words(draft.body)} / ${index===0?90:65} words</span></div><textarea id="body-${index}" data-draft-index="${index}" data-draft-field="body" rows="9">${escape(draft.body)}</textarea></div>${draft.angle?`<div class="email-angle">${escape(draft.angle)}</div>`:""}</div>${draft.source_urls?.length?`<div class="email-card-sources"><span>Research</span>${draft.source_urls.map((url)=>sourceLink(url)).join("")}</div>`:""}</article>`).join("")}`;
  }
  function openLead(id,tab) {
    const lead=leadList().find((item)=>item.id===id);
    if (!lead) return;
    state.lead=lead;state.leadWorking=structuredClone(lead);state.leadDirty=false;state.leadTab=tab||(state.view==="review"?"sequence":"overview");
    renderLeadDialog();openDialog("#lead-dialog");
  }
  function renderLeadDialog() {
    const lead=state.leadWorking;
    if (!lead) return;
    const qa=lead.qa_issues||[];
    $("#lead-dialog").innerHTML=`<div class="dialog-header"><div><span class="eyebrow">${isDemo()?"Sample":""}</span><div class="lead-title-line"><h2 id="lead-dialog-title">${escape(lead.company)}</h2>${badge(readable(lead.status),statusTone(lead.status))}</div><div class="lead-subtitle">${sourceLink(lead.website)}${lead.location?`<span>${escape(lead.location)}</span>`:""}<span>Fit <strong class="mono">${lead.research_status==="complete"||lead.fit_score?clamp(lead.fit_score,0,100):"—"}</strong></span></div></div><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close lead review">${icon("close")}</button></div><div class="lead-dialog-nav" role="tablist" aria-label="Lead details">${[["overview","Overview"],["sequence","Sequence"],["evidence","Evidence"]].map(([key,label])=>`<button type="button" role="tab" id="tab-${key}" aria-controls="panel-${key}" aria-selected="${state.leadTab===key}" class="${state.leadTab===key?"active":""}" data-action="lead-tab" data-tab="${key}">${label}${key==="evidence"&&lead.evidence?.length?` <span class="text-muted">${lead.evidence.length}</span>`:""}</button>`).join("")}</div><div class="dialog-body"><section class="lead-tab-panel" role="tabpanel" id="panel-overview" aria-labelledby="tab-overview"${state.leadTab!=="overview"?" hidden":""}>${lead.description?`<div class="lead-section"><p class="description">${escape(lead.description)}</p></div>`:""}<div class="lead-section"><h3>Contact</h3><div class="contact-state">${badge(labels[lead.email_status]||"Not verified",statusTone(lead.email_status),tips[lead.email_status]||tips.unknown)}<span>${escape(tips[lead.email_status]||tips.unknown)}</span></div><div class="field-row">${inputField("Name","lead.contact_name",lead.contact_name)}${inputField("Role","lead.contact_role",lead.contact_role)}</div><div class="field-row">${inputField("Email","lead.email",lead.email,{type:"email"})}${inputField("Phone","lead.phone",lead.phone,{type:"tel"})}</div>${lead.email_source||lead.phone_source||lead.linkedin_url?`<div class="source-link-list">${lead.email_source?sourceLink(lead.email_source,"Email source"):""}${lead.phone_source?sourceLink(lead.phone_source,"Phone source"):""}${lead.linkedin_url?sourceLink(lead.linkedin_url,"LinkedIn"):""}</div>`:""}<details class="details-block"><summary>Contact sources & mailbox verification</summary>${inputField("Email source URL","lead.email_source",lead.email_source,{type:"url",placeholder:"https://company.com/contact"})}<div class="field-row">${inputField("Phone source URL","lead.phone_source",lead.phone_source,{type:"url"})}${inputField("Name / role source URL","lead.contact_source",lead.contact_source,{type:"url"})}</div><div class="field-row"><div class="field"><label for="lead-email-status">Contact status</label><select id="lead-email-status" name="lead.email_status">${Object.entries(labels).map(([value,label])=>`<option value="${value}"${(lead.email_status||"unknown")===value?" selected":""}>${label}</option>`).join("")}</select></div>${inputField("Verification source","lead.verification_source",lead.verification_source,{placeholder:"Provider or direct confirmation"})}</div><p>A public address or an MX record does not prove mailbox delivery.</p></details></div><div class="lead-section two-col-info"><div><h3>Why it fits</h3>${lead.fit_reasons?.length?`<ul class="reason-list">${lead.fit_reasons.map((reason)=>`<li>${escape(reason)}</li>`).join("")}</ul>`:'<p class="text-small text-muted">Not researched yet.</p>'}</div><div><h3>Open questions</h3>${lead.risks?.length?`<ul class="reason-list risks">${lead.risks.map((reason)=>`<li>${escape(reason)}</li>`).join("")}</ul>`:'<p class="text-small text-muted">No research gaps recorded.</p>'}</div></div>${lead.pain_hypothesis?`<div class="lead-section fact-card"><span class="eyebrow">WORKING HYPOTHESIS</span><p>${escape(lead.pain_hypothesis)}</p></div>`:""}${lead.personalization?`<div class="lead-section fact-card"><span class="eyebrow">RELEVANT OBSERVATION</span><p>${escape(lead.personalization)}</p></div>`:""}<div class="lead-section">${inputField("Review notes","lead.review_notes",lead.review_notes,{type:"textarea",rows:3,placeholder:"What should you check or change?"})}</div></section><section class="lead-tab-panel" role="tabpanel" id="panel-sequence" aria-labelledby="tab-sequence"${state.leadTab!=="sequence"?" hidden":""}>${lead.export_blockers?.length?`<details class="details-block export-blockers"><summary>${lead.export_blockers.length} export checks remaining</summary><ul class="qa-list">${lead.export_blockers.map((item)=>`<li>${escape(item)}</li>`).join("")}</ul></details>`:""}${qa.length?`<div class="notice qa-notice"><strong>Review before approval</strong><ul class="qa-list">${qa.map((item)=>`<li>${escape(typeof item==="string"?item:item.message||item.code||JSON.stringify(item))}</li>`).join("")}</ul></div>`:""}${sequencesMarkup(lead)}</section><section class="lead-tab-panel" role="tabpanel" id="panel-evidence" aria-labelledby="tab-evidence"${state.leadTab!=="evidence"?" hidden":""}>${evidenceMarkup(lead)}</section></div><div class="dialog-footer lead-footer"><span id="lead-save-state" class="lead-save-indicator">${state.leadDirty?"Unsaved changes":""}</span><button type="button" class="button quiet danger" data-action="lead-suppress">Do not contact</button><button type="button" class="button quiet" data-action="lead-reject">Reject</button><button type="button" class="button" data-action="lead-save">Save edits</button><button type="button" class="button primary" data-action="lead-approve"${!lead.drafts?.length||lead.status==="suppressed"?" disabled":""}>${icon("check")} Approve</button></div>`;
  }
  function changeLeadTab(tab) {
    state.leadTab=tab;
    $$("[data-action='lead-tab']").forEach((button)=>{button.classList.toggle("active",button.dataset.tab===tab);button.setAttribute("aria-selected",String(button.dataset.tab===tab));});
    $$(".lead-tab-panel").forEach((panel)=>panel.hidden=panel.id!==`panel-${tab}`);
  }
  function markLeadDirty() {
    state.leadDirty=true;
    if ($("#lead-save-state")) $("#lead-save-state").textContent="Unsaved changes";
  }
  async function saveLead(status,button) {
    const lead=state.leadWorking;
    if (!lead) return;
    const invalid=$$("input,textarea,select",$("#lead-dialog")).find((field)=>!field.checkValidity());
    if(invalid){
      const panel=invalid.closest(".lead-tab-panel");
      if(panel)changeLeadTab(panel.id.replace("panel-",""));
      const detail=invalid.closest("details");if(detail)detail.open=true;
      invalid.reportValidity();return;
    }
    if (lead.email_status==="verified" && !lead.verification_source?.trim()) {toast("Add the verification source before marking a mailbox verified.",true);changeLeadTab("overview");return;}
    return busy(status==="approved"?"Approving lead":"Saving lead",async()=>{
      const body={};
      for (const key of ["contact_name","contact_role","email","phone","email_status","email_source","phone_source","contact_source","verification_source","review_notes","drafts"]) body[key]=lead[key]??(key==="drafts"?[]:"");
      if (lead.email_status==="verified" && (state.lead.email_status!=="verified" || state.lead.verification_source!==lead.verification_source)) body.verification_at=new Date().toISOString();
      if (status) body.status=status;
      await api(`/api/leads/${encodeURIComponent(lead.id)}`,{method:"PATCH",body});
      state.leadDirty=false;
      await refreshCampaign();
      state.lead=leadList().find((item)=>item.id===lead.id)||lead;state.leadWorking=structuredClone(state.lead);
      renderLeadDialog();toast(status==="approved"?"Lead approved.":status==="rejected"?"Lead rejected.":"Edits saved.");
      return true;
    },button);
  }
  async function bulkStatus(status,button) {
    await busy(status==="approved"?"Approving selection":"Rejecting selection",async()=>{
      let completed=0,failed=0;let lastError="";
      for (const id of state.selected) {
        try {await api(`/api/leads/${encodeURIComponent(id)}`,{method:"PATCH",body:{status}});completed++;}
        catch(error){failed++;lastError=error.message;}
      }
      state.selected.clear();await refreshCampaign();
      toast(`${completed} ${status}${failed?`; ${failed} need attention. ${lastError}`:"."}`,failed>0);
    },button);
  }
  function openLeadSuppression() {
    const lead=state.leadWorking;
    const domain=lead.domain||hostname(lead.website);
    $("#small-dialog").innerHTML=`<div class="dialog-header"><h2 id="small-dialog-title">Do not contact</h2><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close suppression form">${icon("close")}</button></div><form id="lead-suppression-form"><div class="dialog-body"><p>Exclude this contact from future approved exports.</p><div class="field"><label for="suppression-target">Applies to</label><select id="suppression-target" name="target">${lead.email?`<option value="email">This email · ${escape(lead.email)}</option>`:""}${domain?`<option value="domain">Entire company · ${escape(domain)}</option>`:""}</select></div>${inputField("Reason","reason","",{placeholder:"Opted out, existing client, or not a fit"})}</div><div class="dialog-footer"><button class="button quiet" type="button" data-action="close-dialog">Cancel</button><button class="button primary" type="submit"${!lead.email&&!domain?" disabled":""}>Add exclusion</button></div></form>`;
    openDialog("#small-dialog");
  }

  async function download(format,button,scope=state.exportScope) {
    const n=counts();
    if (scope==="approved" && (isDemo()||!n.ready)) {toast(isDemo()?"Use the full research list for demo exports.":"No leads are ready to send. Review their export checks, or choose the full research list.",true);return;}
    await busy("Preparing export",async()=>{
      const response=await api(`/api/campaigns/${encodeURIComponent(state.activeId)}/export?format=${encodeURIComponent(format)}&scope=${encodeURIComponent(scope)}`,{download:true});
      const blob=await response.blob();
      const url=URL.createObjectURL(blob);
      const link=document.createElement("a");link.href=url;
      const disposition=response.headers.get("Content-Disposition")||"";
      const match=disposition.match(/filename="?([^";]+)"?/i);
      link.download=match?.[1]||`${campaign().name.replace(/[^a-zA-Z0-9_-]/g,"-")}-${scope}.${format}`;
      document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
      toast(`${format.toUpperCase()} export ready.`);
    },button);
  }
  async function showSuppressions() {
    await busy("Loading exclusions",async()=>{
      const result=await api("/api/suppressions");state.suppressions=result.suppressions||[];state.view="suppressions";renderMain();
    });
  }
  function renderSuppressions() {
    $("#main").innerHTML=`<div class="page-heading"><div><span class="eyebrow">ACROSS ALL CAMPAIGNS</span><h1>Do not contact.</h1><p>Exclude opted-out contacts, clients, and competitors.</p></div><div class="heading-actions"><button class="button" data-action="import-suppressions" type="button">${icon("upload")} Import CSV</button></div></div><section class="panel" style="margin-bottom:22px"><div class="panel-body"><form id="suppression-form" class="suppression-form"><div class="field"><label for="suppression-kind">Type</label><select id="suppression-kind" name="kind"><option value="email">Email</option><option value="domain">Domain</option></select></div>${inputField("Email or domain","value","",{required:true,placeholder:"example.com"})}${inputField("Reason","reason","",{placeholder:"Existing client"})}<button class="button primary" type="submit">Add exclusion</button></form><p class="suppression-note">Matches are blocked from approved exports.</p></div></section><section class="table-panel"><div class="table-wrap"><table><thead><tr><th>CONTACT / DOMAIN</th><th>REASON</th><th>ADDED</th></tr></thead><tbody>${state.suppressions.map((item)=>`<tr><td><span>${escape(item.value)}</span><span class="cell-sub">${escape(item.kind||item.type)}</span></td><td>${escape(item.reason||"—")}</td><td class="text-muted">${escape(date(item.created_at)||"—")}</td></tr>`).join("")}</tbody></table></div>${!state.suppressions.length?'<div class="empty-table"><h3>No exclusions yet.</h3><p>Add individual addresses or company domains.</p></div>':`<div class="table-bottom"><span>${state.suppressions.length} exclusions</span></div>`}</section>`;
  }
  async function addSuppression(form,button,fromLead=false) {
    await busy("Saving exclusion",async()=>{
      const values=Object.fromEntries(new FormData(form));
      let body=values;
      if(fromLead){const lead=state.leadWorking;body={kind:values.target,value:values.target==="email"?lead.email:lead.domain||hostname(lead.website),reason:values.reason};}
      await api("/api/suppressions",{method:"POST",body});
      if(fromLead){$("#small-dialog").close();$("#lead-dialog").close();await refreshCampaign();}
      else {const result=await api("/api/suppressions");state.suppressions=result.suppressions||[];renderSuppressions();}
      toast("Exclusion saved.");
    },button);
  }
  async function importSuppressions(file) {
    if(!file)return;
    await busy("Importing exclusions",async()=>{
      const form=new FormData();form.append("file",file);
      const imported=await api("/api/suppressions/import",{method:"POST",body:form});
      const result=await api("/api/suppressions");state.suppressions=result.suppressions||[];renderSuppressions();toast(`${imported.imported??0} exclusions imported${imported.skipped?`; ${imported.skipped} skipped`:""}.`);
    });
  }
  function openHelp() {
    $("#small-dialog").innerHTML=`<div class="dialog-header"><div><span class="eyebrow">LOCAL WORKSPACE</span><h2 id="small-dialog-title">Help</h2></div><button class="icon-button" type="button" data-action="close-dialog" aria-label="Close setup help">${icon("close")}</button></div><div class="dialog-body"><div class="help-heading">Your agents</div>${["codex","claude"].map((id)=>{const p=state.session?.providers?.find((item)=>item.id===id);return `<div class="provider-card"><div><strong>${id==="codex"?"Codex":"Claude Code"}</strong><small>${escape(p?.message||p?.version||(p?.available?"Installed. Sign in through your CLI.":"Install the CLI, then sign in."))}</small></div>${badge(p?.available?"Detected":"Not found",p?.available?"good":"")}</div>`;}).join("")}<p>Agent runs use your existing CLI account. Research and uploaded offers are sent to the selected AI provider.</p><div class="inline-actions" style="margin-bottom:23px"><a class="button small" href="https://developers.openai.com/codex/cli" target="_blank" rel="noopener noreferrer">Codex setup ↗</a><a class="button small" href="https://code.claude.com/docs/en/quickstart" target="_blank" rel="noopener noreferrer">Claude setup ↗</a><button class="button small quiet" type="button" data-action="refresh-session">Check again</button></div><details class="details-block"><summary>Open the app</summary><p>Double-click Open Auto Outbound.command on macOS or Open Auto Outbound.cmd on Windows. On Linux, run Open Auto Outbound.sh. Keep the launcher beside the app folder.</p><p>The first launch downloads the managed runtime. Use Quit app to stop the server. Open the launcher again to return to saved lists.</p></details><details class="details-block"><summary>Command line</summary><pre class="help-command">python app/run.py --help</pre><p>Use the same campaigns, jobs, and exports from your terminal. See the repository guide for setup and commands.</p><a class="button small" href="https://github.com/goosnav/auto-outbound" target="_blank" rel="noopener noreferrer">Open guide ↗</a></details><details class="details-block"><summary>Import format</summary><p>CSV or Excel. Use a header row. Start with company and website; add contact details when you have them.</p><pre class="help-command">company,website,location,contact_name,contact_role,email,phone</pre><p>Exclusion CSV: kind,value,reason.</p></details><details class="details-block"><summary>Local data & version</summary><div class="help-data-path">${escape(state.session?.data_directory||"Application data directory")}</div><p>Version ${escape(state.session?.version||"0.1.0")}. No emails are sent by this app.</p></details><div class="inline-actions" style="margin-top:23px"><button type="button" class="button small quiet" data-action="suppressions">Do not contact list</button><button type="button" class="button small quiet danger" data-action="quit">Quit app</button></div></div>`;
    openDialog("#small-dialog");
  }
  function confirmQuit() {
    $("#small-dialog").innerHTML=`<div class="dialog-header"><h2 id="small-dialog-title">Close Auto Outbound?</h2><button class="icon-button" type="button" data-action="close-dialog" aria-label="Cancel quit">${icon("close")}</button></div><div class="dialog-body"><p>Your saved campaigns stay on this computer. Active work will stop.</p></div><div class="dialog-footer"><button class="button quiet" type="button" data-action="close-dialog">Keep working</button><button class="button primary" type="button" data-action="confirm-quit">Quit app</button></div>`;
    openDialog("#small-dialog");
  }
  async function quit(button) {
    await busy("Closing app",async()=>{
      await api("/api/quit",{method:"POST",body:{}});clearTimeout(state.pollTimer);state.job=null;
      $$("dialog[open]").forEach((d)=>d.close());$("#job-region").innerHTML="";
      $("#main").innerHTML='<div class="error-view"><h2>Workspace closed.</h2><p>Your work is saved. You can close this tab.</p></div>';
      $("#connection-dot").classList.add("offline");$("#connection-label").textContent="App closed";
    },button);
  }
  async function initialize() {
    try {
      state.session=await api("/api/session");
      const list=await api("/api/campaigns");state.campaigns=list.campaigns||[];
      let saved;try {saved=localStorage.getItem("auto-outbound-campaign");} catch {}
      const selected=state.campaigns.find((c)=>c.id===saved)||state.campaigns[0];
      if (selected) await loadCampaign(selected.id);
      else {renderSidebar();renderMain();}
    } catch(error) {
      $("#connection-dot").classList.add("offline");$("#connection-label").textContent="Disconnected";
      $("#main").innerHTML=`<div class="error-view"><h2>Could not open the workspace.</h2><p>${escape(error.message)}</p><button class="button primary" type="button" data-action="retry">Try again</button></div>`;
    }
  }

  document.addEventListener("click",async(event)=>{
    const button=event.target.closest("[data-action]");
    if(button){
      const action=button.dataset.action;
      if(button.disabled)return;
      if(button.tagName==="A")event.preventDefault();
      switch(action){
        case "home":
          if(state.configDirty&&!window.confirm("Leave without saving campaign changes?"))return;
          state.configDirty=false;state.view="leads";renderMain();break;
        case "new": openNewCampaign();break;
        case "close-dialog": closeDialog(button);break;
        case "demo": await demo(button);break;
        case "select-campaign":
          if(state.configDirty && !window.confirm("Leave without saving campaign changes?"))return;
          state.view="leads";await busy("Opening campaign",()=>loadCampaign(button.dataset.id),button);break;
        case "view":
          if(state.configDirty && state.view==="campaign" && !window.confirm("Leave without saving campaign changes?"))return;
          state.configDirty=false;state.view=button.dataset.view;state.selected.clear();state.status="all";state.search="";state.quality="all";renderMain();break;
        case "run": await startRun(button);break;
        case "refresh": await busy("Refreshing",()=>refreshCampaign(),button);break;
        case "import":openImportMenu();break;
        case "import-leads":$("#small-dialog").close();$("#import-input").click();break;
        case "import-verification":openVerificationImport();break;
        case "import-outcomes":openOutcomeImport();break;
        case "choose-outcomes":$("#outcomes-input").click();break;
        case "remove-document":await removeDocument(button.dataset.id,button);break;
        case "upload-documents": $("#documents-input").click();break;
        case "job-pause": await jobAction("pause",button);break;
        case "job-resume": await jobAction("resume",button);break;
        case "job-cancel": await jobAction("cancel",button);break;
        case "job-logs": state.logOpen=!state.logOpen;if(state.logOpen&&state.job){try{const r=await api(`/api/jobs/${encodeURIComponent(state.job.id)}`);state.events=r.events||[];}catch{}}renderJob();break;
        case "dismiss-job": state.dismissedJobs.add(state.job.id);renderJob();break;
        case "reset-filters": state.search="";state.status="all";state.quality="all";renderMain();break;
        case "open-lead": openLead(button.dataset.id);break;
        case "lead-tab": changeLeadTab(button.dataset.tab);break;
        case "lead-save": await saveLead(undefined,button);break;
        case "lead-approve": await saveLead("approved",button);break;
        case "lead-reject": await saveLead("rejected",button);break;
        case "lead-suppress": openLeadSuppression();break;
        case "bulk-approve": await bulkStatus("approved",button);break;
        case "bulk-reject": await bulkStatus("rejected",button);break;
        case "clear-selection":state.selected.clear();renderLeadTable();break;
        case "export": await download(button.dataset.format,button,button.dataset.scope||state.exportScope);break;
        case "suppressions":
          if(state.configDirty&&!window.confirm("Leave without saving campaign changes?"))return;
          state.configDirty=false;$$("dialog[open]").forEach((d)=>d.close());await showSuppressions();break;
        case "import-suppressions":$("#suppression-input").click();break;
        case "help":openHelp();break;
        case "refresh-session":await busy("Checking agents",async()=>{state.session=await api("/api/session");openHelp();},button);break;
        case "quit":confirmQuit();break;
        case "confirm-quit":await quit(button);break;
        case "retry":await initialize();break;
      }
      return;
    }
    const row=event.target.closest("tr[data-lead-id]");
    if(row&&!event.target.closest("input,select,a,button"))openLead(row.dataset.leadId);
  });
  document.addEventListener("submit",async(event)=>{
    const form=event.target;
    if(!["new-campaign-form","campaign-form","suppression-form","lead-suppression-form","verification-form"].includes(form.id))return;
    event.preventDefault();
    const button=event.submitter;
    if(form.id==="new-campaign-form")await createCampaign(form,button);
    if(form.id==="campaign-form")await saveCampaign(form,button);
    if(form.id==="suppression-form")await addSuppression(form,button);
    if(form.id==="lead-suppression-form")await addSuppression(form,button,true);
    if(form.id==="verification-form")await importVerification(form,button);
  });
  document.addEventListener("input",(event)=>{
    const target=event.target;
    if(target.id==="lead-search"){state.search=target.value;renderLeadTable();}
    if(target.closest("#campaign-form")){state.configDirty=true;if($("#campaign-save-state"))$("#campaign-save-state").textContent="Unsaved changes";}
    if(target.dataset.draftField&&state.leadWorking){
      const index=Number(target.dataset.draftIndex),field=target.dataset.draftField;
      state.leadWorking.drafts[index][field]=field==="delay_days"?Number(target.value):target.value;markLeadDirty();
      if(field==="subject"||field==="body"){
        const count=words(target.value),counter=$(`#${field}-count-${index}`);
        const limit=field==="subject"?5:index===0?90:65;
        counter.textContent=`${count} / ${limit} words`;counter.classList.toggle("over",count>limit);
      }
    }
    if(target.name?.startsWith("lead.")&&state.leadWorking){
      state.leadWorking[target.name.slice(5)]=target.value;
      if(target.name==="lead.email"&&target.value!==state.lead.email){
        state.leadWorking.email_status="unknown";state.leadWorking.verification_source="";state.leadWorking.verification_at="";
        if($("#lead-email-status"))$("#lead-email-status").value="unknown";
        if($('[name="lead.verification_source"]'))$('[name="lead.verification_source"]').value="";
      }
      markLeadDirty();
    }
  });
  document.addEventListener("change",async(event)=>{
    const target=event.target;
    if(target.id==="status-filter"){state.status=target.value;state.selected.clear();renderLeadTable();}
    if(target.id==="quality-filter"){state.quality=target.value;state.selected.clear();renderLeadTable();}
    if(target.id==="provider-select")$("#provider-notice").innerHTML=providerNotice(target.value);
    if(target.dataset.selectLead){const id=target.dataset.selectLead;target.checked?state.selected.add(id):state.selected.delete(id);renderLeadTable();$$("[data-select-lead]").find((field)=>field.dataset.selectLead===id)?.focus();}
    if(target.id==="select-all"){filteredLeads().forEach((lead)=>target.checked?state.selected.add(lead.id):state.selected.delete(lead.id));renderLeadTable();$("#select-all")?.focus();}
    if(target.name==="export-scope"){state.exportScope=target.value;renderMain();$('[name="export-scope"]:checked')?.focus();}
    if(target.id==="import-input"){const file=target.files[0];target.value="";await importLeads(file);}
    if(target.id==="documents-input"){const files=[...target.files];target.value="";await uploadDocuments(files);}
    if(target.id==="suppression-input"){const file=target.files[0];target.value="";await importSuppressions(file);}
    if(target.id==="outcomes-input"){const file=target.files[0];target.value="";await importOutcomes(file);}
  });
  document.addEventListener("dragover",(event)=>{const drop=event.target.closest("#document-drop");if(drop){event.preventDefault();drop.classList.add("dragover");}});
  document.addEventListener("dragleave",(event)=>{event.target.closest("#document-drop")?.classList.remove("dragover");});
  document.addEventListener("drop",async(event)=>{const drop=event.target.closest("#document-drop");if(drop){event.preventDefault();drop.classList.remove("dragover");await uploadDocuments([...event.dataTransfer.files]);}});
  $("#lead-dialog").addEventListener("cancel",(event)=>{if(state.leadDirty&&!window.confirm("Close without saving edits?"))event.preventDefault();});
  window.addEventListener("beforeunload",(event)=>{if(state.configDirty||state.leadDirty&&$("#lead-dialog").open){event.preventDefault();event.returnValue="";}});
  document.addEventListener("keydown",(event)=>{
    const tab=event.target.closest?.('[role="tab"]');
    if(tab&&["ArrowLeft","ArrowRight","Home","End"].includes(event.key)){
      const tabs=$$('[role="tab"]',tab.closest('[role="tablist"]'));
      const next=event.key==="Home"?0:event.key==="End"?tabs.length-1:(tabs.indexOf(tab)+(event.key==="ArrowRight"?1:-1)+tabs.length)%tabs.length;
      event.preventDefault();changeLeadTab(tabs[next].dataset.tab);tabs[next].focus();
    }
    if((event.ctrlKey||event.metaKey)&&event.key==="s"){
      const form=$("#campaign-form");
      if($("#lead-dialog").open){event.preventDefault();void saveLead();}
      else if(form){event.preventDefault();form.requestSubmit();}
    }
  });
  initialize();
})();

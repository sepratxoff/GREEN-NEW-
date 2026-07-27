import io
import os
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from flask import Flask, jsonify, render_template_string, request, send_file
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
APP_NAME = "GreenVentures"
APP_VERSION = "3.1.0-green-ui"

# Official FMCSA / DOT sources used by the n8n workflow.
CENSUS_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
CURRENT_INSURANCE_URL = "https://data.transportation.gov/resource/c5y8-a4uz.json"
PREVIOUS_INSURANCE_URL = "https://data.transportation.gov/resource/3uet-3z4i.json"
NEW_ENTRANT_OOS_URL = "https://data.transportation.gov/resource/p2mt-9ige.json"

EASTERN = ZoneInfo("America/New_York")
DEFAULT_DAYS = 3
MAX_DAYS = 30
PAGE_SIZE = 1000
LOOKUP_BATCH_SIZE = 75
REQUEST_TIMEOUT = 120

CACHE_LOCK = threading.Lock()
LATEST_RESULT = None


def build_session():
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        status=5,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "GreenVentures-FMCSA/2.0"})

    # A Socrata app token is optional but recommended for higher rate limits.
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    if token:
        session.headers.update({"X-App-Token": token})
    return session


HTTP = build_session()


def clean(value):
    if value is None:
        return ""
    return str(value).strip()


def clean_usdot(value):
    value = clean(value)
    return value[:-2] if value.endswith(".0") else value


def safe_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def get_date_window(days):
    """Return the previous N Eastern calendar dates, excluding today."""
    days = max(1, min(int(days), MAX_DAYS))
    today = datetime.now(EASTERN).date()
    start = today - timedelta(days=days)
    return start.strftime("%Y%m%d"), today.strftime("%Y%m%d")


def socrata_get(url, params):
    response = HTTP.get(url, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError(f"Unexpected response from {url}: expected a JSON array")
    return payload


def fetch_all_pages(url, params):
    rows = []
    offset = 0
    while True:
        page_params = dict(params)
        page_params["$limit"] = PAGE_SIZE
        page_params["$offset"] = offset
        batch = socrata_get(url, page_params)
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            return rows
        offset += PAGE_SIZE


def fetch_strict_new_dot_candidates(days):
    start_date, today_date = get_date_window(days)

    # These server-side conditions match the strict n8n New DOT filter.
    where = (
        f"add_date >= '{start_date}' AND add_date < '{today_date}' "
        "AND status_code = 'A' "
        "AND classdef = 'AUTHORIZED FOR HIRE' "
        "AND power_units IS NOT NULL AND power_units != '0'"
    )

    params = {
        "$select": (
            "add_date,status_code,dot_number,phone,cell_phone,power_units,"
            "truck_units,classdef,legal_name,dba_name,phy_street,phy_city,"
            "phy_state,phy_zip,phy_country,email_address"
        ),
        "$where": where,
        "$order": "add_date DESC,dot_number ASC",
    }
    rows = fetch_all_pages(CENSUS_URL, params)

    candidates = []
    seen = set()
    for row in rows:
        usdot = clean_usdot(row.get("dot_number"))
        add_date = clean(row.get("add_date"))
        power_units = safe_int(row.get("power_units"), 0)

        # Local validation protects against unexpected source changes.
        if not usdot or usdot in seen:
            continue
        if not (start_date <= add_date < today_date):
            continue
        if clean(row.get("status_code")).upper() != "A":
            continue
        if clean(row.get("classdef")).upper() != "AUTHORIZED FOR HIRE":
            continue
        if power_units <= 0:
            continue

        seen.add(usdot)
        candidates.append(
            {
                "usdot_number": usdot,
                "docket_number": "",
                "legal_name": clean(row.get("legal_name")),
                "dba_name": clean(row.get("dba_name")),
                "add_date": add_date,
                "status_change_date": "",
                "op_auth_status": "Active",
                "reason": "New DOT / Initial Authority",
                "op_auth_type": "Motor Carrier of Property",
                "phone": clean(row.get("phone")) or clean(row.get("cell_phone")),
                "email_address": clean(row.get("email_address")),
                "phy_street": clean(row.get("phy_street")),
                "phy_city": clean(row.get("phy_city")),
                "phy_state": clean(row.get("phy_state")),
                "phy_zip": clean(row.get("phy_zip")),
                "phy_country": clean(row.get("phy_country")),
                "power_units": power_units,
                "classdef": "AUTHORIZED FOR HIRE",
                "source_dataset": "COMPANY_CENSUS",
            }
        )

    candidates.sort(key=lambda x: (x["add_date"], x["usdot_number"]), reverse=True)
    return candidates, start_date, today_date


def chunks(values, size):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def usdot_where(field_name, usdots):
    safe_ids = [value for value in usdots if value.isdigit()]
    quoted = ",".join(f"'{value}'" for value in safe_ids)
    return f"{field_name} in ({quoted})"


def fetch_insurance_matches(candidates):
    """Return current/pending and previous insurance rows grouped by USDOT."""
    current = {}
    previous = {}
    usdots = [item["usdot_number"] for item in candidates]

    for batch in chunks(usdots, LOOKUP_BATCH_SIZE):
        where = usdot_where("usdot_number", batch)

        current_rows = socrata_get(
            CURRENT_INSURANCE_URL,
            {
                "$select": (
                    "usdot_number,ins_form_code,ins_type_code,ins_class_code,"
                    "effective_date,trans_date,policy_no,insurance_company_name"
                ),
                "$where": where,
                "$limit": 5000,
            },
        )
        for row in current_rows:
            current.setdefault(clean_usdot(row.get("usdot_number")), []).append(row)

        previous_rows = socrata_get(
            PREVIOUS_INSURANCE_URL,
            {
                "$select": (
                    "usdot_number,ins_form_code,filing_status_reason,effective_date,"
                    "cancl_effective_date,policy_no,insurance_company_name"
                ),
                "$where": where,
                "$limit": 5000,
            },
        )
        for row in previous_rows:
            previous.setdefault(clean_usdot(row.get("usdot_number")), []).append(row)

    return current, previous


def remove_all_known_insurance(candidates, current, previous, checked_at):
    """Strict rule: any FMCSA insurance form, current/pending or previous, excludes."""
    passing = []
    excluded = []

    for carrier in candidates:
        usdot = carrier["usdot_number"]
        current_rows = current.get(usdot, [])
        previous_rows = previous.get(usdot, [])
        all_rows = current_rows + previous_rows
        form_codes = sorted(
            {
                clean(row.get("ins_form_code"))
                for row in all_rows
                if clean(row.get("ins_form_code"))
            }
        )

        if all_rows:
            excluded.append(
                {
                    "usdot_number": usdot,
                    "current_or_pending_rows": len(current_rows),
                    "previous_rows": len(previous_rows),
                    "form_codes": form_codes,
                }
            )
            continue

        passing.append(
            {
                **carrier,
                "insurance_current_found": False,
                "insurance_history_found": False,
                "insurance_form_codes_found": "NONE",
                "bipd_filing_found": False,
                "cargo_filing_found": False,
                "bond_or_trust_filing_found": False,
                "insurance_checked_at": checked_at,
                "insurance_verification_status": "NO_FMCSA_INSURANCE_HISTORY_FOUND",
            }
        )

    return passing, excluded


def fetch_new_entrant_oos(candidates):
    matches = {}
    usdots = [item["usdot_number"] for item in candidates]
    for batch in chunks(usdots, LOOKUP_BATCH_SIZE):
        rows = socrata_get(
            NEW_ENTRANT_OOS_URL,
            {
                "$select": "dot_number,oos_date,oos_reason,status,rescind_date",
                "$where": usdot_where("dot_number", batch),
                "$limit": 5000,
            },
        )
        for row in rows:
            matches.setdefault(clean_usdot(row.get("dot_number")), []).append(row)
    return matches


def remove_new_entrant_oos(candidates, oos_matches, checked_at):
    passing = []
    excluded = []
    for carrier in candidates:
        usdot = carrier["usdot_number"]
        rows = oos_matches.get(usdot, [])
        if rows:
            excluded.append(
                {
                    "usdot_number": usdot,
                    "oos_orders": rows,
                }
            )
            continue

        passing.append(
            {
                **carrier,
                # This is derived from recent add_date; it is not a direct FMCSA
                # bulk field asserting official New Entrant program status.
                "new_entrant_status": "NEW_ENTRANT_CANDIDATE_DERIVED_FROM_RECENT_ADD_DATE",
                "new_entrant_oos_found": False,
                "new_entrant_oos_checked_at": checked_at,
            }
        )
    return passing, excluded


def fetch_verified_new_ventures(days=DEFAULT_DAYS):
    days = max(1, min(int(days), MAX_DAYS))
    checked_at = datetime.now(EASTERN).isoformat()

    candidates, start_date, today_date = fetch_strict_new_dot_candidates(days)
    current, previous = fetch_insurance_matches(candidates)
    uninsured, insurance_excluded = remove_all_known_insurance(
        candidates, current, previous, checked_at
    )
    oos_matches = fetch_new_entrant_oos(uninsured)
    final, oos_excluded = remove_new_entrant_oos(uninsured, oos_matches, checked_at)

    final.sort(key=lambda x: (x["add_date"], x["usdot_number"]), reverse=True)

    return {
        "success": True,
        "generated_at": checked_at,
        "days": days,
        "date_window": {
            "start_inclusive": start_date,
            "end_exclusive": today_date,
            "timezone": "America/New_York",
        },
        "counts": {
            "strict_new_dot_candidates": len(candidates),
            "excluded_for_current_pending_or_previous_insurance": len(insurance_excluded),
            "after_insurance_filter": len(uninsured),
            "excluded_for_new_entrant_oos": len(oos_excluded),
            "final_verified_candidates": len(final),
        },
        "definition": (
            "Recent strict New DOT carrier with exact AUTHORIZED FOR HIRE, "
            "positive power units, no current/pending/previous FMCSA insurance "
            "record of any form code, and no New Entrant OOS order found."
        ),
        "sources": {
            "census": "az4n-8mr2",
            "current_pending_insurance": "c5y8-a4uz",
            "previous_insurance": "3uet-3z4i",
            "new_entrant_oos": "p2mt-9ige",
        },
        "carriers": final,
        # Audit summaries contain USDOT identifiers and reasons but are not sent
        # as final leads.
        "audit": {
            "insurance_excluded": insurance_excluded,
            "new_entrant_oos_excluded": oos_excluded,
        },
    }


def parse_days(value):
    try:
        return max(1, min(int(value), MAX_DAYS))
    except (TypeError, ValueError):
        return DEFAULT_DAYS


def save_latest(result):
    global LATEST_RESULT
    with CACHE_LOCK:
        LATEST_RESULT = result


def get_or_generate(days):
    result = fetch_verified_new_ventures(days)
    save_latest(result)
    return result


HTML_TEMPLATE = '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width,initial-scale=1">\n  <title>GreenVentures • FMCSA New Venture Intelligence</title>\n  <style>\n    :root{--navy:#ffffff;--navy2:#f6fff9;--ink:#123326;--muted:#648075;--line:#dcebe3;--bg:#f4faf6;--white:#fff;--green:#16a34a;--blue:#16a34a;--cyan:#059669;--amber:#f59e0b;--red:#ef4444}\n    *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}\n    .layout{display:grid;grid-template-columns:258px 1fr;min-height:100vh}.side{background:linear-gradient(180deg,var(--navy),var(--navy2));color:#47695b;border-right:1px solid var(--line);padding:24px 18px;position:sticky;top:0;height:100vh}\n    .brand{display:flex;align-items:center;gap:12px;color:#14532d;font-weight:800;font-size:20px;padding:4px 7px 26px}.logo{width:38px;height:38px;border-radius:11px;display:grid;place-items:center;background:linear-gradient(135deg,#16a34a,#34d399);color:white;box-shadow:0 8px 25px #16a34a35}\n    .nav-title{font-size:11px;text-transform:uppercase;letter-spacing:.14em;color:#7b9589;margin:18px 10px 8px}.nav-item{display:flex;gap:11px;align-items:center;padding:11px 12px;border-radius:10px;margin:4px 0;color:#47695b;text-decoration:none}.nav-item:hover{background:#dcfce7;color:#166534}.nav-item.active{background:#dcfce7;color:#166534;font-weight:750}.side-foot{position:absolute;bottom:22px;left:18px;right:18px;border:1px solid #d7eadf;background:#ffffff;border-radius:12px;padding:13px;font-size:12px}.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px #10b98122;margin-right:8px}\n    main{padding:30px;min-width:0}.top{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;margin-bottom:24px}.top h1{font-size:27px;margin:0 0 7px}.top p{margin:0;color:var(--muted)}.actions{display:flex;gap:9px;flex-wrap:wrap}select,button,input{font:inherit}.btn,.select{border:1px solid var(--line);background:#fff;border-radius:10px;padding:10px 13px}.btn{cursor:pointer;font-weight:700}.btn.primary{background:var(--blue);color:#fff;border-color:var(--blue);box-shadow:0 8px 18px #16a34a2b}.btn:hover{transform:translateY(-1px)}\n    .notice{display:flex;gap:12px;background:#fffbeb;border:1px solid #fde68a;border-radius:13px;padding:13px 15px;color:#854d0e;font-size:13px;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:15px;margin-bottom:20px}.stat{background:#fff;border:1px solid var(--line);border-radius:15px;padding:18px;box-shadow:0 7px 25px #0f172a0a;position:relative;overflow:hidden}.stat:after{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent,var(--blue))}.stat small{color:var(--muted);font-weight:700}.stat strong{font-size:28px;display:block;margin-top:8px}.stat span{font-size:12px;color:#94a3b8}\n    .panel{background:#fff;border:1px solid var(--line);border-radius:15px;box-shadow:0 7px 25px #0f172a0a;overflow:hidden}.panel-head{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:17px 18px;border-bottom:1px solid var(--line)}.panel-head h2{font-size:16px;margin:0}.filters{display:flex;gap:8px}.search{min-width:290px;border:1px solid var(--line);border-radius:9px;padding:9px 11px}.mini{border:1px solid var(--line);border-radius:9px;padding:9px;background:#fff}\n    .table-wrap{overflow:auto;max-height:650px}table{border-collapse:collapse;width:100%;font-size:13px}th{position:sticky;top:0;background:#f0fdf4;color:#35604d;text-align:left;z-index:1;font-size:11px;text-transform:uppercase;letter-spacing:.05em}th,td{padding:12px 14px;border-bottom:1px solid #edf1f5;white-space:nowrap}tbody tr:hover{background:#fafbff}.name{font-weight:750}.sub{display:block;color:var(--muted);font-size:11px;margin-top:3px}.tag{display:inline-flex;align-items:center;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:750}.tag.green{background:#d1fae5;color:#047857}.tag.blue{background:#dcfce7;color:#166534}.tag.gray{background:#eef2f7;color:#475569}.empty{text-align:center;padding:50px;color:var(--muted)}\n    .loader{position:fixed;inset:0;background:#0b2e20dd;display:none;z-index:50;place-items:center;color:#fff;text-align:center}.loader.show{display:grid}.spinner{width:46px;height:46px;border:4px solid #ffffff2b;border-top-color:#4ade80;border-radius:50%;animation:spin .8s linear infinite;margin:auto auto 14px}@keyframes spin{to{transform:rotate(360deg)}}\n    .meta{font-size:12px;color:var(--muted);padding:12px 18px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}.error{background:#fee2e2;color:#991b1b;border:1px solid #fecaca;border-radius:12px;padding:13px;display:none;margin-bottom:15px}\n.section-title{display:flex;justify-content:space-between;align-items:end;margin:28px 0 14px}.section-title h2{margin:0;font-size:20px}.section-title p{margin:5px 0 0;color:var(--muted);font-size:13px}.endpoint-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:15px}.endpoint-card{background:#fff;border:1px solid var(--line);border-radius:15px;padding:18px;box-shadow:0 7px 25px #0f172a0a}.endpoint-top{display:flex;justify-content:space-between;align-items:center;gap:10px}.method{font-size:10px;font-weight:850;padding:5px 8px;border-radius:7px;background:#dcfce7;color:#166534}.method.get{background:#cffafe;color:#0e7490}.endpoint-card h3{font-size:15px;margin:0}.endpoint-card p{font-size:13px;color:var(--muted);line-height:1.5;min-height:39px}.codebox{background:#123326;color:#ecfdf5;border-radius:10px;padding:12px;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;overflow:auto;white-space:pre-wrap;word-break:break-all;margin:10px 0}.copy{border:0;background:#dcfce7;color:#166534;font-weight:750;border-radius:8px;padding:7px 9px;cursor:pointer}.payload-label{font-size:11px;font-weight:800;color:#475569;text-transform:uppercase;letter-spacing:.05em}.flow{background:linear-gradient(135deg,#f0fdf4,#ecfdf5);border:1px solid #bbf7d0;border-radius:14px;padding:16px;margin:15px 0;color:#334155;font-size:13px}.source-list{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.source{background:white;border:1px solid var(--line);padding:12px;border-radius:11px;font-size:12px}.source b{display:block;margin-bottom:4px}.source span{color:var(--muted)}\n    @media(max-width:1100px){.layout{grid-template-columns:82px 1fr}.side{padding:20px 11px}.brand span,.nav-item span,.nav-title,.side-foot{display:none}.brand{justify-content:center}.nav-item{justify-content:center}.grid{grid-template-columns:repeat(2,1fr)}.endpoint-grid{grid-template-columns:1fr}.source-list{grid-template-columns:repeat(2,1fr)}}\n    @media(max-width:720px){.layout{display:block}.side{display:none}main{padding:18px}.top{display:block}.actions{margin-top:15px}.grid{grid-template-columns:1fr}.source-list{grid-template-columns:1fr}.panel-head{display:block}.filters{margin-top:12px;display:grid}.search{min-width:0}.table-wrap{max-height:none}}\n  </style>\n</head>\n<body>\n<div class="loader" id="loader"><div><div class="spinner"></div><b>Screening official FMCSA datasets</b><div class="sub" style="color:#cbd5e1;margin-top:8px">Census → insurance → history → New Entrant OOS</div></div></div>\n<div class="layout">\n  <aside class="side">\n    <div class="brand"><div class="logo">GV</div><span>GreenVentures</span></div>\n    <div class="nav-title">Workspace</div>\n    <a class="nav-item active" href="#dashboard">◫ <span>GreenVentures</span></a>\n    <a class="nav-item" href="#integrations">⌁ <span>n8n & API</span></a>\n    <a class="nav-item" href="#exports">⇩ <span>Exports</span></a>\n    <div class="nav-title">Verification</div>\n    <div class="nav-item">✓ <span>Insurance screening</span></div>\n    <div class="nav-item">✓ <span>New Entrant OOS</span></div>\n    <div class="side-foot"><span class="dot"></span>Official-source pipeline<br><span style="color:#64748b;display:block;margin-top:7px">Daily FMCSA snapshots</span></div>\n  </aside>\n  <main id="dashboard">\n    <div class="top">\n      <div><h1>GreenVentures</h1><p>New-venture intelligence designed for focused commercial trucking outreach.</p></div>\n      <div class="actions">\n        <select class="select" id="days"><option value="1">Previous 1 day</option><option value="3" selected>Previous 3 days</option><option value="7">Previous 7 days</option><option value="14">Previous 14 days</option><option value="30">Previous 30 days</option></select>\n        <button id="exports" class="btn" onclick="location.href=\'/download/csv\'">CSV</button><button class="btn" onclick="location.href=\'/download/excel\'">Excel</button><button class="btn primary" onclick="loadData()">Refresh data</button>\n      </div>\n    </div>\n    <div class="error" id="error"></div>\n    <div class="notice"><b>Verification meaning</b><span>No current, pending, or previous filing was found in the checked FMCSA insurance datasets at the recorded check time. This does not cover private/non-filed insurance outside FMCSA.</span></div>\n    <section class="grid">\n      <div class="stat" style="--accent:#4f46e5"><small>Initial candidates</small><strong id="initial">–</strong><span>Strict recent Census matches</span></div>\n      <div class="stat" style="--accent:#ef4444"><small>Insurance excluded</small><strong id="insurance">–</strong><span>Current, pending, or previous</span></div>\n      <div class="stat" style="--accent:#f59e0b"><small>OOS excluded</small><strong id="oos">–</strong><span>New Entrant orders found</span></div>\n      <div class="stat" style="--accent:#10b981"><small>Final qualified</small><strong id="final">–</strong><span>Ready for workflow output</span></div>\n    </section>\n    <section class="panel">\n      <div class="panel-head"><h2>Qualified carrier records</h2><div class="filters"><input class="search" id="search" placeholder="Search USDOT, company, city, state…" oninput="render()"><select class="mini" id="state" onchange="render()"><option value="">All states</option></select></div></div>\n      <div class="table-wrap"><table><thead><tr><th>USDOT</th><th>Company</th><th>Add date</th><th>Contact</th><th>Location</th><th>Units</th><th>Insurance</th><th>New Entrant</th></tr></thead><tbody id="rows"></tbody></table></div>\n      <div class="meta"><span id="window">Date window: –</span><span id="generated">Generated: –</span></div>\n    </section>\n\n    <div class="section-title" id="integrations"><div><h2>n8n & API integrations</h2><p>Production-ready endpoints for automations, complete responses, exports, and monitoring.</p></div></div>\n    <div class="flow"><b>Recommended n8n flow:</b> HTTP Request → Split Out <code>carriers</code> → Google Sheets / CRM. Use <code>/n8n/output</code> because it returns a compact automation-friendly response without the larger audit arrays.</div>\n    <div class="endpoint-grid">\n      <article class="endpoint-card"><div class="endpoint-top"><h3>n8n qualified output</h3><span class="method">GET / POST</span></div><p>Use this in n8n. It runs every qualification check and returns the final carriers in a <code>carriers</code> array.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/n8n/output">/n8n/output</div><div class="payload-label">POST payload</div><div class="codebox">{\n  "days": 3\n}</div><button class="copy" onclick="copyEndpoint(\'/n8n/output\')">Copy endpoint</button></article>\n      <article class="endpoint-card"><div class="endpoint-top"><h3>Complete platform response</h3><span class="method get">GET</span></div><p>Use for dashboards or debugging. Includes final carriers, exclusion counts, source details, and audit summaries.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/api/data?days=3">/api/data?days=3</div><div class="payload-label">Query payload</div><div class="codebox">days=3  // allowed: 1–30</div><button class="copy" onclick="copyEndpoint(\'/api/data?days=3\')">Copy endpoint</button></article>\n      <article class="endpoint-card"><div class="endpoint-top"><h3>Webhook-compatible output</h3><span class="method">GET / POST</span></div><p>Use for existing webhook clients. Returns the complete platform result and accepts the same number-of-days payload.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/webhook">/webhook</div><div class="payload-label">POST payload</div><div class="codebox">{\n  "days": 3\n}</div><button class="copy" onclick="copyEndpoint(\'/webhook\')">Copy endpoint</button></article>\n      <article class="endpoint-card"><div class="endpoint-top"><h3>Service health check</h3><span class="method get">GET</span></div><p>Use in Render uptime monitors or n8n before a long run. It confirms that the Flask service is online.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/health">/health</div><div class="payload-label">Payload</div><div class="codebox">No payload required</div><button class="copy" onclick="copyEndpoint(\'/health\')">Copy endpoint</button></article>\n      <article class="endpoint-card"><div class="endpoint-top"><h3>CSV export</h3><span class="method get">GET</span></div><p>Downloads the latest final qualified carrier list. Use for spreadsheet imports and archival exports.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/download/csv">/download/csv</div><div class="payload-label">Payload</div><div class="codebox">No payload required</div><button class="copy" onclick="copyEndpoint(\'/download/csv\')">Copy endpoint</button></article>\n      <article class="endpoint-card"><div class="endpoint-top"><h3>Excel workbook</h3><span class="method get">GET</span></div><p>Downloads qualified carriers plus separate audit sheets for insurance and OOS exclusions.</p><div class="payload-label">Endpoint</div><div class="codebox dynamic-url" data-path="/download/excel">/download/excel</div><div class="payload-label">Payload</div><div class="codebox">No payload required</div><button class="copy" onclick="copyEndpoint(\'/download/excel\')">Copy endpoint</button></article>\n    </div>\n\n<script>\nlet records=[];\nconst esc=v=>String(v??\'\').replace(/[&<>"\']/g,m=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#039;\'}[m]));\nfunction render(){\n const q=document.getElementById(\'search\').value.toLowerCase().trim(), state=document.getElementById(\'state\').value;\n const list=records.filter(c=>(!state||c.phy_state===state)&&(!q||[c.usdot_number,c.legal_name,c.dba_name,c.phy_city,c.phy_state].join(\' \').toLowerCase().includes(q)));\n const body=document.getElementById(\'rows\');\n if(!list.length){body.innerHTML=\'<tr><td colspan="8" class="empty">No qualified carriers match this view.</td></tr>\';return}\n body.innerHTML=list.map(c=>`<tr><td><span class="name">${esc(c.usdot_number)}</span><span class="sub">${esc(c.docket_number||\'No docket\')}</span></td><td><span class="name">${esc(c.legal_name)}</span><span class="sub">${esc(c.dba_name||\'\')}</span></td><td><span class="tag blue">${esc(c.add_date)}</span></td><td>${esc(c.phone)}<span class="sub">${esc(c.email_address)}</span></td><td>${esc(c.phy_city)}, ${esc(c.phy_state)}<span class="sub">${esc(c.phy_zip)}</span></td><td>${esc(c.power_units)}</td><td><span class="tag green">No filing found</span><span class="sub">Codes: ${esc(c.insurance_form_codes_found)}</span></td><td><span class="tag gray">Candidate</span><span class="sub">No OOS found</span></td></tr>`).join(\'\');\n}\nasync function loadData(){\n const loader=document.getElementById(\'loader\'), err=document.getElementById(\'error\');loader.classList.add(\'show\');err.style.display=\'none\';\n try{\n  const r=await fetch(\'/api/data?days=\'+document.getElementById(\'days\').value),x=await r.json();if(!r.ok||!x.success)throw new Error(x.error||\'FMCSA request failed\');\n  records=x.carriers||[];const c=x.counts;\n  document.getElementById(\'initial\').textContent=c.strict_new_dot_candidates;document.getElementById(\'insurance\').textContent=c.excluded_for_current_pending_or_previous_insurance;document.getElementById(\'oos\').textContent=c.excluded_for_new_entrant_oos;document.getElementById(\'final\').textContent=c.final_verified_candidates;\n  document.getElementById(\'window\').textContent=`Window: ${x.date_window.start_inclusive} to before ${x.date_window.end_exclusive} • ${x.date_window.timezone}`;document.getElementById(\'generated\').textContent=\'Checked: \'+new Date(x.generated_at).toLocaleString();\n  const states=[...new Set(records.map(x=>x.phy_state).filter(Boolean))].sort();document.getElementById(\'state\').innerHTML=\'<option value="">All states</option>\'+states.map(s=>`<option>${esc(s)}</option>`).join(\'\');render();\n }catch(e){err.textContent=e.message;err.style.display=\'block\'}finally{loader.classList.remove(\'show\')}\n}\n\nfunction initializeEndpoints(){document.querySelectorAll(\'.dynamic-url\').forEach(el=>el.textContent=location.origin+el.dataset.path)}\nasync function copyEndpoint(path){try{await navigator.clipboard.writeText(location.origin+path);event.target.textContent=\'Copied\';setTimeout(()=>event.target.textContent=\'Copy endpoint\',1200)}catch(e){prompt(\'Copy endpoint:\',location.origin+path)}}\nwindow.onload=()=>{initializeEndpoints();loadData()};\n</script>\n</body></html>'


@app.get("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.get("/api/data")
def api_data():
    days = parse_days(request.args.get("days", DEFAULT_DAYS))
    try:
        return jsonify(get_or_generate(days))
    except Exception as error:
        app.logger.exception("FMCSA pipeline failed")
        return jsonify({"success": False, "error": str(error)}), 502


@app.route("/run", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    body = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    days = parse_days(body.get("days", DEFAULT_DAYS))
    try:
        return jsonify(get_or_generate(days))
    except Exception as error:
        app.logger.exception("FMCSA pipeline failed")
        return jsonify({"success": False, "error": str(error)}), 502


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": APP_NAME, "version": APP_VERSION})


@app.route("/n8n/output", methods=["GET", "POST"])
def n8n_output():
    """Compact n8n-compatible output.

    GET:  /n8n/output?days=3
    POST: /n8n/output with {"days": 3}

    The `carriers` property works directly with n8n's Split Out node.
    """
    payload = request.get_json(silent=True) or {} if request.method == "POST" else request.args
    days = parse_days(payload.get("days", DEFAULT_DAYS))
    try:
        result = get_or_generate(days)
        return jsonify({
            "success": True,
            "timestamp": result["generated_at"],
            "days": result["days"],
            "date_window": result["date_window"],
            "total_records": len(result["carriers"]),
            "counts": result["counts"],
            "filters_applied": {
                "census_status": "Active",
                "classdef": "AUTHORIZED FOR HIRE (exact)",
                "power_units": "> 0",
                "insurance": "No current, pending, or previous FMCSA record of any form",
                "new_entrant_oos": "No OOS order found",
            },
            "carriers": result["carriers"],
        })
    except Exception as error:
        app.logger.exception("n8n output pipeline failed")
        return jsonify({"success": False, "error": str(error), "carriers": []}), 502


def latest_or_generate():
    with CACHE_LOCK:
        cached = LATEST_RESULT
    return cached if cached else get_or_generate(DEFAULT_DAYS)


@app.get("/download/csv")
def download_csv():
    result = latest_or_generate()
    frame = pd.DataFrame(result["carriers"])
    data = io.BytesIO(frame.to_csv(index=False).encode("utf-8"))
    return send_file(data, mimetype="text/csv", as_attachment=True, download_name="verified_new_ventures.csv")


@app.get("/download/excel")
def download_excel():
    result = latest_or_generate()
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame(result["carriers"]).to_excel(writer, index=False, sheet_name="Qualified Ventures")
        pd.DataFrame(result["audit"]["insurance_excluded"]).to_excel(writer, index=False, sheet_name="Insurance Excluded")
        pd.DataFrame(result["audit"]["new_entrant_oos_excluded"]).to_excel(writer, index=False, sheet_name="OOS Excluded")
    output.seek(0)
    return send_file(
        output,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="verified_new_ventures.xlsx",
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")

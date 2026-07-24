import os
import io
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import requests
from dateutil import parser as dateutil_parser
from flask import Flask, jsonify, render_template_string, request, send_file
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
STATUS_CHANGE_URL = "https://data.transportation.gov/resource/dm5j-zc6c.json"
CARRIER_INFO_URL  = "https://data.transportation.gov/resource/az4n-8mr2.json"

VALID_DAYS    = {1, 3, 7, 14, 30}
CACHE_TTL_SEC = 300
BATCH_SIZE    = 50
MAX_WORKERS   = 5

# ─────────────────────────────────────────────
# IN-MEMORY CACHE  (thread-safe, TTL-based)
# ─────────────────────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()


def get_cached_ventures(days: int, only_pending: bool = True):
    """Cache key now includes the pending flag so both variants are cached separately."""
    key = f"ventures_{days}_{only_pending}"
    now = datetime.now()

    with _cache_lock:
        if key in _cache:
            data, cached_at = _cache[key]
            if (now - cached_at).total_seconds() < CACHE_TTL_SEC:
                print(f"[cache hit] key={key}  age={(now - cached_at).total_seconds():.0f}s")
                return data

    data = fetch_true_new_ventures(days=days, only_pending=only_pending)

    with _cache_lock:
        _cache[key] = (data, now)

    return data


# ─────────────────────────────────────────────
# HTTP SESSION  (retries + back-off)
# ─────────────────────────────────────────────
def create_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


# ─────────────────────────────────────────────
# DATE HELPERS
# ─────────────────────────────────────────────
def normalize_date(raw) -> str:
    """Convert any FMCSA date format to YYYYMMDD for safe comparison."""
    if not raw:
        return ""
    try:
        return dateutil_parser.parse(str(raw)).strftime("%Y%m%d")
    except (ValueError, TypeError):
        return ""


def iso_cutoff(days: int) -> str:
    """ISO-8601 timestamp for SODA $where clauses."""
    dt = datetime.now() - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT00:00:00.000")


def yyyymmdd_cutoff(days: int) -> str:
    """YYYYMMDD string for add_date comparison."""
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


# ─────────────────────────────────────────────
# API RESPONSE HELPERS
# ─────────────────────────────────────────────
def api_success(data, **kwargs):
    body = {
        "success"  : True,
        "timestamp": datetime.now().isoformat(),
        "count"    : len(data),
        "data"     : data,
    }
    body.update(kwargs)
    return jsonify(body)


def api_error(message, status=500, details=None):
    body = {
        "success"  : False,
        "error"    : message,
        "timestamp": datetime.now().isoformat(),
    }
    if details:
        body["details"] = str(details)
    return jsonify(body), status


# ─────────────────────────────────────────────
# STEP 1 — QUERY MASTER REGISTRY FIRST  (az4n-8mr2)
# ─────────────────────────────────────────────
def fetch_new_carriers_by_add_date(session: requests.Session, days: int) -> list:
    """
    Query az4n-8mr2 DIRECTLY for carriers whose master registration
    (add_date) falls within the requested window.

    This is the ONLY reliable source of truly-new USDOT numbers.
    Starting here (instead of status changes) ensures we do NOT miss
    the ~90 % of new carriers who never file an MC docket in the same
    30-day window.
    """
    iso_start = iso_cutoff(days)
    all_rows  = []
    limit     = 1000                    # az4n-8mr2 supports up to 50 000/call
    offset    = 0

    print(f"[*] MASTER REGISTRY — fetching add_date >= {iso_start}  (last {days} days)")

    while True:
        params = {
            "$where" : f"add_date >= '{iso_start}'",
            "$limit" : limit,
            "$offset": offset,
            "$order" : "add_date DESC",
        }
        try:
            resp = session.get(CARRIER_INFO_URL, params=params, timeout=60)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            print(f"[!] Master registry error (offset={offset}): {exc}")
            break

        if not batch:
            break

        all_rows.extend(batch)
        print(f"    … page offset={offset}  got {len(batch)} rows  total={len(all_rows)}")

        if len(batch) < limit:
            break
        offset += limit

    print(f"[+] MASTER REGISTRY new registrations: {len(all_rows)}")
    return all_rows


# ─────────────────────────────────────────────
# STEP 2 — ENRICH WITH STATUS-CHANGE DATA  (dm5j-zc6c)
# ─────────────────────────────────────────────
def _fetch_status_batch(session: requests.Session, batch: list) -> list:
    """Fetch status-change rows for one batch of DOT numbers."""
    dots_str = ",".join(f"'{d}'" for d in batch)
    params   = {
        "$where": f"usdot_number in ({dots_str})",
        "$limit": len(batch) * 5,       # each DOT may have multiple events
        "$order": "status_change_date DESC",
    }
    try:
        resp = session.get(STATUS_CHANGE_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[!] Status enrichment batch error: {exc}")
        return []


def fetch_status_for_dots(session: requests.Session, dots: list) -> dict:
    """
    Look up status-change events for every new DOT so we can classify
    operating-authority state (Pending / Active).  Returns a dict keyed
    by USDOT → most recent status row.
    """
    if not dots:
        return {}

    print(f"[*] STATUS CHANGES — enriching {len(dots)} new DOTs  (parallel)")

    status_map = {}
    batches    = [dots[i : i + BATCH_SIZE] for i in range(0, len(dots), BATCH_SIZE)]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(_fetch_status_batch, session, b) for b in batches]
        for fut in as_completed(futures):
            for row in fut.result():
                dot = row.get("usdot_number")
                if dot and dot not in status_map:      # DESC order → newest first
                    status_map[dot] = row

    print(f"[+] STATUS CHANGES matched for {len(status_map)} DOTs")
    return status_map


# ─────────────────────────────────────────────
# STEP 3 — MERGE + APPLY BUSINESS RULES
# ─────────────────────────────────────────────
def merge_new_ventures(
    carrier_rows : list,
    status_map   : dict,
    only_pending : bool = True,
) -> list:
    """
    Combine master registry + status change into unified rows.

    Business rules:
      • add_date filter is already applied upstream (in the query itself)
      • If only_pending=True → keep only carriers whose Operating Authority
        is Pending (matches the New-Entrant + Pending-MC use case from
        the screenshots).
    """
    combined = []

    for cd in carrier_rows:
        dot           = cd.get("dot_number")
        sc            = status_map.get(dot, {})
        add_date_raw  = cd.get("add_date", "")
        add_date_norm = normalize_date(add_date_raw)

        # Resolve authority status
        op_status_raw = (
            sc.get("op_auth_status")
            or ("ACTIVE" if cd.get("status_code") == "A" else "PENDING")
        ).upper()

        # Optional strict filter — matches the screenshots
        if only_pending and op_status_raw != "PENDING":
            continue

        combined.append({
            # ── identifiers ────────────────────────────────────────────
            "usdot_number"      : dot or "—",
            "docket_number"     : sc.get("docket_number") or cd.get("docket1") or "—",
            # ── names ──────────────────────────────────────────────────
            "legal_name"        : cd.get("legal_name")   or "—",
            "dba_name"          : cd.get("dba_name")     or "—",
            # ── dates ──────────────────────────────────────────────────
            "add_date"          : add_date_norm,
            "status_change_date": normalize_date(
                                      sc.get("status_change_date") or add_date_raw
                                  ) or add_date_norm,
            # ── authority ──────────────────────────────────────────────
            "op_auth_status"    : op_status_raw,
            "reason"            : sc.get("reason")        or "New Entrant",
            "op_auth_type"      : sc.get("op_auth_type")  or "Motor Carrier of Property",
            # ── contact ────────────────────────────────────────────────
            "phone"             : cd.get("phone") or cd.get("cell_phone") or "—",
            "email_address"     : cd.get("email_address") or "—",
            # ── address ────────────────────────────────────────────────
            "phy_street"        : cd.get("phy_street")    or "—",
            "phy_city"          : cd.get("phy_city")      or "—",
            "phy_state"         : cd.get("phy_state")     or "—",
            "phy_zip"           : cd.get("phy_zip")       or "—",
            # ── fleet ──────────────────────────────────────────────────
            "power_units"       : int(cd.get("power_units") or 1),
            "classdef"          : cd.get("classdef")      or "AUTHORIZED FOR HIRE",
        })

    combined.sort(key=lambda x: x["add_date"], reverse=True)
    return combined


# ─────────────────────────────────────────────
# MAIN PIPELINE  (correct inverted order)
# ─────────────────────────────────────────────
def fetch_true_new_ventures(days: int = 3, only_pending: bool = True) -> list:
    """
    CORRECTED four-step pipeline:

      1. Query az4n-8mr2 for every carrier whose add_date is in the window
      2. Enrich each DOT with its latest dm5j-zc6c status-change event
      3. Optionally keep only carriers with Pending Operating Authority
         (matches the New-Entrant Program + Pending MC combination)
      4. Sort DESC by registration date
    """
    session = create_session()

    # Step 1 — direct pull of new registrations
    carrier_rows = fetch_new_carriers_by_add_date(session, days)
    if not carrier_rows:
        print("[!] No new carriers in master registry — aborting.")
        return []

    # Step 2 — enrich with status data
    unique_dots = [c.get("dot_number") for c in carrier_rows if c.get("dot_number")]
    status_map  = fetch_status_for_dots(session, unique_dots)

    # Step 3 — merge + optional pending-only filter
    results = merge_new_ventures(carrier_rows, status_map, only_pending=only_pending)

    print(
        f"[+] TRUE NEW VENTURES: {len(results)}  "
        f"(pending_only={only_pending}, window={days} days)"
    )
    return results


# ─────────────────────────────────────────────
# CSV BUILDER  (in-memory, no filesystem)
# ─────────────────────────────────────────────
CSV_COLUMNS = [
    "add_date", "status_change_date",
    "legal_name", "dba_name",
    "usdot_number", "docket_number",
    "op_auth_status", "op_auth_type", "reason",
    "phone", "email_address",
    "phy_street", "phy_city", "phy_state", "phy_zip",
    "power_units", "classdef",
]


def build_csv_bytes(data: list) -> bytes:
    df   = pd.DataFrame(data)
    cols = [c for c in CSV_COLUMNS if c in df.columns]
    buf  = io.StringIO()
    df[cols].to_csv(buf, index=False)
    return buf.getvalue().encode("utf-8")


# ─────────────────────────────────────────────
# HTML TEMPLATE
# ─────────────────────────────────────────────
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>GreenSearch — FMCSA True New Ventures</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --font-main        : 'Plus Jakarta Sans', sans-serif;
            --brand-green      : #10b981;
            --brand-green-hover: #059669;
            --body-bg          : #f8fafc;
            --text-dark        : #0f172a;
            --text-muted       : #64748b;
            --border-color     : #e2e8f0;
        }
        body {
            font-family     : var(--font-main);
            background-color: var(--body-bg);
            color           : var(--text-dark);
            overflow-x      : hidden;
        }
        .navbar-green {
            background   : #ffffff;
            border-bottom: 1px solid var(--border-color);
            padding      : 0.85rem 2rem;
        }
        .navbar-brand {
            font-weight    : 800;
            font-size      : 1.25rem;
            color          : var(--text-dark);
            display        : flex;
            align-items    : center;
            gap            : 10px;
            text-decoration: none;
        }
        .navbar-brand .logo-badge {
            background     : var(--brand-green);
            color          : white;
            width          : 32px;
            height         : 32px;
            display        : flex;
            align-items    : center;
            justify-content: center;
            border-radius  : 8px;
            font-size      : 1rem;
        }
        .nav-link {
            font-weight    : 600;
            color          : var(--text-muted);
            text-decoration: none;
            transition     : color 0.2s;
            cursor         : pointer;
        }
        .nav-link:hover,
        .nav-link.active { color: var(--brand-green); }
        .hero-section {
            background   : #ffffff;
            border-bottom: 1px solid var(--border-color);
            padding      : 2.5rem 1.5rem;
            text-align   : center;
        }
        .hero-title {
            font-weight   : 800;
            font-size     : 2.25rem;
            color         : var(--text-dark);
            letter-spacing: -0.02em;
            margin-bottom : 0.5rem;
        }
        .hero-subtitle {
            color        : var(--text-muted);
            font-size    : 1rem;
            margin-bottom: 1.5rem;
        }
        .main-container {
            max-width: 1440px;
            margin   : 2rem auto;
            padding  : 0 1.5rem;
        }
        .filter-card {
            background   : #ffffff;
            border       : 1px solid var(--border-color);
            border-radius: 14px;
            padding      : 1.5rem;
            box-shadow   : 0 2px 10px rgba(0,0,0,0.02);
        }
        .filter-title {
            font-weight   : 700;
            font-size     : 0.95rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color         : var(--text-dark);
        }
        .table-card {
            background   : #ffffff;
            border       : 1px solid var(--border-color);
            border-radius: 14px;
            box-shadow   : 0 2px 10px rgba(0,0,0,0.02);
            overflow     : hidden;
        }
        .table-header-bar {
            padding        : 1.25rem 1.5rem;
            border-bottom  : 1px solid var(--border-color);
            display        : flex;
            justify-content: space-between;
            align-items    : center;
            background     : #fff;
        }
        .table-custom { margin-bottom: 0; white-space: nowrap; }
        .table-custom th {
            font-weight   : 700;
            font-size     : 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            color         : var(--text-muted);
            background    : #f8fafc !important;
            border-bottom : 1px solid var(--border-color);
            padding       : 12px 16px;
        }
        .table-custom td {
            padding       : 14px 16px;
            vertical-align: middle;
            color         : var(--text-dark);
            border-bottom : 1px solid var(--border-color);
            font-size     : 0.9rem;
        }
        .table-container {
            max-height: 600px;
            overflow-y: auto;
        }
        .status-badge {
            font-weight   : 700;
            font-size     : 0.7rem;
            padding       : 5px 10px;
            border-radius : 6px;
            letter-spacing: 0.05em;
        }
        .badge-active  { background: rgba(16,185,129,0.15); color: #059669; }
        .badge-pending { background: rgba(245,158,11,0.15);  color: #d97706; }
        .btn-green {
            background   : var(--brand-green);
            color        : #fff;
            border       : none;
            font-weight  : 600;
            padding      : 0.5rem 1rem;
            border-radius: 8px;
            transition   : background 0.2s;
        }
        .btn-green:hover { background: var(--brand-green-hover); color: #fff; }
        #loadingOverlay {
            display        : none;
            position       : fixed;
            inset          : 0;
            background     : rgba(15,23,42,0.75);
            backdrop-filter: blur(4px);
            z-index        : 9999;
            justify-content: center;
            align-items    : center;
            flex-direction : column;
            gap            : 15px;
            color          : #fff;
        }
        .toast-container { z-index: 11000; }
    </style>
</head>
<body>

<div id="loadingOverlay">
    <div class="spinner-border text-light" style="width:3.5rem;height:3.5rem;" role="status"></div>
    <h4 class="fw-bold mt-3">Loading New Ventures…</h4>
    <p class="text-light opacity-75 small">Querying FMCSA master registry + status data…</p>
</div>

<div class="toast-container position-fixed bottom-0 end-0 p-3">
    <div id="appToast" class="toast align-items-center text-bg-success border-0" role="alert">
        <div class="d-flex">
            <div class="toast-body fw-semibold" id="toastMsg">Done!</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto"
                    data-bs-dismiss="toast"></button>
        </div>
    </div>
</div>

<nav class="navbar navbar-green navbar-expand-lg">
    <div class="container-fluid px-3">
        <a class="navbar-brand" href="#">
            <div class="logo-badge"><i class="fa-solid fa-bolt"></i></div>
            <span>GreenSearch</span>
        </a>
        <div class="d-flex align-items-center gap-4">
            <a href="#" class="nav-link active" id="navDashboard"
               onclick="switchTab('dashboard',event)">Dashboard</a>
            <a href="#" class="nav-link" id="navWebhook"
               onclick="switchTab('webhook',event)">API / Webhook</a>
            <a href="#" class="btn btn-green btn-sm" onclick="downloadCsv(event)">
                <i class="fa-solid fa-download me-1"></i> Export CSV
            </a>
        </div>
    </div>
</nav>

<section class="hero-section">
    <div class="container">
        <h1 class="hero-title">Look up any FMCSA new venture</h1>
        <p class="hero-subtitle">
            Free, public motor-carrier data — search by name, DOT#, MC#, location, or contact.
        </p>
    </div>
</section>

<div class="main-container">

    <!-- DASHBOARD TAB -->
    <div id="tab-dashboard">
        <div class="row g-4">

            <!-- Sidebar -->
            <div class="col-lg-3">
                <div class="filter-card">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <span class="filter-title">Filters</span>
                        <a href="#" class="text-success text-decoration-none small fw-semibold"
                           onclick="resetFilters(event)">Reset all</a>
                    </div>

                    <div class="mb-3">
                        <label class="form-label small fw-bold text-muted">Timeframe</label>
                        <select id="daysSelect" class="form-select form-select-sm"
                                onchange="loadData()">
                            <option value="1">Last 1 Day</option>
                            <option value="3" selected>Last 3 Days</option>
                            <option value="7">Last 7 Days</option>
                            <option value="14">Last 14 Days</option>
                            <option value="30">Last 30 Days</option>
                        </select>
                    </div>

                    <div class="mb-3">
                        <label class="form-label small fw-bold text-muted">Authority Filter</label>
                        <select id="pendingFilter" class="form-select form-select-sm"
                                onchange="loadData()">
                            <option value="true" selected>Pending Only (New Entrants)</option>
                            <option value="false">All New Registrations</option>
                        </select>
                    </div>

                    <div class="mb-3">
                        <label class="form-label small fw-bold text-muted">State</label>
                        <select id="stateFilter" class="form-select form-select-sm"
                                onchange="filterTable()">
                            <option value="">All States</option>
                        </select>
                    </div>

                    <div class="mb-3">
                        <label class="form-label small fw-bold text-muted">Status</label>
                        <select id="statusFilter" class="form-select form-select-sm"
                                onchange="filterTable()">
                            <option value="">All Statuses</option>
                            <option value="ACTIVE">Active</option>
                            <option value="PENDING">Pending</option>
                        </select>
                    </div>

                    <div id="statsBox" class="mt-3 pt-3 border-top d-none">
                        <div class="small text-muted fw-semibold mb-2">QUICK STATS</div>
                        <div class="d-flex justify-content-between small mb-1">
                            <span>Total Records</span>
                            <span class="fw-bold" id="statTotal">—</span>
                        </div>
                        <div class="d-flex justify-content-between small mb-1">
                            <span>Active</span>
                            <span class="fw-bold text-success" id="statActive">—</span>
                        </div>
                        <div class="d-flex justify-content-between small">
                            <span>Pending</span>
                            <span class="fw-bold text-warning" id="statPending">—</span>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Table -->
            <div class="col-lg-9">
                <div class="table-card">
                    <div class="table-header-bar">
                        <div class="w-50">
                            <input type="text" id="searchInput"
                                   class="form-control form-control-sm"
                                   placeholder="Search name, DOT#, MC#, city, email…"
                                   oninput="onSearchInput()">
                        </div>
                        <div class="d-flex gap-2 align-items-center">
                            <span id="resultCount"
                                  class="text-muted small fw-semibold">Showing 0 results</span>
                            <a href="#" class="btn btn-green btn-sm"
                               onclick="downloadCsv(event)">
                                <i class="fa-solid fa-download me-1"></i> Export CSV
                            </a>
                        </div>
                    </div>

                    <div class="table-container">
                        <table class="table table-hover align-middle table-custom">
                            <thead class="sticky-top">
                                <tr>
                                    <th>Carrier</th>
                                    <th>USDOT #</th>
                                    <th>MC #</th>
                                    <th>Location</th>
                                    <th>Units</th>
                                    <th>Auth Type</th>
                                    <th>Status</th>
                                </tr>
                            </thead>
                            <tbody id="tableBody"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- WEBHOOK TAB -->
    <div id="tab-webhook" style="display:none;">
        <div class="filter-card">
            <h4 class="fw-bold text-success mb-3">
                <i class="fa-solid fa-link me-2"></i>n8n Webhook &amp; API Endpoints
            </h4>
            <p class="text-muted mb-4">
                Use these endpoints to integrate GreenSearch into n8n or any
                HTTP-capable automation platform.
            </p>

            <div class="mb-4">
                <label class="form-label fw-bold small">JSON API  (GET)</label>
                <div class="input-group shadow-sm">
                    <input type="text" id="apiUrl"
                           class="form-control font-monospace bg-light" readonly>
                    <button class="btn btn-outline-success"
                            onclick="copyField('apiUrl','API URL copied!')">
                        <i class="fa-solid fa-copy"></i> Copy
                    </button>
                </div>
                <small class="text-muted mt-1 d-block">
                    Query params: <code>?days=3&amp;pending_only=true</code>
                </small>
            </div>

            <div class="mb-4">
                <label class="form-label fw-bold small">Webhook URL  (POST / GET)</label>
                <div class="input-group shadow-sm">
                    <input type="text" id="webhookUrl"
                           class="form-control font-monospace bg-light" readonly>
                    <button class="btn btn-outline-success"
                            onclick="copyField('webhookUrl','Webhook URL copied!')">
                        <i class="fa-solid fa-copy"></i> Copy
                    </button>
                </div>
                <small class="text-muted mt-1 d-block">
                    JSON body: <code>{ "days": 3, "pending_only": true }</code>
                </small>
            </div>

            <div class="mb-4">
                <label class="form-label fw-bold small">CSV Download URL  (GET)</label>
                <div class="input-group shadow-sm">
                    <input type="text" id="csvUrl"
                           class="form-control font-monospace bg-light" readonly>
                    <button class="btn btn-outline-success"
                            onclick="copyField('csvUrl','CSV URL copied!')">
                        <i class="fa-solid fa-copy"></i> Copy
                    </button>
                </div>
                <small class="text-muted mt-1 d-block">
                    Query params: <code>?days=3&amp;pending_only=true</code>
                </small>
            </div>

            <hr>
            <h6 class="fw-bold mt-3">n8n Quick-Start</h6>
            <ol class="text-muted small mt-2">
                <li>Add an <strong>HTTP Request</strong> node → Method: <code>POST</code></li>
                <li>URL: paste the Webhook URL above</li>
                <li>Body (JSON): <code>{ "days": 3, "pending_only": true }</code></li>
                <li>Add a <strong>Split Out</strong> node on the <code>carriers</code> field</li>
                <li>Connect to Google Sheets, Airtable, or a Write File node</li>
            </ol>

            <hr>
            <h6 class="fw-bold mt-3">Data Sources</h6>
            <p class="text-muted small mb-1">
                <strong>Master Registry (primary):</strong>
                <code>data.transportation.gov/resource/az4n-8mr2.json</code>
            </p>
            <p class="text-muted small">
                <strong>Status Changes (enrichment):</strong>
                <code>data.transportation.gov/resource/dm5j-zc6c.json</code>
            </p>
        </div>
    </div>

</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ─────────────────────────────────────────
// GLOBALS
// ─────────────────────────────────────────
let allData        = [];
let searchTimer    = null;
let currentDays    = 3;
let currentPending = "true";

// ─────────────────────────────────────────
// INIT
// ─────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
    updateUrlFields();
    loadData();
});

function updateUrlFields() {
    document.getElementById('apiUrl').value =
        `${location.origin}/api/data?days=${currentDays}&pending_only=${currentPending}`;
    document.getElementById('webhookUrl').value =
        `${location.origin}/webhook`;
    document.getElementById('csvUrl').value =
        `${location.origin}/download/csv?days=${currentDays}&pending_only=${currentPending}`;
}

// ─────────────────────────────────────────
// TAB SWITCHING
// ─────────────────────────────────────────
function switchTab(name, e) {
    if (e) e.preventDefault();
    document.getElementById('tab-dashboard').style.display =
        name === 'dashboard' ? 'block' : 'none';
    document.getElementById('tab-webhook').style.display =
        name === 'webhook' ? 'block' : 'none';
    document.getElementById('navDashboard').classList.toggle('active', name === 'dashboard');
    document.getElementById('navWebhook').classList.toggle('active',   name === 'webhook');
}

// ─────────────────────────────────────────
// DATA LOADING
// ─────────────────────────────────────────
async function loadData() {
    currentDays    = parseInt(document.getElementById('daysSelect').value, 10);
    currentPending = document.getElementById('pendingFilter').value;
    showOverlay(true);

    try {
        const res    = await fetch(
            `/api/data?days=${currentDays}&pending_only=${currentPending}`
        );
        const result = await res.json();

        if (!result.success) {
            showToast('Server error: ' + (result.error || 'Unknown'), 'danger');
            return;
        }

        allData = result.data || [];
        populateStateDropdown(allData);
        updateStats(allData);
        renderTable(allData);
        updateUrlFields();

    } catch (err) {
        console.error(err);
        showToast('Network error — check console.', 'danger');
    } finally {
        showOverlay(false);
    }
}

// ─────────────────────────────────────────
// RENDER TABLE
// ─────────────────────────────────────────
function renderTable(data) {
    const tbody    = document.getElementById('tableBody');
    const fragment = document.createDocumentFragment();

    document.getElementById('resultCount').textContent =
        `Showing ${data.length.toLocaleString()} results`;

    if (data.length === 0) {
        const tr = document.createElement('tr');
        tr.innerHTML =
            `<td colspan="7" class="text-center text-muted py-5">
                 No new ventures found for this period.
             </td>`;
        fragment.appendChild(tr);
    } else {
        data.forEach(item => {
            const isActive   = (item.op_auth_status || '').toUpperCase() === 'ACTIVE';
            const badgeCls   = isActive ? 'badge-active' : 'badge-pending';
            const statusText = isActive ? 'ACTIVE' : 'PENDING';
            const tr         = document.createElement('tr');

            tr.innerHTML = `
                <td>
                    <div class="fw-bold text-dark">${esc(item.legal_name)}</div>
                    ${item.dba_name && item.dba_name !== '—'
                        ? `<small class="text-muted">d/b/a ${esc(item.dba_name)}</small>`
                        : ''}
                    <div class="mt-1">
                        <span class="badge bg-light text-secondary border font-monospace"
                              style="font-size:0.65rem;">
                            Reg: ${esc(item.add_date)}
                        </span>
                        <span class="badge bg-light text-secondary border font-monospace ms-1"
                              style="font-size:0.65rem;">
                            SC: ${esc(item.status_change_date)}
                        </span>
                    </div>
                </td>
                <td><span class="font-monospace fw-bold">${esc(item.usdot_number)}</span></td>
                <td><span class="font-monospace">${esc(item.docket_number)}</span></td>
                <td>
                    <div>${esc(item.phy_city)}, ${esc(item.phy_state)}</div>
                    <small class="text-muted">${esc(item.phy_zip)}</small>
                </td>
                <td><span class="fw-semibold">${item.power_units ?? '—'}</span></td>
                <td><small class="text-muted">${esc(item.op_auth_type)}</small></td>
                <td><span class="status-badge ${badgeCls}">${statusText}</span></td>`;

            fragment.appendChild(tr);
        });
    }

    tbody.innerHTML = '';
    tbody.appendChild(fragment);
}

// ─────────────────────────────────────────
// XSS PREVENTION
// ─────────────────────────────────────────
function esc(str) {
    if (!str || str === '—') return '—';
    return String(str)
        .replace(/&/g,  '&amp;')
        .replace(/</g,  '&lt;')
        .replace(/>/g,  '&gt;')
        .replace(/"/g,  '&quot;')
        .replace(/'/g,  '&#039;');
}

// ─────────────────────────────────────────
// FILTERING
// ─────────────────────────────────────────
function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(filterTable, 250);
}

function filterTable() {
    const query  = document.getElementById('searchInput').value.toLowerCase().trim();
    const state  = document.getElementById('stateFilter').value;
    const status = document.getElementById('statusFilter').value.toUpperCase();

    const filtered = allData.filter(item => {
        const haystack = [
            item.legal_name, item.dba_name,
            item.usdot_number, item.docket_number,
            item.phy_city, item.phy_state,
            item.email_address, item.phone,
            item.op_auth_type, item.reason,
        ].join(' ').toLowerCase();

        return (
            (!query  || haystack.includes(query)) &&
            (!state  || item.phy_state === state)  &&
            (!status || (item.op_auth_status || '').toUpperCase() === status)
        );
    });

    renderTable(filtered);
}

// ─────────────────────────────────────────
// STATE DROPDOWN
// ─────────────────────────────────────────
function populateStateDropdown(data) {
    const states = [
        ...new Set(data.map(i => i.phy_state).filter(s => s && s !== '—'))
    ].sort();

    const sel = document.getElementById('stateFilter');
    sel.innerHTML = '<option value="">All States</option>';
    states.forEach(s => {
        const opt       = document.createElement('option');
        opt.value       = s;
        opt.textContent = s;
        sel.appendChild(opt);
    });
}

// ─────────────────────────────────────────
// QUICK STATS
// ─────────────────────────────────────────
function updateStats(data) {
    const active  = data.filter(
        d => (d.op_auth_status || '').toUpperCase() === 'ACTIVE'
    ).length;
    const pending = data.length - active;

    document.getElementById('statTotal').textContent   = data.length.toLocaleString();
    document.getElementById('statActive').textContent  = active.toLocaleString();
    document.getElementById('statPending').textContent = pending.toLocaleString();
    document.getElementById('statsBox').classList.remove('d-none');
}

// ─────────────────────────────────────────
// CSV DOWNLOAD
// ─────────────────────────────────────────
function downloadCsv(e) {
    if (e) e.preventDefault();
    window.open(
        `/download/csv?days=${currentDays}&pending_only=${currentPending}`,
        '_blank'
    );
}

// ─────────────────────────────────────────
// RESET FILTERS
// ─────────────────────────────────────────
function resetFilters(e) {
    if (e) e.preventDefault();
    document.getElementById('searchInput').value  = '';
    document.getElementById('stateFilter').value  = '';
    document.getElementById('statusFilter').value = '';
    document.getElementById('daysSelect').value   = '3';
    document.getElementById('pendingFilter').value = 'true';
    loadData();
}

// ─────────────────────────────────────────
// OVERLAY  /  TOAST  /  CLIPBOARD
// ─────────────────────────────────────────
function showOverlay(show) {
    document.getElementById('loadingOverlay').style.display =
        show ? 'flex' : 'none';
}

function showToast(msg, type = 'success') {
    const el = document.getElementById('appToast');
    el.className = `toast align-items-center text-bg-${type} border-0`;
    document.getElementById('toastMsg').textContent = msg;
    bootstrap.Toast.getOrCreateInstance(el).show();
}

function copyField(id, label = 'Copied!') {
    const el = document.getElementById(id);
    navigator.clipboard.writeText(el.value)
        .then(()  => showToast(label))
        .catch(()  => showToast('Copy failed — select manually.', 'danger'));
}
</script>
</body>
</html>
"""


# ─────────────────────────────────────────────
# HELPER — parse pending_only flag safely
# ─────────────────────────────────────────────
def parse_pending_flag(raw) -> bool:
    if raw is None:
        return True
    return str(raw).strip().lower() in ("true", "1", "yes", "y", "on")


# ─────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/data", methods=["GET"])
def api_data():
    try:
        days = int(request.args.get("days", 3))
    except (ValueError, TypeError):
        return api_error("'days' must be an integer.", status=400)

    if days not in VALID_DAYS:
        return api_error(
            f"Invalid days value. Allowed: {sorted(VALID_DAYS)}", status=400
        )

    pending_only = parse_pending_flag(request.args.get("pending_only", "true"))

    try:
        data = get_cached_ventures(days=days, only_pending=pending_only)
        return api_success(data, days_queried=days, pending_only=pending_only)
    except Exception as exc:
        print(f"[!] /api/data error: {exc}")
        return api_error("Failed to fetch FMCSA data.", status=503, details=exc)


@app.route("/webhook", methods=["GET", "POST"])
def n8n_webhook():
    payload = request.get_json(silent=True) or request.args.to_dict()

    try:
        days = int(payload.get("days", 3))
    except (ValueError, TypeError):
        days = 3

    if days not in VALID_DAYS:
        days = 3

    pending_only = parse_pending_flag(payload.get("pending_only", True))

    try:
        data = get_cached_ventures(days=days, only_pending=pending_only)
        return jsonify({
            "success"      : True,
            "timestamp"    : datetime.now().isoformat(),
            "days_queried" : days,
            "pending_only" : pending_only,
            "total_records": len(data),
            "carriers"     : data,
        })
    except Exception as exc:
        print(f"[!] /webhook error: {exc}")
        return api_error("Pipeline error.", status=503, details=exc)


@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    payload = request.get_json(silent=True) or request.args.to_dict()

    try:
        days = int(payload.get("days", 3))
    except (ValueError, TypeError):
        days = 3

    if days not in VALID_DAYS:
        days = 3

    pending_only = parse_pending_flag(payload.get("pending_only", True))

    try:
        data = get_cached_ventures(days=days, only_pending=pending_only)
        return api_success(
            data,
            days_queried=days,
            pending_only=pending_only,
            download_url=f"/download/csv?days={days}&pending_only={pending_only}",
        )
    except Exception as exc:
        print(f"[!] /run error: {exc}")
        return api_error("Pipeline error.", status=503, details=exc)


@app.route("/download/csv", methods=["GET"])
def download_csv():
    """Stream CSV directly from memory — respects both ?days and ?pending_only."""
    try:
        days = int(request.args.get("days", 3))
    except (ValueError, TypeError):
        days = 3

    if days not in VALID_DAYS:
        days = 3

    pending_only = parse_pending_flag(request.args.get("pending_only", "true"))

    try:
        data = get_cached_ventures(days=days, only_pending=pending_only)
    except Exception as exc:
        return api_error("Could not generate CSV.", status=503, details=exc)

    if not data:
        return api_error(
            "No data available — load the dashboard first.", status=404
        )

    csv_bytes = build_csv_bytes(data)
    suffix    = "pending" if pending_only else "all"
    filename  = f"true_new_ventures_{suffix}_{datetime.now().strftime('%Y%m%d')}.csv"

    return send_file(
        io.BytesIO(csv_bytes),
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

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


def get_cached_ventures(days: int):
    key = f"ventures_{days}"
    now = datetime.now()

    with _cache_lock:
        if key in _cache:
            data, cached_at = _cache[key]
            if (now - cached_at).total_seconds() < CACHE_TTL_SEC:
                print(f"[cache hit] key={key}  age={(now - cached_at).total_seconds():.0f}s")
                return data

    data = fetch_true_new_ventures(days=days)

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
# STEP 1 — FETCH STATUS CHANGES  (dm5j-zc6c)
# ─────────────────────────────────────────────
def fetch_status_changes(session: requests.Session, days: int) -> list:
    """
    Pull every status-change event from dm5j-zc6c that falls within
    the requested window AND represents an initial / pending filing.
    Paginates automatically until the full result set is collected.
    """
    iso_start = iso_cutoff(days)
    all_rows  = []
    limit     = 500
    offset    = 0

    print(f"[*] STATUS CHANGES — fetching from {iso_start}  (last {days} days)")

    while True:
        params = {
            "$where" : (
                f"status_change_date >= '{iso_start}' "
                f"AND (op_auth_status = 'Pending' OR reason = 'Initial Status')"
            ),
            "$limit" : limit,
            "$offset": offset,
            "$order" : "status_change_date DESC",
        }
        try:
            resp = session.get(STATUS_CHANGE_URL, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as exc:
            print(f"[!] Status-change fetch error (offset={offset}): {exc}")
            break

        if not batch:
            break

        all_rows.extend(batch)
        print(f"    … page offset={offset}  got {len(batch)} rows  total={len(all_rows)}")

        if len(batch) < limit:
            break

        offset += limit

    print(f"[+] STATUS CHANGES total: {len(all_rows)}")
    return all_rows


# ─────────────────────────────────────────────
# STEP 2 — DEDUPLICATE BY DOT
# ─────────────────────────────────────────────
def build_status_map(status_rows: list) -> dict:
    """
    Keep only the most-recent status-change event per DOT number.
    Because rows arrive DESC-ordered the first occurrence per DOT
    is always the newest.
    """
    status_map = {}
    for sc in status_rows:
        dot = sc.get("usdot_number")
        if dot and dot not in status_map:
            status_map[dot] = sc
    print(f"[*] Unique DOTs after dedup: {len(status_map)}")
    return status_map


# ─────────────────────────────────────────────
# STEP 3 — FETCH CARRIER PROFILES  (az4n-8mr2)
# ─────────────────────────────────────────────
def fetch_one_carrier_batch(session: requests.Session, dots: list) -> list:
    """Fetch master-registry records for one batch of DOT numbers."""
    dots_str = ",".join(f"'{d}'" for d in dots)
    params   = {
        "$where": f"dot_number in ({dots_str})",
        "$limit": len(dots) + 10,          # safety buffer above batch size
    }
    try:
        resp = session.get(CARRIER_INFO_URL, params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"[!] Carrier batch error: {exc}")
        return []


def fetch_all_carrier_profiles(session: requests.Session, unique_dots: list) -> list:
    """
    Split unique DOTs into batches and fetch all carrier master profiles
    in parallel using a thread pool — significantly faster than sequential.
    """
    batches = [
        unique_dots[i : i + BATCH_SIZE]
        for i in range(0, len(unique_dots), BATCH_SIZE)
    ]
    results = []

    print(f"[*] CARRIER PROFILES — {len(unique_dots)} DOTs in {len(batches)} batches  (parallel)")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_one_carrier_batch, session, batch): batch
            for batch in batches
        }
        for fut in as_completed(futures):
            batch_result = fut.result()
            results.extend(batch_result)

    print(f"[+] CARRIER PROFILES total returned: {len(results)}")
    return results


# ─────────────────────────────────────────────
# STEP 4 — MERGE + STRICT add_date FILTER
# ─────────────────────────────────────────────
def merge_and_filter(
    carrier_rows: list,
    status_map  : dict,
    cutoff_str  : str,
) -> list:
    """
    Join carrier master data with the matching status-change event,
    then apply the CORE BUSINESS RULE:

        add_date >= cutoff_str

    This single condition is what eliminates reinstated carriers,
    holding-company secondary filings, and all other false positives.
    Only entities whose PRIMARY registration date falls inside the
    requested window survive.
    """
    combined = []

    for cd in carrier_rows:
        dot           = cd.get("dot_number")
        sc            = status_map.get(dot, {})
        add_date_raw  = cd.get("add_date", "")
        add_date_norm = normalize_date(add_date_raw)   # always YYYYMMDD or ''

        # ── CORE BUSINESS RULE ─────────────────────────────────────────────
        # Reject any carrier whose primary registration predates the window.
        if not add_date_norm or add_date_norm < cutoff_str:
            continue
        # ──────────────────────────────────────────────────────────────────

        # Resolve op_auth_status — prefer status-change record, fall back to
        # master-registry status_code so every row always has a value.
        raw_status = sc.get("op_auth_status") or (
            "ACTIVE" if cd.get("status_code") == "A" else "PENDING"
        )

        combined.append({
            # ── identifiers ────────────────────────────────────────────────
            "usdot_number"      : dot or "—",
            "docket_number"     : sc.get("docket_number") or cd.get("docket1") or "—",
            # ── names ──────────────────────────────────────────────────────
            "legal_name"        : cd.get("legal_name")   or "—",
            "dba_name"          : cd.get("dba_name")     or "—",
            # ── dates ──────────────────────────────────────────────────────
            "add_date"          : add_date_norm,
            "status_change_date": normalize_date(
                                      sc.get("status_change_date") or add_date_raw
                                  ) or add_date_norm,
            # ── authority ──────────────────────────────────────────────────
            "op_auth_status"    : raw_status.upper(),
            "reason"            : sc.get("reason")        or "Initial Status",
            "op_auth_type"      : sc.get("op_auth_type")  or "Motor Carrier of Property",
            # ── contact ────────────────────────────────────────────────────
            "phone"             : cd.get("phone") or cd.get("cell_phone") or "—",
            "email_address"     : cd.get("email_address") or "—",
            # ── address ────────────────────────────────────────────────────
            "phy_street"        : cd.get("phy_street")    or "—",
            "phy_city"          : cd.get("phy_city")      or "—",
            "phy_state"         : cd.get("phy_state")     or "—",
            "phy_zip"           : cd.get("phy_zip")       or "—",
            # ── fleet ──────────────────────────────────────────────────────
            "power_units"       : int(cd.get("power_units") or 1),
            "classdef"          : cd.get("classdef")      or "AUTHORIZED FOR HIRE",
        })

    combined.sort(key=lambda x: x["add_date"], reverse=True)
    return combined


# ─────────────────────────────────────────────
# MAIN PIPELINE ORCHESTRATOR
# ─────────────────────────────────────────────
def fetch_true_new_ventures(days: int = 3) -> list:
    """
    Full four-step pipeline:
      1. Fetch status-change events  (dm5j-zc6c)
      2. Deduplicate by DOT number
      3. Fetch carrier master profiles in parallel  (az4n-8mr2)
      4. Merge + apply strict add_date filter → sort DESC
    """
    session    = create_session()
    cutoff_str = yyyymmdd_cutoff(days)

    # Step 1 — status changes
    status_rows = fetch_status_changes(session, days)
    if not status_rows:
        print("[!] No status-change rows returned — aborting pipeline.")
        return []

    # Step 2 — deduplicate
    status_map  = build_status_map(status_rows)
    unique_dots = list(status_map.keys())

    # Step 3 — carrier master profiles
    carrier_rows = fetch_all_carrier_profiles(session, unique_dots)

    # Step 4 — merge + filter
    results = merge_and_filter(carrier_rows, status_map, cutoff_str)

    print(f"[+] TRUE NEW VENTURES (add_date >= {cutoff_str}): {len(results)}")
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

        /* ── Navbar ── */
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

        /* ── Hero ── */
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

        /* ── Layout ── */
        .main-container {
            max-width: 1440px;
            margin   : 2rem auto;
            padding  : 0 1.5rem;
        }

        /* ── Filter card ── */
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

        /* ── Table card ── */
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

        /* ── Badges ── */
        .status-badge {
            font-weight   : 700;
            font-size     : 0.7rem;
            padding       : 5px 10px;
            border-radius : 6px;
            letter-spacing: 0.05em;
        }
        .badge-active  { background: rgba(16,185,129,0.15); color: #059669; }
        .badge-pending { background: rgba(245,158,11,0.15);  color: #d97706; }

        /* ── Buttons ── */
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

        /* ── Loading overlay ── */
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

<!-- Loading Overlay -->
<div id="loadingOverlay">
    <div class="spinner-border text-light" style="width:3.5rem;height:3.5rem;" role="status"></div>
    <h4 class="fw-bold mt-3">Loading New Ventures…</h4>
    <p class="text-light opacity-75 small">Querying FMCSA status-change + carrier registry…</p>
</div>

<!-- Toast -->
<div class="toast-container position-fixed bottom-0 end-0 p-3">
    <div id="appToast" class="toast align-items-center text-bg-success border-0" role="alert">
        <div class="d-flex">
            <div class="toast-body fw-semibold" id="toastMsg">Done!</div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto"
                    data-bs-dismiss="toast"></button>
        </div>
    </div>
</div>

<!-- Navbar -->
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

<!-- Hero -->
<section class="hero-section">
    <div class="container">
        <h1 class="hero-title">Look up any FMCSA new venture</h1>
        <p class="hero-subtitle">
            Free, public motor-carrier data — search by name, DOT#, MC#, location, or contact.
        </p>
    </div>
</section>

<!-- Main -->
<div class="main-container">

    <!-- ── DASHBOARD TAB ── -->
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

                    <!-- Quick Stats -->
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
    </div><!-- /tab-dashboard -->

    <!-- ── WEBHOOK TAB ── -->
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
                    Query param: <code>?days=3</code>
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
                    JSON body: <code>{ "days": 3 }</code>
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
                    Query param: <code>?days=3</code>
                </small>
            </div>

            <hr>
            <h6 class="fw-bold mt-3">n8n Quick-Start</h6>
            <ol class="text-muted small mt-2">
                <li>Add an <strong>HTTP Request</strong> node → Method: <code>POST</code></li>
                <li>URL: paste the Webhook URL above</li>
                <li>Body (JSON): <code>{ "days": 3 }</code></li>
                <li>Add a <strong>Split Out</strong> node on the <code>carriers</code> field</li>
                <li>Connect to Google Sheets, Airtable, or a Write File node</li>
            </ol>

            <hr>
            <h6 class="fw-bold mt-3">Data Sources</h6>
            <p class="text-muted small mb-1">
                <strong>Status Changes:</strong>
                <code>data.transportation.gov/resource/dm5j-zc6c.json</code>
            </p>
            <p class="text-muted small">
                <strong>Carrier Master Registry:</strong>
                <code>data.transportation.gov/resource/az4n-8mr2.json</code>
            </p>
        </div>
    </div><!-- /tab-webhook -->

</div><!-- /main-container -->

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
<script>
// ─────────────────────────────────────────
// GLOBALS
// ─────────────────────────────────────────
let allData     = [];
let searchTimer = null;
let currentDays = 3;

// ─────────────────────────────────────────
// INIT
// ─────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
    document.getElementById('apiUrl').value     = `${location.origin}/api/data?days=3`;
    document.getElementById('webhookUrl').value  = `${location.origin}/webhook`;
    document.getElementById('csvUrl').value      = `${location.origin}/download/csv?days=3`;
    loadData();
});

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
    currentDays = parseInt(document.getElementById('daysSelect').value, 10);
    showOverlay(true);

    try {
        const res    = await fetch(`/api/data?days=${currentDays}`);
        const result = await res.json();

        if (!result.success) {
            showToast('Server error: ' + (result.error || 'Unknown'), 'danger');
            return;
        }

        allData = result.data || [];
        populateStateDropdown(allData);
        updateStats(allData);
        renderTable(allData);

        // keep URL fields in sync with selected timeframe
        document.getElementById('apiUrl').value =
            `${location.origin}/api/data?days=${currentDays}`;
        document.getElementById('csvUrl').value =
            `${location.origin}/download/csv?days=${currentDays}`;

    } catch (err) {
        console.error(err);
        showToast('Network error — check console.', 'danger');
    } finally {
        showOverlay(false);
    }
}

// ─────────────────────────────────────────
// RENDER TABLE  (DocumentFragment — single DOM write)
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
                <td>
                    <small class="text-muted">${esc(item.op_auth_type)}</small>
                </td>
                <td>
                    <span class="status-badge ${badgeCls}">${statusText}</span>
                </td>`;

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
// FILTERING  (debounced)
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
    window.open(`/download/csv?days=${currentDays}`, '_blank');
}

// ─────────────────────────────────────────
// RESET FILTERS
// ─────────────────────────────────────────
function resetFilters(e) {
    if (e) e.preventDefault();
    document.getElementById('searchInput').value = '';
    document.getElementById('stateFilter').value  = '';
    document.getElementById('statusFilter').value = '';
    document.getElementById('daysSelect').value   = '3';
    loadData();
}

// ─────────────────────────────────────────
// OVERLAY
// ─────────────────────────────────────────
function showOverlay(show) {
    document.getElementById('loadingOverlay').style.display =
        show ? 'flex' : 'none';
}

// ─────────────────────────────────────────
// TOAST
// ─────────────────────────────────────────
function showToast(msg, type = 'success') {
    const el = document.getElementById('appToast');
    el.className = `toast align-items-center text-bg-${type} border-0`;
    document.getElementById('toastMsg').textContent = msg;
    bootstrap.Toast.getOrCreateInstance(el).show();
}

// ─────────────────────────────────────────
// COPY TO CLIPBOARD
// ─────────────────────────────────────────
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

    try:
        data = get_cached_ventures(days=days)
        return api_success(data, days_queried=days)
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

    try:
        data = get_cached_ventures(days=days)
        return jsonify({
            "success"      : True,
            "timestamp"    : datetime.now().isoformat(),
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

    try:
        data = get_cached_ventures(days=days)
        return api_success(
            data,
            days_queried=days,
            download_url=f"/download/csv?days={days}",
        )
    except Exception as exc:
        print(f"[!] /run error: {exc}")
        return api_error("Pipeline error.", status=503, details=exc)


@app.route("/download/csv", methods=["GET"])
def download_csv():
    """
    Stream CSV directly from memory — no ephemeral filesystem dependency.
    Respects ?days= so the download always matches the dashboard timeframe.
    """
    try:
        days = int(request.args.get("days", 3))
    except (ValueError, TypeError):
        days = 3

    if days not in VALID_DAYS:
        days = 3

    try:
        data = get_cached_ventures(days=days)
    except Exception as exc:
        return api_error("Could not generate CSV.", status=503, details=exc)

    if not data:
        return api_error(
            "No data available — load the dashboard first.", status=404
        )

    csv_bytes = build_csv_bytes(data)
    filename  = f"true_new_ventures_{datetime.now().strftime('%Y%m%d')}.csv"

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

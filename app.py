import os
from flask import Flask, render_template_string, request, jsonify, send_file
import requests
import pandas as pd
from datetime import datetime, timedelta

app = Flask(__name__)

CARRIER_INFO_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
STATUS_CHANGE_URL = "https://data.transportation.gov/resource/dm5j-zc6c.json"

# =============================================================================
# STRICT DATE LOGIC
# =============================================================================

def get_previous_business_day(from_date=None):
    """
    Get the previous business day (Mon-Fri).
    - Monday -> Friday
    - Sunday -> Friday
    - Saturday -> Friday
    - Otherwise -> Yesterday
    """
    if from_date is None:
        from_date = datetime.now()

    target = from_date - timedelta(days=1)
    while target.weekday() >= 5:  # 5=Saturday, 6=Sunday
        target -= timedelta(days=1)
    return target

def get_date_range(days, from_date=None):
    """
    Calculate the strict date range for queries.
    - days=1: Returns (previous_business_day, previous_business_day)
    - days=7: Returns (previous_business_day - 6 days, previous_business_day)
    - days=14: Returns (previous_business_day - 13 days, previous_business_day)
    """
    end_date = get_previous_business_day(from_date)
    start_date = end_date - timedelta(days=days - 1)
    return start_date, end_date

# =============================================================================
# STATUS CHANGE FETCHING
# =============================================================================

def fetch_status_change_dates(dot_numbers):
    """
    Fetch the latest 'Pending' status change date for each DOT.
    """
    if not dot_numbers:
        return {}

    in_clause = "(" + ",".join([f"'{d}'" for d in dot_numbers]) + ")"
    params = {
        "$where": f"dot_number in {in_clause} AND status_code = 'P'",
        "$order": "status_date DESC",
        "$limit": 10000
    }
    try:
        resp = requests.get(STATUS_CHANGE_URL, params=params, timeout=60)
        resp.raise_for_status()
        status_data = resp.json()
    except Exception as e:
        print(f"[!] Error fetching status changes: {e}")
        return {}

    latest_status = {}
    for item in status_data:
        dot = item.get("dot_number")
        status_date = item.get("status_date")
        if dot and status_date and dot not in latest_status:
            latest_status[dot] = status_date
    return latest_status

# =============================================================================
# STRICT CARRIER FETCHING
# =============================================================================

def fetch_new_ventures(days=1):
    """
    STRICT New Ventures Engine.

    Filters (ALWAYS applied):
      - status_code = 'P' (Pending)
      - operation_classification = 'Motor Carrier of Property (Except Household Goods)'

    Date Logic:
      - 1-day: Previous business day ONLY (e.g., if today is 25th Jul Sat, fetch 24th Jul Fri)
      - 7-day: Last 7 calendar days ending on previous business day
      - 14-day: Last 14 calendar days ending on previous business day

    Validation:
      - All returned records are verified to have the exact requested add_date(s)
      - Only 2026 records are accepted
    """
    today = datetime.now()
    start_date, end_date = get_date_range(days, today)

    start_str = start_date.strftime('%Y%m%d')
    end_str = end_date.strftime('%Y%m%d')

    print(f"[*] Today: {today.strftime('%Y-%m-%d %A')}")
    print(f"[*] Mode: LAST {days} DAY(S)")
    print(f"[*] Fetching add_date from {start_str} to {end_str}")
    print(f"[*] Strict Filters: status_code='P' | op_class='Motor Carrier of Property (Except Household Goods)'")

    all_carriers = []
    limit = 1000
    offset = 0

    # STRICT where clause - NEVER changes
    where_clause = (
        f"status_code = 'P' "
        f"AND operation_classification = 'Motor Carrier of Property (Except Household Goods)' "
        f"AND add_date >= '{start_str}' AND add_date <= '{end_str}'"
    )

    while True:
        params = {
            "$where": where_clause,
            "$limit": limit,
            "$offset": offset,
            "$order": "add_date DESC"
        }
        try:
            resp = requests.get(CARRIER_INFO_URL, params=params, timeout=60)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[!] API error during pagination: {e}")
            break

        if not batch:
            break

        # STRICT VALIDATION: Only accept records that match our exact date range and year 2026
        for record in batch:
            add_date = record.get('add_date', '')
            if add_date and add_date.startswith('2026') and start_str <= add_date <= end_str:
                all_carriers.append(record)
            else:
                print(f"[!] FILTERED OUT record with add_date={add_date} (outside {start_str}-{end_str})")

        if len(batch) < limit:
            break
        offset += limit

    print(f"[+] Strictly validated {len(all_carriers)} new ventures from {start_str} to {end_str}.")
    return process_carriers(all_carriers)

# =============================================================================
# DATA PROCESSING
# =============================================================================

def process_carriers(carrier_list):
    """Format and enrich carrier data."""
    dot_numbers = [cd.get("dot_number") for cd in carrier_list if cd.get("dot_number")]
    status_dates = fetch_status_change_dates(dot_numbers)

    verified_ventures = []
    for cd in carrier_list:
        dot = cd.get("dot_number")
        legal_name = cd.get("legal_name", "").strip()
        if not dot or not legal_name:
            continue

        docket = cd.get("docket1", "")
        if docket and cd.get("docket1prefix"):
            docket = f"{cd.get('docket1prefix')}{docket}"

        merged = {
            "usdot_number": dot,
            "docket_number": docket or "—",
            "legal_name": legal_name,
            "dba_name": cd.get("dba_name") or "—",
            "add_date": cd.get("add_date", ""),
            "status_change_date": status_dates.get(dot, cd.get("mcs150_date", "")),
            "op_auth_status": "PENDING",
            "reason": "Initial Registration",
            "op_auth_type": "Motor Carrier of Property (Except Household Goods)",
            "phone": cd.get("phone") or cd.get("cell_phone") or "N/A",
            "email_address": cd.get("email_address") or "N/A",
            "phy_street": cd.get("phy_street") or "",
            "phy_city": cd.get("phy_city") or "",
            "phy_state": cd.get("phy_state") or "",
            "phy_zip": cd.get("phy_zip") or "",
            "power_units": int(cd.get("power_units") or 1),
            "drivers": int(cd.get("total_drivers") or cd.get("total_cdl") or 1),
            "classdef": cd.get("classdef") or "AUTHORIZED FOR HIRE",
        }
        verified_ventures.append(merged)

    verified_ventures.sort(key=lambda x: x["add_date"], reverse=True)
    return verified_ventures

# =============================================================================
# HTML TEMPLATE
# =============================================================================

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
            --font-main: 'Plus Jakarta Sans', sans-serif;
            --brand-green: #10b981;
            --brand-green-hover: #059669;
            --body-bg: #f8fafc;
            --text-dark: #0f172a;
            --text-muted: #64748b;
            --border-color: #e2e8f0;
        }
        body { font-family: var(--font-main); background-color: var(--body-bg); color: var(--text-dark); overflow-x: hidden; }
        .navbar-green { background: #ffffff; border-bottom: 1px solid var(--border-color); padding: 0.85rem 2rem; }
        .navbar-brand { font-weight: 800; font-size: 1.25rem; color: var(--text-dark); display: flex; align-items: center; gap: 10px; text-decoration: none; }
        .navbar-brand .logo-badge { background: var(--brand-green); color: white; width: 32px; height: 32px; display: flex; align-items: center; justify-content: center; border-radius: 8px; font-size: 1rem; }
        .nav-link { font-weight: 600; color: var(--text-muted); text-decoration: none; transition: color 0.2s; cursor: pointer; }
        .nav-link:hover, .nav-link.active { color: var(--brand-green); }
        .hero-section { background: #ffffff; border-bottom: 1px solid var(--border-color); padding: 2.5rem 1.5rem; text-align: center; }
        .hero-title { font-weight: 800; font-size: 2.25rem; color: var(--text-dark); letter-spacing: -0.02em; margin-bottom: 0.5rem; }
        .hero-subtitle { color: var(--text-muted); font-size: 1rem; margin-bottom: 1.5rem; }
        .main-container { max-width: 1440px; margin: 2rem auto; padding: 0 1.5rem; }
        .filter-card { background: #ffffff; border: 1px solid var(--border-color); border-radius: 14px; padding: 1.5rem; box-shadow: 0 2px 10px rgba(0,0,0,0.02); }
        .filter-title { font-weight: 700; font-size: 0.95rem; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-dark); margin-bottom: 1rem; }
        .table-card { background: #ffffff; border: 1px solid var(--border-color); border-radius: 14px; box-shadow: 0 2px 10px rgba(0,0,0,0.02); overflow: hidden; }
        .table-header-bar { padding: 1.25rem 1.5rem; border-bottom: 1px solid var(--border-color); display: flex; justify-content: space-between; align-items: center; background: #fff; }
        .table-custom { margin-bottom: 0; white-space: nowrap; }
        .table-custom th { font-weight: 700; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); background: #f8fafc !important; border-bottom: 1px solid var(--border-color); padding: 12px 16px; }
        .table-custom td { padding: 14px 16px; vertical-align: middle; color: var(--text-dark); border-bottom: 1px solid var(--border-color); font-size: 0.9rem; }
        .table-container { max-height: 600px; overflow-y: auto; }
        .status-badge { font-weight: 700; font-size: 0.7rem; padding: 5px 10px; border-radius: 6px; letter-spacing: 0.05em; }
        .badge-pending { background: rgba(245, 158, 11, 0.15); color: #d97706; }
        .btn-green { background: var(--brand-green); color: #fff; border: none; font-weight: 600; padding: 0.5rem 1rem; border-radius: 8px; transition: background 0.2s; }
        .btn-green:hover { background: var(--brand-green-hover); color: #fff; }
        .date-badge { background: #ecfdf5; color: #059669; border: 1px solid #a7f3d0; font-weight: 700; font-size: 0.75rem; padding: 4px 10px; border-radius: 6px; }
        #loadingOverlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(4px); z-index: 9999; justify-content: center; align-items: center; flex-direction: column; gap: 15px; color: #fff; }
        .info-bar { background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 10px; padding: 0.75rem 1rem; margin-bottom: 1rem; font-size: 0.85rem; color: #166534; }
    </style>
</head>
<body>
    <div id="loadingOverlay">
        <div class="spinner-border text-light" style="width: 3.5rem; height: 3.5rem;" role="status"></div>
        <h4 class="fw-bold mt-3">Loading New Ventures...</h4>
        <p class="text-light opacity-75 small">Connecting to FMCSA live registry database...</p>
    </div>

    <nav class="navbar navbar-green navbar-expand-lg">
        <div class="container-fluid px-3">
            <a class="navbar-brand" href="#">
                <div class="logo-badge"><i class="fa-solid fa-bolt"></i></div>
                <span>GreenSearch</span>
            </a>
            <div class="d-flex align-items-center gap-4">
                <a href="#" class="nav-link active" onclick="switchTab('dashboard', event)">Dashboard</a>
                <a href="#" class="nav-link" onclick="switchTab('webhook', event)">API / Webhook</a>
                <a href="/download/csv" class="btn btn-green btn-sm" target="_blank"><i class="fa-solid fa-download me-1"></i> Export CSV</a>
            </div>
        </div>
    </nav>

    <section class="hero-section">
        <div class="container">
            <h1 class="hero-title">Look up any FMCSA new venture</h1>
            <p class="hero-subtitle">Free, public motor‑carrier data — search by name, DOT#, MC#, location, cargo, or contact.</p>
        </div>
    </section>

    <div class="main-container">
        <div id="tab-dashboard">
            <div class="row g-4">
                <div class="col-lg-3">
                    <div class="filter-card">
                        <div class="d-flex justify-content-between align-items-center mb-3">
                            <span class="filter-title mb-0">Filters</span>
                            <a href="#" class="text-success text-decoration-none small fw-semibold" onclick="resetFilters()">Reset all</a>
                        </div>

                        <div class="mb-3">
                            <label class="form-label small fw-bold text-muted">Timeframe</label>
                            <select id="daysSelect" class="form-select form-select-sm" onchange="loadData()">
                                <option value="1">Latest Batch (Previous Business Day)</option>
                                <option value="7">Last 7 Days</option>
                                <option value="14">Last 14 Days</option>
                            </select>
                        </div>

                        <div class="mb-3">
                            <label class="form-label small fw-bold text-muted">State</label>
                            <select id="stateFilter" class="form-select form-select-sm" onchange="filterTable()">
                                <option value="">All States</option>
                            </select>
                        </div>

                        <div class="mb-3">
                            <label class="form-label small fw-bold text-muted">Status</label>
                            <select id="statusFilter" class="form-select form-select-sm" onchange="filterTable()">
                                <option value="">All Statuses</option>
                                <option value="PENDING">Pending</option>
                            </select>
                        </div>

                        <div class="mt-4 pt-3 border-top">
                            <small class="text-muted d-block mb-1"><i class="fa-solid fa-circle-info me-1"></i> Active Filters</small>
                            <div class="d-flex flex-wrap gap-1" id="activeFilters">
                                <span class="date-badge"><i class="fa-solid fa-filter me-1"></i>Status: PENDING</span>
                                <span class="date-badge"><i class="fa-solid fa-truck me-1"></i>Motor Carrier of Property</span>
                            </div>
                        </div>
                    </div>
                </div>

                <div class="col-lg-9">
                    <div class="info-bar d-flex align-items-center gap-2">
                        <i class="fa-solid fa-calendar-check"></i>
                        <span id="dateInfo">Loading date range...</span>
                    </div>

                    <div class="table-card">
                        <div class="table-header-bar">
                            <div class="w-50">
                                <input type="text" id="searchInput" class="form-control form-control-sm" placeholder="Search company name, USDOT, city, email..." onkeyup="filterTable()">
                            </div>
                            <div class="d-flex gap-2">
                                <span id="resultCount" class="text-muted small fw-semibold align-self-center me-2">Showing 0 results</span>
                                <a href="/download/csv" class="btn btn-green btn-sm" target="_blank"><i class="fa-solid fa-download me-1"></i> Export CSV</a>
                            </div>
                        </div>

                        <div class="table-container">
                            <table class="table table-hover align-middle table-custom" id="venturesTable">
                                <thead class="sticky-top">
                                    <tr>
                                        <th>Carrier</th>
                                        <th>DOT #</th>
                                        <th>MC#</th>
                                        <th>Location</th>
                                        <th>Power Units</th>
                                        <th>Status</th>
                                    </tr>
                                </thead>
                                <tbody id="tableBody">
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div id="tab-webhook" class="tab-pane" style="display: none;">
            <div class="filter-card">
                <h4 class="fw-bold text-success mb-3"><i class="fa-solid fa-link me-2"></i> n8n Webhook & API Endpoints</h4>
                <p class="text-muted mb-4">Use these endpoints to integrate your GreenSearch platform into n8n or automated scripts.</p>

                <div class="mb-4">
                    <label class="form-label fw-bold small">Webhook URL (POST / GET)</label>
                    <div class="input-group shadow-sm">
                        <input type="text" class="form-control font-monospace bg-light" id="webhookUrl" value="" readonly>
                        <button class="btn btn-outline-success" onclick="copyText('webhookUrl')"><i class="fa-solid fa-copy"></i> Copy</button>
                    </div>
                    <small class="text-muted mt-1 d-block">JSON Payload: <code>{ "days": 7 }</code></small>
                </div>

                <div class="mb-4">
                    <label class="form-label fw-bold small">Direct CSV Download URL</label>
                    <div class="input-group shadow-sm">
                        <input type="text" class="form-control font-monospace bg-light" id="csvUrl" value="" readonly>
                        <button class="btn btn-outline-success" onclick="copyText('csvUrl')"><i class="fa-solid fa-copy"></i> Copy</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let allData = [];

        document.getElementById('webhookUrl').value = window.location.origin + '/webhook';
        document.getElementById('csvUrl').value = window.location.origin + '/download/csv';

        function switchTab(tabName, event) {
            if (event) event.preventDefault();
            document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
            if (event && event.currentTarget) event.currentTarget.classList.add('active');
            document.getElementById('tab-dashboard').style.display = tabName === 'dashboard' ? 'block' : 'none';
            document.getElementById('tab-webhook').style.display = tabName === 'webhook' ? 'block' : 'none';
        }

        async function loadData() {
            const days = document.getElementById('daysSelect').value;
            const overlay = document.getElementById('loadingOverlay');
            overlay.style.display = 'flex';

            try {
                const response = await fetch(`/api/data?days=${days}`);
                const result = await response.json();

                if (result.success) {
                    allData = result.data;
                    document.getElementById('dateInfo').innerHTML = 
                        `<strong>Date Range:</strong> ${result.date_range.start} → ${result.date_range.end} &nbsp;|&nbsp; <strong>Business Day Logic:</strong> ${result.date_range.note}`;
                    populateStateDropdown(allData);
                    renderTable(allData);
                } else {
                    alert('Failed to load data: ' + (result.error || 'Unknown error'));
                }
            } catch (err) {
                console.error(err);
                alert('Error connecting to server');
            } finally {
                overlay.style.display = 'none';
            }
        }

        function populateStateDropdown(data) {
            const states = [...new Set(data.map(i => i.phy_state).filter(Boolean))].sort();
            const select = document.getElementById('stateFilter');
            select.innerHTML = '<option value="">All States</option>';
            states.forEach(state => {
                select.innerHTML += `<option value="${state}">${state}</option>`;
            });
        }

        function renderTable(data) {
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';
            document.getElementById('resultCount').innerText = `Showing ${data.length} results`;

            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-5">No new ventures found for this period.</td></tr>';
                return;
            }

            data.forEach(item => {
                const row = `<tr>
                    <td>
                        <div class="fw-bold text-dark">${item.legal_name}</div>
                        ${item.dba_name && item.dba_name !== '—' ? `<small class="text-muted">d/b/a ${item.dba_name}</small>` : ''}
                        <div><span class="badge bg-light text-secondary border font-monospace" style="font-size:0.65rem;">Added: ${item.add_date}</span></div>
                    </td>
                    <td><span class="font-monospace fw-bold">${item.usdot_number}</span></td>
                    <td><span class="font-monospace">${item.docket_number}</span></td>
                    <td>${item.phy_city}, ${item.phy_state}</td>
                    <td><span class="fw-semibold">${item.power_units}</span></td>
                    <td><span class="status-badge badge-pending">${item.op_auth_status}</span></td>
                </tr>`;
                tbody.innerHTML += row;
            });
        }

        function filterTable() {
            const query = document.getElementById('searchInput').value.toLowerCase();
            const selectedState = document.getElementById('stateFilter').value;
            const selectedStatus = document.getElementById('statusFilter').value;

            const filtered = allData.filter(item => {
                const matchesQuery = (
                    (item.legal_name && item.legal_name.toLowerCase().includes(query)) ||
                    (item.usdot_number && item.usdot_number.toLowerCase().includes(query)) ||
                    (item.docket_number && item.docket_number.toLowerCase().includes(query)) ||
                    (item.phy_city && item.phy_city.toLowerCase().includes(query)) ||
                    (item.phy_state && item.phy_state.toLowerCase().includes(query)) ||
                    (item.email_address && item.email_address.toLowerCase().includes(query))
                );
                const matchesState = !selectedState || item.phy_state === selectedState;
                const matchesStatus = !selectedStatus || item.op_auth_status === selectedStatus;
                return matchesQuery && matchesState && matchesStatus;
            });
            renderTable(filtered);
        }

        function resetFilters() {
            document.getElementById('searchInput').value = '';
            document.getElementById('stateFilter').value = '';
            document.getElementById('statusFilter').value = '';
            loadData();
        }

        function copyText(elementId) {
            const copyText = document.getElementById(elementId);
            copyText.select();
            navigator.clipboard.writeText(copyText.value);
            alert("Copied to clipboard!");
        }

        window.onload = () => {
            loadData();
        };
    </script>
</body>
</html>
"""

# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/", methods=["GET", "POST"])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/data", methods=["GET"])
def api_data():
    days = request.args.get('days', default=1, type=int)
    data = fetch_new_ventures(days=days)

    start_date, end_date = get_date_range(days)

    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)

    return jsonify({
        "success": True,
        "count": len(data),
        "days_queried": days,
        "date_range": {
            "start": start_date.strftime('%Y-%m-%d'),
            "end": end_date.strftime('%Y-%m-%d'),
            "note": "Weekends auto-skip to previous Friday"
        },
        "data": data
    })

@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    days = request.args.get('days', default=1, type=int)
    data = fetch_new_ventures(days=days)
    current_dir = os.path.dirname(os.path.abspath(__file__))

    start_date, end_date = get_date_range(days)

    if data:
        df = pd.DataFrame(data)
        csv_filename = f"new_ventures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        csv_path = os.path.join(current_dir, csv_filename)
        df.to_csv(csv_path, index=False)

        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)
    else:
        csv_filename = None

    return jsonify({
        "success": True,
        "count": len(data),
        "days_queried": days,
        "date_range": {
            "start": start_date.strftime('%Y-%m-%d'),
            "end": end_date.strftime('%Y-%m-%d')
        },
        "csv_file": csv_filename,
        "download_url": "/download/csv",
        "data": data
    })

@app.route("/webhook", methods=["POST", "GET"])
def n8n_webhook():
    if request.is_json:
        data_payload = request.get_json()
        days = data_payload.get('days', 1)
    else:
        days = request.args.get('days', default=1, type=int)

    data = fetch_new_ventures(days=days)
    current_dir = os.path.dirname(os.path.abspath(__file__))

    start_date, end_date = get_date_range(days)

    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)

    return jsonify({
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "days_queried": days,
        "date_range": {
            "start": start_date.strftime('%Y-%m-%d'),
            "end": end_date.strftime('%Y-%m-%d')
        },
        "total_records": len(data),
        "carriers": data
    })

@app.route("/download/csv", methods=["GET"])
def download_csv():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
    if os.path.exists(latest_path):
        return send_file(latest_path, mimetype="text/csv", as_attachment=True, download_name="true_new_ventures_sorted.csv")
    return jsonify({"error": "No CSV file generated yet."}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

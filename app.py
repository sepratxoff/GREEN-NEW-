import os
from flask import Flask, render_template_string, request, jsonify, send_file
import requests
import pandas as pd
from datetime import datetime, timedelta
import io

app = Flask(__name__)

STATUS_CHANGE_URL = "https://data.transportation.gov/resource/dm5j-zc6c.json"
CARRIER_INFO_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"

TARGET_AUTH_TYPE = "Motor Carrier of Property (Except Household Goods)"


def get_target_date():
    """
    Returns the date we should treat as 'today' for filtering.
    Weekend fallback: if today is Saturday -> use Friday (yesterday).
                       if today is Sunday   -> use Friday (2 days back).
                       otherwise            -> use today.
    (Weekday(): Mon=0 ... Sat=5, Sun=6)
    """
    today = datetime.now()
    wd = today.weekday()
    if wd == 5:      # Saturday
        return today - timedelta(days=1)
    elif wd == 6:    # Sunday
        return today - timedelta(days=2)
    return today


def fetch_new_ventures():
    """
    Strict engine:
      1. add_date on the carrier master record (az4n-8mr2) MUST equal the
         target date (today, or the preceding Friday on weekends) in
         YYYYMMDD format - exact match, not a prefix/range.
      2. op_auth_status MUST equal 'Pending' exactly.
      3. op_auth_type MUST equal 'Motor Carrier of Property (Except Household
         Goods)' exactly.
      Both (2) and (3) are re-validated in Python after the API call, so a
      malformed/ignored $where clause can never let a mismatched record
      (e.g. a Broker record) slip through silently.
    """
    target_date = get_target_date()
    target_date_str = target_date.strftime("%Y%m%d")
    print(f"[*] Target date: {target_date_str} ({target_date.strftime('%A')})")

    # ---- Step 1: pull status-change records that are Pending + correct auth type ----
    # Bounded window: only look forward from the target date (status changes can't
    # predate the carrier's add_date), which keeps the result set small.
    all_status_changes = []
    limit = 1000
    offset = 0
    while True:
        params = {
            "$where": (
                f"status_change_date >= '{target_date_str}' "
                f"AND op_auth_type = '{TARGET_AUTH_TYPE}' "
                f"AND op_auth_status = 'Pending'"
            ),
            "$limit": limit,
            "$offset": offset,
            "$order": "status_change_date DESC",
        }
        try:
            resp = requests.get(STATUS_CHANGE_URL, params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[!] Error fetching status changes: {e}")
            break

        if not batch:
            break
        all_status_changes.extend(batch)
        if len(batch) < limit:
            break
        offset += limit

    print(f"[+] Raw status-change rows fetched: {len(all_status_changes)}")

    # ---- Strict re-validation in Python (never trust the API filter blindly) ----
    status_map = {}
    for sc in all_status_changes:
        if sc.get("op_auth_status") != "Pending":
            continue
        if sc.get("op_auth_type") != TARGET_AUTH_TYPE:
            continue
        dot = sc.get("usdot_number")
        if dot and dot not in status_map:
            status_map[dot] = sc

    unique_dots = list(status_map.keys())
    print(f"[*] Unique DOTs after strict Pending+auth_type check: {len(unique_dots)}")

    if not unique_dots:
        return []

    # ---- Step 2: fetch carrier master profiles for those DOTs ----
    carrier_records = []
    batch_size = 50
    for i in range(0, len(unique_dots), batch_size):
        batch_dots = unique_dots[i : i + batch_size]
        dots_str = ",".join([f"'{d}'" for d in batch_dots])
        params = {"$where": f"dot_number in ({dots_str})", "$limit": batch_size}
        try:
            resp = requests.get(CARRIER_INFO_URL, params=params, timeout=30)
            resp.raise_for_status()
            carrier_records.extend(resp.json())
        except Exception as e:
            print(f"[!] Error fetching carrier batch: {e}")

    # ---- Step 3: strict merge - add_date must equal target date EXACTLY ----
    verified_ventures = []
    for cd in carrier_records:
        dot = cd.get("dot_number")
        sc = status_map.get(dot)
        if not sc:
            continue

        add_date = cd.get("add_date", "")
        if add_date != target_date_str:
            continue

        legal_name = (cd.get("legal_name") or "").strip()
        if not legal_name:
            continue

        docket = sc.get("docket_number") or cd.get("docket1", "")
        if docket and cd.get("docket1prefix") and not str(docket).startswith(cd.get("docket1prefix")):
            docket = f"{cd.get('docket1prefix')}{docket}"

        merged = {
            "usdot_number": dot,
            "docket_number": docket or "\u2014",
            "legal_name": legal_name,
            "dba_name": cd.get("dba_name") or "\u2014",
            "add_date": add_date,
            "status_change_date": sc.get("status_change_date") or add_date,
            "op_auth_status": sc.get("op_auth_status"),
            "reason": sc.get("reason") or "Initial Registration",
            "op_auth_type": sc.get("op_auth_type"),
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

    verified_ventures.sort(key=lambda x: x["status_change_date"], reverse=True)
    print(f"[+] Final strict-match new ventures: {len(verified_ventures)}")
    return verified_ventures


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
        .badge-active { background: rgba(16, 185, 129, 0.15); color: #059669; }
        .badge-pending { background: rgba(245, 158, 11, 0.15); color: #d97706; }
        .btn-green { background: var(--brand-green); color: #fff; border: none; font-weight: 600; padding: 0.5rem 1rem; border-radius: 8px; transition: background 0.2s; }
        .btn-green:hover { background: var(--brand-green-hover); color: #fff; }
        #loadingOverlay { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(15, 23, 42, 0.75); backdrop-filter: blur(4px); z-index: 9999; justify-content: center; align-items: center; flex-direction: column; gap: 15px; color: #fff; }
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
            <h1 class="hero-title">Today's FMCSA new ventures</h1>
            <p class="hero-subtitle">Strict match: add_date = today (Fri on weekends) &middot; op_auth_status = Pending &middot; op_auth_type = Motor Carrier of Property (Except Household Goods)</p>
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
                            <label class="form-label small fw-bold text-muted">State</label>
                            <select id="stateFilter" class="form-select form-select-sm" onchange="filterTable()">
                                <option value="">All States</option>
                            </select>
                        </div>
                    </div>
                </div>

                <div class="col-lg-9">
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
                                <tbody id="tableBody"></tbody>
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
            const overlay = document.getElementById('loadingOverlay');
            overlay.style.display = 'flex';
            try {
                const response = await fetch(`/api/data`);
                const result = await response.json();
                if (result.success) {
                    allData = result.data;
                    populateStateDropdown(allData);
                    renderTable(allData);
                } else {
                    alert('Failed to load data');
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
            states.forEach(state => { select.innerHTML += `<option value="${state}">${state}</option>`; });
        }

        function renderTable(data) {
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';
            document.getElementById('resultCount').innerText = `Showing ${data.length} results`;
            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted py-5">No new ventures found for today.</td></tr>';
                return;
            }
            data.forEach(item => {
                const row = `<tr>
                    <td>
                        <div class="fw-bold text-dark">${item.legal_name}</div>
                        ${item.dba_name && item.dba_name !== '\u2014' ? `<small class="text-muted">d/b/a ${item.dba_name}</small>` : ''}
                        <div><span class="badge bg-light text-secondary border font-monospace" style="font-size:0.65rem;">${item.add_date}</span></div>
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
                return matchesQuery && matchesState;
            });
            renderTable(filtered);
        }

        function resetFilters() {
            document.getElementById('searchInput').value = '';
            document.getElementById('stateFilter').value = '';
            loadData();
        }

        function copyText(elementId) {
            const copyText = document.getElementById(elementId);
            copyText.select();
            navigator.clipboard.writeText(copyText.value);
            alert("Copied to clipboard!");
        }

        window.onload = () => { loadData(); };
    </script>
</body>
</html>
"""


@app.route("/", methods=["GET", "POST"])
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/data", methods=["GET"])
def api_data():
    data = fetch_new_ventures()
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        df.to_csv(os.path.join(current_dir, "new_ventures_latest.csv"), index=False)
    return jsonify({"success": True, "count": len(data), "data": data})


@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    data = fetch_new_ventures()
    current_dir = os.path.dirname(os.path.abspath(__file__))
    csv_filename = None
    if data:
        df = pd.DataFrame(data)
        csv_filename = f"new_ventures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.to_csv(os.path.join(current_dir, csv_filename), index=False)
        df.to_csv(os.path.join(current_dir, "new_ventures_latest.csv"), index=False)
    return jsonify({
        "success": True,
        "count": len(data),
        "csv_file": csv_filename,
        "download_url": "/download/csv",
        "data": data,
    })


@app.route("/webhook", methods=["POST", "GET"])
def n8n_webhook():
    data = fetch_new_ventures()
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        df.to_csv(os.path.join(current_dir, "new_ventures_latest.csv"), index=False)
    return jsonify({
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "total_records": len(data),
        "carriers": data,
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

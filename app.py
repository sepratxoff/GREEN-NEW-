import os
from flask import Flask, render_template_string, request, jsonify, send_file
import requests
import json
import pandas as pd
from datetime import datetime, timedelta
import math

app = Flask(__name__)

STATUS_CHANGE_URL = "https://data.transportation.gov/resource/dm5j-zc6c.json"
CARRIER_INFO_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"

def fetch_true_new_ventures(days=7):
    today = datetime.now()
    start_date = (today - timedelta(days=days)).strftime('%Y%m%d')
    print(f"[*] Fetching carriers added between {start_date} and today (last {days} days)...")
    
    all_carriers = []
    limit = 500
    offset = 0
    
    while True:
        params = {
            "$where": f"add_date >= '{start_date}'",
            "$limit": limit,
            "$offset": offset,
            "$order": "add_date DESC"
        }
        try:
            resp = requests.get(CARRIER_INFO_URL, params=params)
            resp.raise_for_status()
            batch = resp.json()
        except Exception as e:
            print(f"[!] Error fetching carrier details: {e}")
            break
            
        if not batch:
            break
        all_carriers.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
        
    print(f"[+] Found {len(all_carriers)} carriers added in the last {days} days.")
    if not all_carriers:
        return []

    usdot_list = [c.get("dot_number") for c in all_carriers if c.get("dot_number")]
    
    status_map = {}
    batch_size = 50
    for i in range(0, len(usdot_list), batch_size):
        batch_dots = usdot_list[i:i+batch_size]
        dots_str = ",".join([f"'{d}'" for d in batch_dots])
        params = {
            "$where": f"usdot_number in ({dots_str})",
            "$limit": batch_size
        }
        try:
            resp = requests.get(STATUS_CHANGE_URL, params=params)
            resp.raise_for_status()
            for sc in resp.json():
                dot = sc.get("usdot_number")
                if dot:
                    status_map[dot] = sc
        except Exception as e:
            print(f"[!] Error fetching status changes batch: {e}")

    combined = []
    for cd in all_carriers:
        dot = cd.get("dot_number")
        sc = status_map.get(dot, {})
        
        merged = {
            "usdot_number": dot,
            "docket_number": sc.get("docket_number") or cd.get("docket1") or "",
            "legal_name": cd.get("legal_name") or "",
            "dba_name": cd.get("dba_name") or "",
            "add_date": cd.get("add_date") or "",
            "status_change_date": sc.get("status_change_date") or "",
            "op_auth_status": sc.get("op_auth_status") or ("Active" if cd.get("status_code") == "A" else "Pending"),
            "reason": sc.get("reason") or "Initial Status",
            "op_auth_type": sc.get("op_auth_type") or "",
            "phone": cd.get("phone") or cd.get("cell_phone") or "",
            "email_address": cd.get("email_address") or "",
            "phy_street": cd.get("phy_street") or "",
            "phy_city": cd.get("phy_city") or "",
            "phy_state": cd.get("phy_state") or "",
            "phy_zip": cd.get("phy_zip") or "",
            "power_units": cd.get("power_units") or "1",
            "classdef": cd.get("classdef") or "",
        }
        combined.append(merged)

    combined.sort(key=lambda x: x["add_date"] if x["add_date"] else "", reverse=True)
    return combined

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>New Ventures FMCSA Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/assets/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
    <style>
        body { background-color: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .sidebar { background: #212529; min-height: 100vh; color: white; }
        .sidebar a { color: #adb5bd; text-decoration: none; padding: 10px 20px; display: block; border-radius: 5px; margin-bottom: 5px; }
        .sidebar a:hover, .sidebar a.active { background: #0d6efd; color: white; }
        .card-stat { border: none; border-radius: 10px; box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); }
        .table-container { background: white; border-radius: 10px; box-shadow: 0 0.125rem 0.25rem rgba(0, 0, 0, 0.075); padding: 20px; }
        .loader { display: none; width: 3rem; height: 3rem; }
    </style>
</head>
<body>
    <div class="container-fluid">
        <div class="row">
            <!-- Sidebar -->
            <div class="col-md-2 sidebar p-4 d-flex flex-column justify-content-between">
                <div>
                    <h4 class="text-white mb-4"><i class="fa-solid fa-truck-fast text-primary"></i> New Ventures</h4>
                    <hr class="text-secondary">
                    <a href="#" class="active"><i class="fa-solid fa-chart-line me-2"></i> Dashboard</a>
                    <a href="/download/csv" target="_blank"><i class="fa-solid fa-download me-2"></i> Download CSV</a>
                </div>
                <div class="text-secondary small">
                    <p class="mb-0">FMCSA Data Platform v2.0</p>
                    <p class="mb-0 text-success"><i class="fa-solid fa-circle fa-2xs"></i> Cloud Ready</p>
                </div>
            </div>

            <!-- Main Content -->
            <div class="col-md-10 p-5">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <h2>True New Ventures Dashboard</h2>
                    <div class="d-flex gap-2">
                        <select id="daysSelect" class="form-select" style="width: 150px;">
                            <option value="1">Last 1 Day</option>
                            <option value="3">Last 3 Days</option>
                            <option value="7" selected>Last 7 Days</option>
                            <option value="14">Last 14 Days</option>
                            <option value="30">Last 30 Days</option>
                        </select>
                        <button id="fetchBtn" class="btn btn-primary" onclick="loadData()">
                            <i class="fa-solid fa-rotate me-1"></i> Fetch Data
                        </button>
                        <a href="/download/csv" class="btn btn-success">
                            <i class="fa-solid fa-file-excel me-1"></i> Export CSV
                        </a>
                    </div>
                </div>

                <!-- Stats Cards -->
                <div class="row mb-4">
                    <div class="col-md-4">
                        <div class="card card-stat p-3 bg-white">
                            <h6 class="text-muted">Total New Ventures</h6>
                            <h3 id="statTotal" class="fw-bold text-primary">0</h3>
                        </div>
                    </div>
                    <div class="col-md-4">
                        <div class="card card-stat p-3 bg-white">
                            <h6 class="text-muted">Active Authorities</h6>
                            <h3 id="statActive" class="fw-bold text-success">0</h3>
                        </div>
                    </div>
                    <div class="col-md-4">
                        <div class="card card-stat p-3 bg-white">
                            <h6 class="text-muted">States Covered</h6>
                            <h3 id="statStates" class="fw-bold text-info">0</h3>
                        </div>
                    </div>
                </div>

                <!-- Loading Spinner -->
                <div id="loading" class="text-center py-5">
                    <div class="spinner-border text-primary loader" role="status"></div>
                    <p class="mt-3 text-muted">Fetching and verifying true new ventures from FMCSA database...</p>
                </div>

                <!-- Table -->
                <div class="table-container" id="tableContainer" style="display: none;">
                    <div class="mb-3">
                        <input type="text" id="searchInput" class="form-control" placeholder="Search by company name, USDOT, city or state..." onkeyup="filterTable()">
                    </div>
                    <div class="table-responsive" style="max-height: 600px; overflow-y: auto;">
                        <table class="table table-hover align-middle" id="venturesTable">
                            <thead class="table-dark sticky-top">
                                <tr>
                                    <th>USDOT</th>
                                    <th>Legal Name</th>
                                    <th>Add Date</th>
                                    <th>Status</th>
                                    <th>Phone</th>
                                    <th>Email</th>
                                    <th>Location</th>
                                    <th>Units</th>
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

    <script>
        let allData = [];

        async function loadData() {
            const days = document.getElementById('daysSelect').value;
            const loading = document.getElementById('loading');
            const tableContainer = document.getElementById('tableContainer');
            const fetchBtn = document.getElementById('fetchBtn');

            loading.style.display = 'block';
            tableContainer.style.display = 'none';
            fetchBtn.disabled = true;

            try {
                const response = await fetch(`/api/data?days=${days}`);
                const result = await response.json();
                
                if (result.success) {
                    allData = result.data;
                    renderTable(allData);
                    updateStats(allData);
                    tableContainer.style.display = 'block';
                } else {
                    alert('Failed to load data');
                }
            } catch (err) {
                console.error(err);
                alert('Error connecting to server');
            } finally {
                loading.style.display = 'none';
                fetchBtn.disabled = false;
            }
        }

        function renderTable(data) {
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';

            if (data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">No new ventures found for this period.</td></tr>';
                return;
            }

            data.forEach(item => {
                const row = `<tr>
                    <td><strong>${item.usdot_number}</strong></td>
                    <td>${item.legal_name} <br><small class="text-muted">${item.dba_name ? 'DBA: ' + item.dba_name : ''}</small></td>
                    <td><span class="badge bg-secondary">${item.add_date}</span></td>
                    <td><span class="badge bg-${item.op_auth_status === 'Active' ? 'success' : 'warning'}">${item.op_auth_status}</span></td>
                    <td>${item.phone}</td>
                    <td><a href="mailto:${item.email_address}">${item.email_address}</a></td>
                    <td>${item.phy_city}, ${item.phy_state} ${item.phy_zip}</td>
                    <td><span class="badge bg-dark">${item.power_units}</span></td>
                </tr>`;
                tbody.innerHTML += row;
            });
        }

        function updateStats(data) {
            document.getElementById('statTotal').innerText = data.length;
            const activeCount = data.filter(i => i.op_auth_status === 'Active').length;
            document.getElementById('statActive').innerText = activeCount;
            const states = new Set(data.map(i => i.phy_state)).size;
            document.getElementById('statStates').innerText = states;
        }

        function filterTable() {
            const query = document.getElementById('searchInput').value.toLowerCase();
            const filtered = allData.filter(item => 
                (item.legal_name && item.legal_name.toLowerCase().includes(query)) ||
                (item.usdot_number && item.usdot_number.toLowerCase().includes(query)) ||
                (item.phy_city && item.phy_city.toLowerCase().includes(query)) ||
                (item.phy_state && item.phy_state.toLowerCase().includes(query))
            );
            renderTable(filtered);
        }

        window.onload = () => {
            loadData();
        };
    </script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/data", methods=["GET"])
def api_data():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7

    data = fetch_true_new_ventures(days=days)
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)

    return jsonify({
        "success": True,
        "count": len(data),
        "days_queried": days,
        "data": data
    })

@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    try:
        days = int(request.args.get("days", 7))
    except ValueError:
        days = 7

    data = fetch_true_new_ventures(days=days)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        csv_filename = f"true_new_ventures_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
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
        "csv_file": csv_filename,
        "download_url": "/download/csv",
        "data": data
    })

@app.route("/webhook", methods=["POST", "GET"])
def n8n_webhook():
    req_data = request.json or request.args.to_dict()
    try:
        days = int(req_data.get("days", 7))
    except (ValueError, TypeError):
        days = 7

    data = fetch_true_new_ventures(days=days)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)

    return jsonify({
        "success": True,
        "timestamp": datetime.now().isoformat(),
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

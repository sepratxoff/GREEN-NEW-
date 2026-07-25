import os
from flask import Flask, render_template_string, request, jsonify, send_file
import requests
import json
import pandas as pd
from datetime import datetime, timedelta
import io

app = Flask(__name__)

CARRIER_INFO_URL = "https://data.transportation.gov/resource/az4n-8mr2.json"
STATUS_CHANGE_URL = "https://data.transportation.gov/resource/dm5j-zc6c.json"

def get_date_window(days):
    """
    Calculate start and end dates for the window based on add_date.
    - Last 1 Day: ONLY the previous calendar day (yesterday)
    - Last N Days (N>1): from (today - N days) to today inclusive
    
    Returns (start_date_str, end_date_str) in YYYYMMDD format.
    """
    today = datetime.now()
    end_date = today.strftime('%Y%m%d')
    
    if days == 1:
        # Strictly yesterday only (as per user example: 25th → 24th)
        start_date = (today - timedelta(days=1)).strftime('%Y%m%d')
    else:
        # For 3,7,14,30 days: go back N days (inclusive)
        start_date = (today - timedelta(days=days)).strftime('%Y%m%d')
    
    return start_date, end_date

def fetch_true_new_ventures(days=3):
    """
    Fetch carriers strictly based on add_date within the requested time window.
    This is the CORRECT approach: filter on add_date from carrier master data.
    
    Rules:
      - Last 1 Day: Only yesterday's add_date (previous calendar day)
      - Last N Days: add_date >= (today - N days)
    
    Results are sorted DESCENDING by add_date (latest first) for best "recent first" experience.
    """
    start_date, end_date = get_date_window(days)
    
    print(f"[*] Fetching new ventures: add_date >= {start_date} (window: {start_date} → {end_date}) | days={days}")

    all_carriers = []
    limit = 1000
    offset = 0

    # Direct query on CARRIER master table using add_date (no status needed for filtering)
    where_clause = f"add_date >= '{start_date}'"
    
    while True:
        params = {
            "$where": where_clause,
            "$limit": limit,
            "$offset": offset,
            "$order": "add_date DESC, dot_number DESC"
        }
        try:
            resp = requests.get(CARRIER_INFO_URL, params=params)
            resp.raise_for_status()
            batch = resp.json()
            all_carriers.extend(batch)
            print(f"    Batch: {len(batch)} records (offset {offset})")
            if len(batch) < limit:
                break
            offset += limit
        except Exception as e:
            print(f"[!] Error fetching carrier batch: {e}")
            break

    print(f"[+] Retrieved {len(all_carriers)} carriers with add_date >= {start_date}")

    if not all_carriers:
        return []

    # Enrich with recent status changes (optional, for better "new entrant" info)
    dots = [c.get("dot_number") for c in all_carriers if c.get("dot_number")]
    status_map = {}

    if dots:
        print(f"[*] Fetching status changes for {len(dots)} carriers...")
        batch_size = 100
        for i in range(0, len(dots), batch_size):
            batch_dots = dots[i:i + batch_size]
            dots_str = ",".join([f"'{d}'" for d in batch_dots])
            try:
                sc_params = {
                    "$where": f"usdot_number in ({dots_str}) AND status_change_date >= '{start_date}'",
                    "$limit": 2000
                }
                resp = requests.get(STATUS_CHANGE_URL, params=sc_params)
                if resp.status_code == 200:
                    for sc in resp.json():
                        dot = sc.get("usdot_number")
                        if dot and dot not in status_map:
                            status_map[dot] = sc
            except Exception as e:
                print(f"[!] Status batch error (non-fatal): {e}")

    # Build final dataset — strictly filtered by add_date
    combined = []
    for cd in all_carriers:
        add_date = cd.get("add_date", "")
        
        # Strict add_date window filter
        if not add_date or add_date < start_date:
            continue

        dot = cd.get("dot_number")
        sc = status_map.get(dot, {})

        merged = {
            "usdot_number": dot or "",
            "docket_number": sc.get("docket_number") or cd.get("docket1") or "",
            "legal_name": cd.get("legal_name") or "",
            "dba_name": cd.get("dba_name") or "",
            "add_date": add_date,
            "status_change_date": sc.get("status_change_date", ""),
            "op_auth_status": sc.get("op_auth_status") or ("Active" if cd.get("status_code") == "A" else "Pending"),
            "reason": sc.get("reason") or "New DOT / Initial Authority",
            "op_auth_type": sc.get("op_auth_type") or "Motor Carrier of Property",
            "phone": cd.get("phone") or cd.get("cell_phone") or "N/A",
            "email_address": cd.get("email_address") or "N/A",
            "phy_street": cd.get("phy_street") or "",
            "phy_city": cd.get("phy_city") or "",
            "phy_state": cd.get("phy_state") or "",
            "phy_zip": cd.get("phy_zip") or "",
            "power_units": int(cd.get("power_units") or cd.get("truck_units") or 1),
            "classdef": cd.get("classdef") or "",
        }
        combined.append(merged)

    # Sort DESCENDING by add_date (latest first) - this is the best ordering for "pulls in the latest"
    # Secondary sort by status_change_date if present
    combined.sort(key=lambda x: (x["add_date"], x.get("status_change_date") or ""), reverse=True)

    print(f"[+] Final result: {len(combined)} carriers added between {start_date} and {end_date}")
    return combined


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FMCSA True New Ventures Intelligence Platform</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.2/css/all.min.css" rel="stylesheet">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&amp;display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bs-body-font-family: 'Inter', sans-serif;
            --sidebar-width: 260px;
            --primary-color: #4f46e5;
            --bg-color: #f8fafc;
        }
        body { background-color: var(--bg-color); color: #1e293b; overflow-x: hidden; }
        .sidebar {
            width: var(--sidebar-width);
            height: 100vh;
            position: fixed;
            top: 0; left: 0;
            background: #0f172a;
            color: #94a3b8;
            z-index: 1000;
            box-shadow: 4px 0 10px rgba(0,0,0,0.05);
        }
        .sidebar-brand {
            font-size: 1.25rem; font-weight: 700; color: #fff;
            padding: 1.5rem 1.25rem; display: flex; align-items: center; gap: 10px;
            border-bottom: 1px solid #1e293b;
        }
        .sidebar-menu { padding: 1.25rem 0.75rem; list-style: none; margin: 0; }
        .sidebar-menu li { margin-bottom: 0.5rem; }
        .sidebar-menu a {
            display: flex; align-items: center; gap: 12px;
            padding: 10px 14px; color: #94a3b8; text-decoration: none;
            font-weight: 500; border-radius: 8px; transition: all 0.2s ease;
        }
        .sidebar-menu a:hover, .sidebar-menu a.active {
            background: var(--primary-color); color: #fff;
            box-shadow: 0 4px 12px rgba(79, 70, 229, 0.3);
        }
        .main-content { margin-left: var(--sidebar-width); padding: 2.5rem; min-height: 100vh; }
        .card-stat {
            background: #fff; border: none; border-radius: 14px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.03); position: relative; overflow: hidden;
        }
        .card-stat::after {
            content: ''; position: absolute; top: 0; left: 0; width: 4px; height: 100%;
            background: var(--primary-color);
        }
        .panel-box {
            background: #fff; border: none; border-radius: 16px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.03); padding: 1.75rem; margin-bottom: 2rem;
        }
        .table-custom th {
            font-weight: 600; color: #475569; background: #f8fafc !important;
            border-bottom: 2px solid #e2e8f0; padding: 12px 16px; white-space: nowrap;
        }
        .table-custom td { padding: 14px 16px; vertical-align: middle; color: #334155; white-space: nowrap; }
        .table-container { max-height: 650px; overflow-y: auto; border-radius: 12px; border: 1px solid #e2e8f0; }
        #loadingOverlay {
            display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(255, 255, 255, 0.85); z-index: 9999;
            justify-content: center; align-items: center; flex-direction: column; gap: 15px;
        }
        .badge-status { padding: 6px 12px; border-radius: 20px; font-weight: 500; font-size: 0.75rem; }
        @media (max-width: 992px) {
            .sidebar { width: 70px; }
            .sidebar .sidebar-brand span, .sidebar .sidebar-menu span, .sidebar .sidebar-footer { display: none; }
            .main-content { margin-left: 70px; padding: 1.5rem; }
        }
    </style>
</head>
<body>
    <div id="loadingOverlay">
        <div class="spinner-border text-indigo" style="width: 3.5rem; height: 3.5rem; color: var(--primary-color);" role="status"></div>
        <h5 class="fw-semibold text-dark mt-2">Querying FMCSA New Entrant Program Database...</h5>
        <p class="text-muted small">Fetching newly issued USDOTs and Initial Status authorities...</p>
    </div>

    <div class="sidebar d-flex flex-column justify-content-between">
        <div>
            <div class="sidebar-brand">
                <i class="fa-solid fa-truck-fast text-indigo" style="color: #6366f1;"></i>
                <span>NewVentures</span>
            </div>
            <ul class="sidebar-menu">
                <li><a href="#" class="active" onclick="switchTab('dashboard', event)"><i class="fa-solid fa-chart-pie fa-fw"></i> <span>Dashboard</span></a></li>
                <li><a href="#" onclick="switchTab('analytics', event)"><i class="fa-solid fa-chart-column fa-fw"></i> <span>Analytics</span></a></li>
                <li><a href="#" onclick="switchTab('webhook', event)"><i class="fa-solid fa-bolt fa-fw"></i> <span>n8n Webhooks</span></a></li>
                <li><a href="/download/csv" target="_blank"><i class="fa-solid fa-file-csv fa-fw"></i> <span>Export CSV</span></a></li>
                <li><a href="/download/excel" target="_blank"><i class="fa-solid fa-file-excel fa-fw"></i> <span>Export Excel</span></a></li>
            </ul>
        </div>
        <div class="p-3 m-3 rounded bg-dark border border-secondary text-center sidebar-footer">
            <small class="text-success fw-bold"><i class="fa-solid fa-circle fa-2xs me-1"></i> New Entrant Verified</small>
        </div>
    </div>

    <div class="main-content">
        <div id="tab-dashboard" class="tab-pane">
            <div class="d-flex flex-wrap justify-content-between align-items-center gap-3 mb-4">
                <div>
                    <h2 class="fw-bold mb-1">True New Ventures (New Entrant Program)</h2>
                    <p class="text-muted mb-0">Carriers getting their USDOT or Initial Authority status for the first time (filtered by add_date).</p>
                </div>
                <div class="d-flex align-items-center gap-2">
                    <select id="daysSelect" class="form-select shadow-sm" style="width: 150px;">
                        <option value="1">Last 1 Day</option>
                        <option value="3" selected>Last 3 Days</option>
                        <option value="7">Last 7 Days</option>
                        <option value="14">Last 14 Days</option>
                        <option value="30">Last 30 Days</option>
                    </select>
                    <button class="btn btn-primary shadow-sm px-4 d-flex align-items-center gap-2" onclick="loadData()" style="background-color: var(--primary-color); border: none;">
                        <i class="fa-solid fa-rotate"></i> Refresh
                    </button>
                </div>
            </div>

            <div class="row g-4 mb-4">
                <div class="col-md-3">
                    <div class="card card-stat p-4">
                        <span class="text-muted small fw-semibold text-uppercase">New Entrants</span>
                        <h2 id="statTotal" class="fw-bold mt-2 mb-0 text-dark">0</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card card-stat p-4" style="--primary-color: #10b981;">
                        <span class="text-muted small fw-semibold text-uppercase">Active / Pending</span>
                        <h2 id="statActive" class="fw-bold mt-2 mb-0 text-success">0</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card card-stat p-4" style="--primary-color: #0ea5e9;">
                        <span class="text-muted small fw-semibold text-uppercase">States Covered</span>
                        <h2 id="statStates" class="fw-bold mt-2 mb-0 text-info">0</h2>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="card card-stat p-4" style="--primary-color: #f59e0b;">
                        <span class="text-muted small fw-semibold text-uppercase">Total Power Units</span>
                        <h2 id="statUnits" class="fw-bold mt-2 mb-0 text-warning">0</h2>
                    </div>
                </div>
            </div>

            <div class="panel-box">
                <div class="row g-3 mb-4">
                    <div class="col-md-6">
                        <div class="input-group shadow-sm">
                            <span class="input-group-text bg-white border-end-0"><i class="fa-solid fa-search text-muted"></i></span>
                            <input type="text" id="searchInput" class="form-control border-start-0" placeholder="Search company name, USDOT, city, email..." onkeyup="filterTable()">
                        </div>
                    </div>
                    <div class="col-md-3">
                        <select id="stateFilter" class="form-select shadow-sm" onchange="filterTable()">
                            <option value="">All States</option>
                        </select>
                    </div>
                    <div class="col-md-3">
                        <select id="statusFilter" class="form-select shadow-sm" onchange="filterTable()">
                            <option value="">All Statuses</option>
                            <option value="Pending">Pending</option>
                            <option value="Active">Active</option>
                        </select>
                    </div>
                </div>
                <div class="table-container">
                    <table class="table table-hover align-middle table-custom mb-0" id="venturesTable">
                        <thead class="sticky-top">
                            <tr>
                                <th>USDOT / Docket</th>
                                <th>Company Name</th>
                                <th>Add Date</th>
                                <th>Status / Reason</th>
                                <th>Contact Information</th>
                                <th>Location</th>
                                <th>Power Units</th>
                            </tr>
                        </thead>
                        <tbody id="tableBody"></tbody>
                    </table>
                </div>
            </div>
        </div>

        <div id="tab-analytics" class="tab-pane" style="display: none;">
            <h2 class="fw-bold mb-4">New Ventures Analytics &amp; Insights</h2>
            <div class="row g-4">
                <div class="col-lg-6">
                    <div class="panel-box">
                        <h5 class="fw-bold mb-3">Top 10 States for New Registrations</h5>
                        <canvas id="stateChart" height="250"></canvas>
                    </div>
                </div>
                <div class="col-lg-6">
                    <div class="panel-box">
                        <h5 class="fw-bold mb-3">Registration Volume by Date</h5>
                        <canvas id="dateChart" height="250"></canvas>
                    </div>
                </div>
            </div>
        </div>

        <div id="tab-webhook" class="tab-pane" style="display: none;">
            <h2 class="fw-bold mb-4">n8n Automation &amp; Webhook Integration</h2>
            <div class="panel-box">
                <h4 class="fw-bold text-indigo mb-3" style="color: var(--primary-color);"><i class="fa-solid fa-link me-2"></i> Platform Endpoints</h4>
                <p class="text-muted mb-4">Connect these endpoints into n8n or automated scripts to fetch verified new ventures and export data automatically.</p>
                
                <div class="mb-4">
                    <label class="form-label fw-bold">Webhook URL (POST / GET)</label>
                    <div class="input-group shadow-sm">
                        <input type="text" class="form-control font-monospace bg-light" id="webhookUrl" value="" readonly>
                        <button class="btn btn-outline-primary" onclick="copyText('webhookUrl')"><i class="fa-solid fa-copy"></i> Copy</button>
                    </div>
                    <small class="text-muted mt-1 d-block">JSON Payload: <code>{ "days": 3 }</code></small>
                </div>
                <div class="mb-4">
                    <label class="form-label fw-bold">Direct CSV Download URL</label>
                    <div class="input-group shadow-sm">
                        <input type="text" class="form-control font-monospace bg-light" id="csvUrl" value="" readonly>
                        <button class="btn btn-outline-primary" onclick="copyText('csvUrl')"><i class="fa-solid fa-copy"></i> Copy</button>
                    </div>
                </div>
                <div class="mb-3">
                    <label class="form-label fw-bold">Direct Excel Download URL</label>
                    <div class="input-group shadow-sm">
                        <input type="text" class="form-control font-monospace bg-light" id="excelUrl" value="" readonly>
                        <button class="btn btn-outline-primary" onclick="copyText('excelUrl')"><i class="fa-solid fa-copy"></i> Copy</button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js"></script>
    <script>
        let allData = [];
        let stateChartInstance = null;
        let dateChartInstance = null;

        function setWebhookUrls() {
            const origin = window.location.origin;
            document.getElementById('webhookUrl').value = origin + '/webhook';
            document.getElementById('csvUrl').value = origin + '/download/csv';
            document.getElementById('excelUrl').value = origin + '/download/excel';
        }

        function switchTab(tabName, event) {
            if (event) event.preventDefault();
            document.querySelectorAll('.sidebar-menu a').forEach(el => el.classList.remove('active'));
            if (event && event.currentTarget) event.currentTarget.classList.add('active');
            
            document.getElementById('tab-dashboard').style.display = tabName === 'dashboard' ? 'block' : 'none';
            document.getElementById('tab-analytics').style.display = tabName === 'analytics' ? 'block' : 'none';
            document.getElementById('tab-webhook').style.display = tabName === 'webhook' ? 'block' : 'none';
            
            if (tabName === 'analytics') {
                renderCharts();
            }
        }

        async function loadData() {
            const days = document.getElementById('daysSelect').value;
            const overlay = document.getElementById('loadingOverlay');
            overlay.style.display = 'flex';
            
            try {
                const response = await fetch(`/api/data?days=${days}`);
                const result = await response.json();
                
                if (result.success) {
                    allData = result.data || [];
                    populateStateDropdown(allData);
                    renderTable(allData);
                    updateStats(allData);
                } else {
                    alert('Failed to load data from server');
                }
            } catch (err) {
                console.error(err);
                alert('Error connecting to server. Check console for details.');
            } finally {
                overlay.style.display = 'none';
            }
        }

        function populateStateDropdown(data) {
            const states = [...new Set(data.map(i => i.phy_state).filter(Boolean))].sort();
            const select = document.getElementById('stateFilter');
            select.innerHTML = '<option value="">All States</option>';
            states.forEach(state => {
                const opt = document.createElement('option');
                opt.value = state;
                opt.textContent = state;
                select.appendChild(opt);
            });
        }

        function renderTable(data) {
            const tbody = document.getElementById('tableBody');
            tbody.innerHTML = '';
            
            if (!data || data.length === 0) {
                tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted py-5">No new entrant ventures found for this period (based on add_date).</td></tr>';
                return;
            }
            
            data.forEach(item => {
                const isPending = item.op_auth_status === 'Pending' || item.reason.includes('Initial');
                const badgeClass = isPending ? 'bg-primary bg-opacity-10 text-primary' : 'bg-success bg-opacity-10 text-success';
                
                const row = `
                    <tr>
                        <td>
                            <div class="fw-bold">${item.usdot_number}</div>
                            <small class="text-muted">Docket: ${item.docket_number || 'N/A'}</small>
                        </td>
                        <td>
                            <div class="fw-bold text-dark">${item.legal_name || 'N/A'}</div>
                            ${item.dba_name ? `<small class="text-muted">DBA: ${item.dba_name}</small>` : ''}
                        </td>
                        <td><span class="badge bg-light text-dark border">${item.add_date}</span></td>
                        <td>
                            <span class="badge badge-status ${badgeClass}">${item.op_auth_status}</span>
                            <div class="small text-muted mt-1">${item.reason}</div>
                        </td>
                        <td>
                            <div><i class="fa-solid fa-phone text-muted me-1 small"></i> ${item.phone}</div>
                            <div><i class="fa-solid fa-envelope text-muted me-1 small"></i> <a href="mailto:${item.email_address}" class="text-decoration-none">${item.email_address}</a></div>
                        </td>
                        <td>${item.phy_city}, ${item.phy_state} ${item.phy_zip}</td>
                        <td><span class="badge bg-dark bg-opacity-10 text-dark px-3 py-2">${item.power_units} Units</span></td>
                    </tr>`;
                tbody.innerHTML += row;
            });
        }

        function updateStats(data) {
            document.getElementById('statTotal').innerText = data.length;
            
            const activeCount = data.filter(i => 
                i.op_auth_status === 'Active' || 
                (i.op_auth_status !== 'Pending' && !i.reason.includes('Initial'))
            ).length;
            document.getElementById('statActive').innerText = activeCount || data.length;
            
            const states = new Set(data.map(i => i.phy_state).filter(Boolean)).size;
            document.getElementById('statStates').innerText = states;
            
            const totalUnits = data.reduce((acc, curr) => acc + (parseInt(curr.power_units) || 0), 0);
            document.getElementById('statUnits').innerText = totalUnits;
        }

        function filterTable() {
            const query = document.getElementById('searchInput').value.toLowerCase().trim();
            const selectedState = document.getElementById('stateFilter').value;
            const selectedStatus = document.getElementById('statusFilter').value;
            
            const filtered = allData.filter(item => {
                const matchesQuery = !query || 
                    (item.legal_name && item.legal_name.toLowerCase().includes(query)) ||
                    (item.usdot_number && item.usdot_number.toLowerCase().includes(query)) ||
                    (item.phy_city && item.phy_city.toLowerCase().includes(query)) ||
                    (item.phy_state && item.phy_state.toLowerCase().includes(query)) ||
                    (item.email_address && item.email_address.toLowerCase().includes(query)) ||
                    (item.dba_name && item.dba_name.toLowerCase().includes(query));
                
                const matchesState = !selectedState || item.phy_state === selectedState;
                
                let matchesStatus = true;
                if (selectedStatus) {
                    if (selectedStatus === 'Pending') {
                        matchesStatus = item.op_auth_status === 'Pending' || item.reason.includes('Initial');
                    } else {
                        matchesStatus = item.op_auth_status === 'Active';
                    }
                }
                
                return matchesQuery && matchesState && matchesStatus;
            });
            
            renderTable(filtered);
        }

        function renderCharts() {
            if (!allData || allData.length === 0) return;

            // State chart
            const stateCounts = {};
            allData.forEach(item => {
                if (item.phy_state) {
                    stateCounts[item.phy_state] = (stateCounts[item.phy_state] || 0) + 1;
                }
            });
            const sortedStates = Object.entries(stateCounts).sort((a, b) => b[1] - a[1]).slice(0, 10);

            if (stateChartInstance) stateChartInstance.destroy();
            const ctx1 = document.getElementById('stateChart').getContext('2d');
            stateChartInstance = new Chart(ctx1, {
                type: 'bar',
                data: {
                    labels: sortedStates.map(i => i[0]),
                    datasets: [{
                        label: 'New Ventures',
                        data: sortedStates.map(i => i[1]),
                        backgroundColor: '#4f46e5',
                        borderRadius: 6
                    }]
                },
                options: { 
                    responsive: true, 
                    plugins: { legend: { display: false } },
                    scales: { y: { beginAtZero: true } }
                }
            });

            // Date chart - by add_date
            const dateCounts = {};
            allData.forEach(item => {
                if (item.add_date) {
                    dateCounts[item.add_date] = (dateCounts[item.add_date] || 0) + 1;
                }
            });
            const sortedDates = Object.entries(dateCounts).sort((a, b) => a[0].localeCompare(b[0]));

            if (dateChartInstance) dateChartInstance.destroy();
            const ctx2 = document.getElementById('dateChart').getContext('2d');
            dateChartInstance = new Chart(ctx2, {
                type: 'line',
                data: {
                    labels: sortedDates.map(i => i[0]),
                    datasets: [{
                        label: 'Registrations',
                        data: sortedDates.map(i => i[1]),
                        borderColor: '#06b6d4',
                        backgroundColor: 'rgba(6, 182, 212, 0.1)',
                        fill: true,
                        tension: 0.3,
                        pointRadius: 4
                    }]
                },
                options: { 
                    responsive: true, 
                    plugins: { legend: { display: false } },
                    scales: { y: { beginAtZero: true } }
                }
            });
        }

        function copyText(elementId) {
            const el = document.getElementById(elementId);
            el.select();
            navigator.clipboard.writeText(el.value).then(() => {
                const originalText = el.nextElementSibling ? el.nextElementSibling.innerHTML : '';
                const btn = el.parentElement.querySelector('button');
                if (btn) {
                    const orig = btn.innerHTML;
                    btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied!';
                    setTimeout(() => { btn.innerHTML = orig; }, 1800);
                }
            }).catch(() => {
                // fallback
                document.execCommand('copy');
                alert("Copied to clipboard!");
            });
        }

        // Initialize
        window.onload = () => {
            setWebhookUrls();
            loadData();
            
            // Keyboard support
            document.getElementById('daysSelect').addEventListener('change', () => {
                loadData();
            });
        };
    </script>
</body>
</html>"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/api/data", methods=["GET"])
def api_data():
    try:
        days = int(request.args.get("days", 3))
    except (ValueError, TypeError):
        days = 3
    
    data = fetch_true_new_ventures(days=days)
    
    # Always save latest for downloads
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)
    
    return jsonify({
        "success": True,
        "count": len(data),
        "days_queried": days,
        "date_window": get_date_window(days),
        "data": data
    })

@app.route("/run", methods=["GET", "POST"])
def run_pipeline():
    try:
        days = int(request.args.get("days", 3))
    except (ValueError, TypeError):
        days = 3
    
    data = fetch_true_new_ventures(days=days)
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    csv_filename = None
    
    if data:
        df = pd.DataFrame(data)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        csv_filename = f"true_new_ventures_{timestamp}.csv"
        csv_path = os.path.join(current_dir, csv_filename)
        df.to_csv(csv_path, index=False)
        
        # Update latest
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)
    
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
    if request.method == "POST":
        req_data = request.get_json(silent=True) or {}
    else:
        req_data = request.args.to_dict()
    
    try:
        days = int(req_data.get("days", 3))
    except (ValueError, TypeError):
        days = 3
    
    data = fetch_true_new_ventures(days=days)
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if data:
        df = pd.DataFrame(data)
        latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
        df.to_csv(latest_path, index=False)
    
    start_date, end_date = get_date_window(days)
    
    return jsonify({
        "success": True,
        "timestamp": datetime.now().isoformat(),
        "days": days,
        "date_window": {"start": start_date, "end": end_date},
        "total_records": len(data),
        "carriers": data
    })

@app.route("/download/csv", methods=["GET"])
def download_csv():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
    
    if os.path.exists(latest_path):
        return send_file(
            latest_path, 
            mimetype="text/csv", 
            as_attachment=True, 
            download_name="true_new_ventures_by_add_date.csv"
        )
    return jsonify({"error": "No CSV file generated yet. Please load data first."}), 404

@app.route("/download/excel", methods=["GET"])
def download_excel():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    latest_path = os.path.join(current_dir, "new_ventures_latest.csv")
    
    if os.path.exists(latest_path):
        df = pd.read_csv(latest_path)
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='New Ventures')
        output.seek(0)
        
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"true_new_ventures_{datetime.now().strftime('%Y%m%d')}.xlsx"
        )
    return jsonify({"error": "No data generated yet. Please load data first."}), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting FMCSA New Ventures app on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)

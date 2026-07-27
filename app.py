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


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>FMCSA Verified New Ventures</title>
  <style>
    body{font-family:Arial,sans-serif;margin:0;background:#f4f7fb;color:#172033}
    header{background:#111827;color:white;padding:22px 30px}
    main{padding:24px;max-width:1500px;margin:auto}
    .controls,.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:18px}
    button,select{padding:10px 14px;border-radius:8px;border:1px solid #ccd4e0}
    button{background:#4f46e5;color:white;border:0;cursor:pointer}
    .card{background:white;padding:16px;border-radius:12px;min-width:210px;box-shadow:0 3px 14px #0000000b}
    .card b{font-size:26px;display:block;margin-top:8px}
    .note{background:#fff8dc;border-left:4px solid #d99c00;padding:12px;margin-bottom:18px}
    .table-wrap{overflow:auto;background:white;border-radius:12px;box-shadow:0 3px 14px #0000000b}
    table{border-collapse:collapse;width:100%;font-size:13px}
    th,td{padding:10px;border-bottom:1px solid #e6ebf2;text-align:left;white-space:nowrap}
    th{position:sticky;top:0;background:#eef2ff}
    .ok{color:#087f5b;font-weight:bold}.loading{opacity:.55;pointer-events:none}
  </style>
</head>
<body>
<header><h2>FMCSA Verified New Ventures</h2></header>
<main id="app">
  <div class="note">“No insurance history” means no record was found in the checked FMCSA datasets at the verification time. It does not prove that no non-FMCSA or intrastate policy exists.</div>
  <div class="controls">
    <select id="days"><option value="1">Previous 1 day</option><option value="3" selected>Previous 3 days</option><option value="7">Previous 7 days</option><option value="14">Previous 14 days</option><option value="30">Previous 30 days</option></select>
    <button onclick="loadData()">Refresh official FMCSA data</button>
    <button onclick="location.href='/download/csv'">Download CSV</button>
    <button onclick="location.href='/download/excel'">Download Excel</button>
  </div>
  <div class="cards">
    <div class="card">Strict New DOT candidates<b id="initial">–</b></div>
    <div class="card">Excluded for insurance<b id="insurance">–</b></div>
    <div class="card">Excluded for New Entrant OOS<b id="oos">–</b></div>
    <div class="card">Final qualified list<b id="final">–</b></div>
  </div>
  <p id="window"></p>
  <div class="table-wrap"><table><thead><tr><th>USDOT</th><th>Company</th><th>Add date</th><th>Phone</th><th>Email</th><th>Location</th><th>Power units</th><th>Insurance</th><th>New Entrant/OOS</th></tr></thead><tbody id="rows"></tbody></table></div>
</main>
<script>
async function loadData(){
 const app=document.getElementById('app'); app.classList.add('loading');
 try{
  const d=document.getElementById('days').value;
  const r=await fetch('/api/data?days='+d); const x=await r.json();
  if(!r.ok||!x.success) throw new Error(x.error||'Request failed');
  document.getElementById('initial').textContent=x.counts.strict_new_dot_candidates;
  document.getElementById('insurance').textContent=x.counts.excluded_for_current_pending_or_previous_insurance;
  document.getElementById('oos').textContent=x.counts.excluded_for_new_entrant_oos;
  document.getElementById('final').textContent=x.counts.final_verified_candidates;
  document.getElementById('window').textContent=`Date window: ${x.date_window.start_inclusive} through the day before ${x.date_window.end_exclusive} (${x.date_window.timezone})`;
  document.getElementById('rows').innerHTML=x.carriers.map(c=>`<tr><td>${c.usdot_number}</td><td>${c.legal_name||''}</td><td>${c.add_date}</td><td>${c.phone||''}</td><td>${c.email_address||''}</td><td>${c.phy_city||''}, ${c.phy_state||''}</td><td>${c.power_units}</td><td class="ok">No FMCSA history found</td><td class="ok">Candidate; no OOS found</td></tr>`).join('');
 }catch(e){alert(e.message)}finally{app.classList.remove('loading')}
}
window.onload=loadData;
</script>
</body></html>"""


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

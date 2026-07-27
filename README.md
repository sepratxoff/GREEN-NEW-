# FMCSA Verified New Ventures Platform

This Flask app mirrors the final filtering logic used in the n8n workflow.

## Final qualification rules

A carrier is included only when it:

1. Has an FMCSA Company Census `add_date` within the previous N Eastern calendar days, excluding today.
2. Has Census `status_code = A`.
3. Has `classdef = AUTHORIZED FOR HIRE` exactly.
4. Reports `power_units > 0`.
5. Has no current or pending record in Motus Insur – All With History (`c5y8-a4uz`), regardless of form code.
6. Has no previous record in Motus InsHist – All With History (`3uet-3z4i`), regardless of form code.
7. Has no record in the New Entrant Out-of-Service Orders dataset (`p2mt-9ige`).
8. Has a unique USDOT number in the generated result.

The app performs insurance and OOS checks in batches of 75 USDOT numbers.

## Important terminology

`new_entrant_status = NEW_ENTRANT_CANDIDATE_DERIVED_FROM_RECENT_ADD_DATE` is a derived label. The public bulk data does not expose a direct current MOTUS New Entrant program-status field.

`NO_FMCSA_INSURANCE_HISTORY_FOUND` means no record was found in the two checked FMCSA insurance datasets at the verification time. It does not prove that no intrastate, non-filed, or not-yet-published policy exists.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5000`.

## Render / production

Start command:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300
```

## Required security environment variables

Set these in Render under **Environment**:

```text
PLATFORM_USERNAME=your_login_name
PLATFORM_PASSWORD=use_a_long_unique_password
FLASK_SECRET_KEY=use_a_long_random_secret
N8N_USERNAME=your_n8n_username
N8N_PASSWORD=use_a_different_long_unique_password
SESSION_COOKIE_SECURE=true
```

Browser users sign in with `PLATFORM_USERNAME` and `PLATFORM_PASSWORD`. The `/n8n/output` endpoint requires HTTP Basic Auth using `N8N_USERNAME` and `N8N_PASSWORD`.

Optional:

```text
SOCRATA_APP_TOKEN=your_data_transportation_gov_app_token
```

A Socrata app token is recommended to reduce anonymous rate-limit errors.

## Endpoints

- `GET /api/data?days=3` — complete platform response with audit summaries
- `GET or POST /n8n/output` with `{ "days": 3 }` — compact n8n response with a `carriers` array
- `GET or POST /webhook` with `{ "days": 3 }`
- `GET or POST /run`
- `GET /download/csv`
- `GET /download/excel`
- `GET /health`

In n8n, call `/n8n/output`, then use **Split Out** on the `carriers` property.

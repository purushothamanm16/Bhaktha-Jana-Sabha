# Manikandan Bhaktha Jana Sabha

A small FastAPI website for adding and editing **Credit** and **Debit** entries in your existing Google Spreadsheet, without opening Google Sheets.

* Backend: Python, FastAPI, Uvicorn · Frontend: HTML, CSS, vanilla JS
* Sheets: Google Sheets API with a Service Account · Hosting: Render

## How it treats your spreadsheet

* Row 1 (title/totals) and row 2 (headers) are never touched. Transactions live in columns **A–D from row 3**.
* Only cell **values** are written (never formatting). Text is stored literally, so it can never turn into a formula. Existing formulas in column A are left alone.
* The next row and the serial number (No.) are worked out from the sheet each time (row 3 = No. 1, row 4 = No. 2 …). Credit and Debit are numbered independently.
* Each transaction written by the website gets an invisible **developer-metadata** tag (unique id + submission time) on its row. Nothing visible is added. Google moves the tag with the row, even if you sort or insert rows by hand.
* **Credit ⇄ Debit moves** are a single Google `batchUpdate` (write into the other tab + remove from this tab + renumber), which Google applies all-or-nothing. A failure leaves the spreadsheet exactly as it was: no lost row, no duplicate.
* The removed row is deleted (so later rows shift up) and the numbers after it are recalculated.
* Double-click/retry protection: each form submit carries a unique id; a repeat is rejected as "already recorded". A server-side lock serialises all sheet operations.

**Things to know**

* "Recent Transactions" lists only entries made through this website (they carry the tag). Rows you typed by hand are kept and numbered, but are not listed or editable here.
* Only the 10 most recent entries can be edited; the server enforces this.
* Make your total formulas open-ended, e.g. `=SUM(C3:C)`, so new rows are always included.
* The file must be a real **Google Sheet** (if it is an uploaded .xlsx, use *File → Save as Google Sheets*). The tabs must be named exactly `Credit` and `Debit`.
* Run a **single** worker/instance (the default on Render). Login-attempt limits are kept in memory.

## 1. Google Cloud setup

1. Go to <https://console.cloud.google.com/> and create a project (e.g. "sabha-sheets").
2. **APIs & Services → Library →** search **Google Sheets API → Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account.** Give it a name, finish.
4. Open the service account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads. Keep it private.
5. Copy the service account's email (looks like `name@project.iam.gserviceaccount.com`).
6. Open your Google Spreadsheet → **Share** → paste that email → role **Editor** → uncheck "Notify" → Share.
7. Copy the spreadsheet ID from its URL: `https://docs.google.com/spreadsheets/d/<THIS_PART>/edit`.

## 2. Run locally

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then edit .env
python hash_password.py                                # paste the output into ADMIN_PASSWORD_HASH
python -c "import secrets; print(secrets.token_urlsafe(48))"   # paste into SECRET_KEY
```

In `.env` set `SPREADSHEET_ID`, put the downloaded key next to `main.py` as `service-account.json` (or change `GOOGLE_APPLICATION_CREDENTIALS`), keep `COOKIE_SECURE=false` for local http.

Check the Google connection first:

```bash
python sheets.py        # should print: Connected ... Tab "Credit": found ... Tab "Debit": found
```

Start the site:

```bash
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000> and sign in.

## 3. Deploy on Render

1. Push this folder to GitHub (`.gitignore` already excludes `.env` and key files; never commit the key).
2. Render → **New → Web Service** → pick the repo.
3. Settings:
   * **Build command:** `pip install -r requirements.txt`
   * **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips="*"`
   * **Environment variable** `PYTHON_VERSION` = `3.12.3` (or newer)
4. **Environment variables:**

   | Name | Value |
   |---|---|
   | `ADMIN_USERNAME` | your admin username |
   | `ADMIN_PASSWORD_HASH` | output of `python hash_password.py` |
   | `SECRET_KEY` | long random string (keep it stable) |
   | `COOKIE_SECURE` | `true` |
   | `SPREADSHEET_ID` | your spreadsheet ID |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the whole key JSON pasted as one value |

   Alternative to the last row: add a Render **Secret File** named `service-account.json` and set `GOOGLE_APPLICATION_CREDENTIALS=/etc/secrets/service-account.json`.
5. Deploy. Health check path: `/healthz`.

## Environment variables

See `.env.example`. Required: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`, `SECRET_KEY`, `SPREADSHEET_ID`, and one of `GOOGLE_APPLICATION_CREDENTIALS` / `GOOGLE_SERVICE_ACCOUNT_JSON`. Optional: `COOKIE_SECURE` (default `true`), `SESSION_HOURS` (default 8).

## Security summary

bcrypt password hash in environment variables · signed HTTP-only, SameSite cookie sessions (Secure on HTTPS) · all pages and `/api` routes require login (401 → redirect to login) · 5 failed logins per IP per 15 minutes locks that IP out · all input re-validated on the server · internal errors and credentials are never shown to the browser.

## Files

`main.py` routes, login, validation · `sheets.py` all Google Sheets logic · `hash_password.py` password-hash helper · `templates/` pages · `static/` CSS, JS, SVG ornaments.

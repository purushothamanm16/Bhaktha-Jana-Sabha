"""Manikandan Bhaktha Jana Sabha - FastAPI app: pages, admin login, transaction API."""

import hmac
import logging
import math
import os
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path

import bcrypt
from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from datetime import datetime
import sheets

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sabha")

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------- configuration
def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name} (see .env.example)")
    return value


ADMIN_USERNAME = _required("ADMIN_USERNAME")
ADMIN_PASSWORD_HASH = _required("ADMIN_PASSWORD_HASH").encode()
SECRET_KEY = _required("SECRET_KEY")
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").strip().lower() != "false"
SESSION_SECONDS = int(os.environ.get("SESSION_HOURS", "8")) * 3600

if not ADMIN_PASSWORD_HASH.startswith(b"$2"):
    raise RuntimeError("ADMIN_PASSWORD_HASH must be a bcrypt hash. Create one with: python hash_password.py")

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="sabha_session",
                   max_age=SESSION_SECONDS, same_site="lax", https_only=COOKIE_SECURE)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
try:
    templates.env.globals["sheet_url"] = (
        f"https://docs.google.com/spreadsheets/d/{sheets._spreadsheet_id()}/edit")
except sheets.SheetsError:
    templates.env.globals["sheet_url"] = ""


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if not request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------- error handlers
@app.exception_handler(sheets.SheetsError)
async def sheets_error_handler(request: Request, exc: sheets.SheetsError):
    return JSONResponse({"error": exc.message, "duplicate": exc.duplicate}, status_code=exc.status)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "The request was not understood."}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    log.exception("Unhandled error")
    return JSONResponse({"error": "Something went wrong on the server. Please try again."}, status_code=500)


# ---------------------------------------------------------------- authentication
MAX_FAILS = 5
FAIL_WINDOW = 15 * 60
_failed_logins = {}          # ip -> [timestamps of recent failures]


def _client_ip(request: Request):
    return request.client.host if request.client else "unknown"


def _lockout_seconds(ip):
    now = time.time()
    recent = [t for t in _failed_logins.get(ip, []) if now - t < FAIL_WINDOW]
    if recent:
        _failed_logins[ip] = recent
    else:
        _failed_logins.pop(ip, None)
    if len(recent) >= MAX_FAILS:
        return int(FAIL_WINDOW - (now - recent[0]))
    return 0


def _authed(request: Request):
    s = request.session
    return s.get("user") == ADMIN_USERNAME and time.time() - s.get("t", 0) < SESSION_SECONDS


def require_api(request: Request):
    if not _authed(request):
        raise HTTPException(401, "Your session has expired. Please sign in again.")
    if request.method != "GET" and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "Request rejected.")


def _login_page(request, error=None, status=200):
    return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=status)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _authed(request):
        return RedirectResponse("/", 303)
    error = "Your session has expired. Please sign in again." if request.query_params.get("expired") else None
    return _login_page(request, error)


@app.post("/login", response_class=HTMLResponse)
def login(request: Request, username: str = Form(""), password: str = Form("")):
    ip = _client_ip(request)
    wait = _lockout_seconds(ip)
    if wait:
        minutes = max(1, math.ceil(wait / 60))
        return _login_page(request, f"Too many failed attempts. Please try again in {minutes} minute(s).", 429)

    user_ok = hmac.compare_digest(username.strip().encode(), ADMIN_USERNAME.encode())
    try:   # always check the password so timing does not reveal whether the username was right
        pass_ok = bcrypt.checkpw(password.encode()[:72], ADMIN_PASSWORD_HASH)
    except ValueError:
        log.error("ADMIN_PASSWORD_HASH is not a valid bcrypt hash")
        pass_ok = False

    if user_ok and pass_ok:
        _failed_logins.pop(ip, None)
        request.session.clear()
        request.session.update(user=ADMIN_USERNAME, t=int(time.time()))
        return RedirectResponse("/", 303)

    _failed_logins.setdefault(ip, []).append(time.time())
    return _login_page(request, "Incorrect username or password.", 401)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)


# ---------------------------------------------------------------- pages
@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "dashboard.html", {"page": "dashboard"})


@app.get("/transactions", response_class=HTMLResponse)
def transactions_page(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", 303)
    return templates.TemplateResponse(request, "transactions.html", {"page": "transactions"})


# ---------------------------------------------------------------- transaction API
AMOUNT_RE = re.compile(r"^(\d+(\.\d*)?|\.\d+)$")


def _invalid(message):
    raise HTTPException(422, message)


def validate(payload):
    """Validate and clean a submitted transaction. Raises HTTPException(422) with a clear message."""
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    if not name:
        _invalid("Name is required.")
    if len(name) > 100:
        _invalid("Name must be 100 characters or fewer.")

    raw = payload.get("amount")
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        _invalid("Amount is required.")
    text = str(raw).strip().replace(",", "")
    if text.startswith("-"):
        _invalid("Amount must be greater than zero.")
    if not AMOUNT_RE.match(text):
        _invalid("Enter a valid amount, for example 1500 or 250.50.")
    amount = Decimal(text)
    if amount <= 0:
        _invalid("Amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        _invalid("Amount can have at most 2 decimal places.")
    if amount >= Decimal("1000000000000"):
        _invalid("Amount is too large.")
    amount_value = int(amount) if amount == amount.to_integral_value() else float(amount)

    tx_type = str(payload.get("type") or "").strip().lower()
    if not tx_type:
        _invalid("Select Credit or Debit.")
    if tx_type not in sheets.TABS:
        _invalid("Invalid transaction type.")

    raw_stamp = str(payload.get("timestamp") or "").strip().replace("T", " ")
    if not raw_stamp:
        _invalid("Date and time is required.")
    stamp_dt = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            stamp_dt = datetime.strptime(raw_stamp, fmt)
            break
        except ValueError:
            pass
    if stamp_dt is None or not (2000 <= stamp_dt.year <= 2100):
        _invalid("Enter a valid date and time.")
    stamp = stamp_dt.strftime("%d-%m-%Y %H:%M")        # stored exactly as typed (IST)

    description = re.sub(r"[^\S\n]+", " ", str(payload.get("description") or ""))
    description = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", description).strip()
    if len(description) > 500:
        _invalid("Description must be 500 characters or fewer.")

    return {"name": name, "amount": amount_value, "type": tx_type,
            "description": description, "stamp": stamp}
    """Validate and clean a submitted transaction. Raises HTTPException(422) with a clear message."""
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    if not name:
        _invalid("Name is required.")
    if len(name) > 100:
        _invalid("Name must be 100 characters or fewer.")

    raw = payload.get("amount")
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        _invalid("Amount is required.")
    text = str(raw).strip().replace(",", "")
    if text.startswith("-"):
        _invalid("Amount must be greater than zero.")
    if not AMOUNT_RE.match(text):
        _invalid("Enter a valid amount, for example 1500 or 250.50.")
    amount = Decimal(text)
    if amount <= 0:
        _invalid("Amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        _invalid("Amount can have at most 2 decimal places.")
    if amount >= Decimal("1000000000000"):
        _invalid("Amount is too large.")
    amount_value = int(amount) if amount == amount.to_integral_value() else float(amount)

    tx_type = str(payload.get("type") or "").strip().lower()
    if not tx_type:
        _invalid("Select Credit or Debit.")
    if tx_type not in sheets.TABS:
        _invalid("Invalid transaction type.")

    raw_stamp = str(payload.get("timestamp") or "").strip().replace("T", " ")
    if not raw_stamp:
        _invalid("Date and time is required.")
    stamp_dt = None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            stamp_dt = datetime.strptime(raw_stamp, fmt)
            break
        except ValueError:
            pass
    if stamp_dt is None or not (2000 <= stamp_dt.year <= 2100):
        _invalid("Enter a valid date and time.")
    stamp = stamp_dt.strftime("%d-%m-%Y %H:%M")        # stored exactly as typed (IST)

    description = re.sub(r"[^\S\n]+", " ", str(payload.get("description") or ""))
    description = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", description).strip()
    if len(description) > 500:
        _invalid("Description must be 500 characters or fewer.")

    return {"name": name, "amount": amount_value, "type": tx_type,
            "description": description, "stamp": stamp}
    """Validate and clean a submitted transaction. Raises HTTPException(422) with a clear message."""
    name = re.sub(r"\s+", " ", str(payload.get("name") or "")).strip()
    if not name:
        _invalid("Name is required.")
    if len(name) > 100:
        _invalid("Name must be 100 characters or fewer.")

    raw = payload.get("amount")
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        _invalid("Amount is required.")
    text = str(raw).strip().replace(",", "")
    if text.startswith("-"):
        _invalid("Amount must be greater than zero.")
    if not AMOUNT_RE.match(text):
        _invalid("Enter a valid amount, for example 1500 or 250.50.")
    amount = Decimal(text)
    if amount <= 0:
        _invalid("Amount must be greater than zero.")
    if amount.as_tuple().exponent < -2:
        _invalid("Amount can have at most 2 decimal places.")
    if amount >= Decimal("1000000000000"):
        _invalid("Amount is too large.")
    amount_value = int(amount) if amount == amount.to_integral_value() else float(amount)

    tx_type = str(payload.get("type") or "").strip().lower()
    if not tx_type:
        _invalid("Select Credit or Debit.")
    if tx_type not in sheets.TABS:
        _invalid("Invalid transaction type.")

    description = re.sub(r"[^\S\n]+", " ", str(payload.get("description") or ""))
    description = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", description).strip()
    if len(description) > 500:
        _invalid("Description must be 500 characters or fewer.")

    return {"name": name, "amount": amount_value, "type": tx_type, "description": description}


@app.get("/api/transactions")
def api_list(_: None = Depends(require_api)):
    return {"transactions": sheets.list_recent()}


@app.post("/api/transactions")
def api_create(payload: dict = Body(...), _: None = Depends(require_api)):
    tx = validate(payload)
    try:
        client_id = str(uuid.UUID(str(payload.get("client_id"))))
    except ValueError:
        client_id = str(uuid.uuid4())
    result = sheets.add_transaction(tx["type"], tx["name"], tx["amount"], tx["description"],
                                    client_id, tx["stamp"])
    return {"ok": True, "transaction": result}


@app.put("/api/transactions/{tx_id}")
def api_update(tx_id: str, payload: dict = Body(...), _: None = Depends(require_api)):
    try:
        tx_id = str(uuid.UUID(tx_id))
    except ValueError:
        raise HTTPException(404, "That transaction could not be found.")
    tx = validate(payload)
    result = sheets.update_transaction(tx_id, tx["type"], tx["name"], tx["amount"],
                                       tx["description"], tx["stamp"])
    return {"ok": True, "transaction": result}

@app.delete("/api/transactions/{tx_id}")
def api_delete(tx_id: str, _: None = Depends(require_api)):
    try:
        tx_id = str(uuid.UUID(tx_id))
    except ValueError:
        raise HTTPException(404, "That transaction could not be found.")
    sheets.delete_transaction(tx_id)
    return {"ok": True}

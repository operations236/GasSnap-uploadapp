#!/usr/bin/env python3
"""
GasSnap Invoice Upload Backend
Production-ready FastAPI app for secure invoice photo + metadata uploads.
"""

import logging
import os
import json
import time
import uuid
import datetime
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles

import sheets as sheets_integration
import ocr as ocr_integration

load_dotenv(Path(__file__).resolve().parent / ".env")

# Configuration
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
LOGS_DIR = BASE_DIR / "logs"
PINS_FILE = BASE_DIR / "pins.json"
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_CONTENT_TYPES = [
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "application/pdf",
]
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".pdf"}


def load_pin_map(path: Path = PINS_FILE) -> Dict[str, str]:
    """Load PIN→store map from pins.json (same stores/PINs as CigAudit).

    Source of truth for PINs lives outside git (pins.json is gitignored).
    Copied from live CigAudit STORE_PINS in /var/www/gassnap.io/audit/index.html.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"PIN map missing: {path}. Create pins.json with "
            '{"0205": "Killbuck", ...} matching CigAudit STORE_PINS.'
        )
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"PIN map empty or invalid JSON object: {path}")
    # normalize keys to strings (JSON may already be)
    return {str(k).strip(): str(v).strip() for k, v in data.items() if str(k).strip()}


# PIN to Store mapping — never expose to client
PIN_TO_STORE: Dict[str, str] = load_pin_map()

# Ensure directories exist
UPLOAD_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# Logging setup (file + console)
log_file = LOGS_DIR / "upload.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI(
    title="InvUpload",
    description="Secure backend for uploading invoice photos with metadata",
    version="1.0.0",
)

# CORS (restrict in production if needed; currently open for internal use)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SESSION_SECRET = os.getenv("SESSION_SECRET")
if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET not set (check .env in this app's own directory)")

SESSION_MAX_AGE = 24 * 60 * 60  # 24h — covers a full shift with buffer, re-prompts daily

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="invupload_session",
    max_age=SESSION_MAX_AGE,
    same_site="lax",
    https_only=True,
)

# Rate limiting on PIN attempts — in-memory, single-worker process, resets on restart.
# A shared 4-digit PIN with no per-user identity is far more brute-forceable than a
# real password, so this endpoint specifically needs a lockout.
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 300  # 5 min cooldown after 5 failed attempts from one IP
_login_attempts: Dict[str, list] = {}


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    attempts = [t for t in _login_attempts.get(client_ip, []) if now - t < LOGIN_LOCKOUT_SECONDS]
    _login_attempts[client_ip] = attempts
    if len(attempts) >= LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=429,
            detail=f"Too many incorrect PIN attempts. Try again in a few minutes.",
        )


def _record_failed_attempt(client_ip: str) -> None:
    _login_attempts.setdefault(client_ip, []).append(time.time())


def require_session(request: Request) -> str:
    """Dependency: returns the session's store name, or 401s if no valid session."""
    store = request.session.get("store")
    if not store:
        raise HTTPException(status_code=401, detail="Not logged in. Please enter your store PIN.")
    return store


@app.post("/login")
async def login(request: Request, pin: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    pin = (pin or "").strip()
    if pin not in PIN_TO_STORE:
        _record_failed_attempt(client_ip)
        logger.warning(f"Invalid PIN login attempt from {client_ip}")
        raise HTTPException(status_code=401, detail="Incorrect PIN.")

    store_name = PIN_TO_STORE[pin]
    _login_attempts.pop(client_ip, None)  # reset on success
    request.session["store"] = store_name
    logger.info(f"Login successful for store '{store_name}' from {client_ip}")
    return {"success": True, "store": store_name}


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"success": True}


@app.get("/whoami")
async def whoami(request: Request):
    store = request.session.get("store")
    if not store:
        raise HTTPException(status_code=401, detail="Not logged in.")
    return {"store": store}

# Serve the frontend (index.html) at root
@app.get("/")
async def serve_frontend():
    index_path = BASE_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    # Bust mobile/browser cache so picker HTML/JS updates show up immediately
    return FileResponse(
        index_path,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )

# Health check
@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "invupload",
        "stores": len(PIN_TO_STORE),
        "sheets": sheets_integration.sheets_status(),
        "ocr": ocr_integration.ocr_status(),
        "timestamp": datetime.datetime.now().isoformat(),
    }

# Main upload endpoint
@app.post("/upload")
async def upload_invoice(
    background_tasks: BackgroundTasks,
    store_name: str = Depends(require_session),
    invoice_date: str = Form(..., description="Invoice date (YYYY-MM-DD)"),
    invoice_number: str = Form(..., description="Invoice number or reference"),
    photo: UploadFile = File(..., description="Photo or PDF of the invoice"),
):
    """
    Accepts invoice photo/PDF + metadata. Store identity comes from the session
    (set at /login), not a per-request PIN.
    Saves file + creates JSON metadata file.
    Queues Gemini OCR → per-line Google Sheets rows in the background.
    Returns immediately (does not wait on OCR).
    """
    # 1. Basic validations
    if not invoice_date or not invoice_number:
        raise HTTPException(status_code=400, detail="Invoice date and number are required.")

    if not photo or not photo.filename:
        raise HTTPException(status_code=400, detail="Invoice photo or PDF is required.")

    # 3. Read file content (for size + save)
    try:
        content = await photo.read()
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        raise HTTPException(status_code=400, detail="Failed to read uploaded file.")

    file_size = len(content)
    if file_size == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size is {MAX_FILE_SIZE // (1024*1024)} MB.",
        )

    # 4. Content type / extension (best effort — mobile often omits type)
    content_type = (photo.content_type or "").strip().lower()
    file_ext = Path(photo.filename).suffix.lower()
    looks_pdf = content.startswith(b"%PDF") or file_ext == ".pdf" or "pdf" in content_type
    if looks_pdf:
        file_ext = ".pdf"
        if not content_type:
            content_type = "application/pdf"
    elif not file_ext or file_ext not in ALLOWED_EXTENSIONS:
        # default images when extension missing/unknown
        file_ext = ".jpg"
    if content_type and not any(ct in content_type for ct in ALLOWED_CONTENT_TYPES):
        # Allow empty/odd mobile types; hard-reject only clear non-media
        if content_type not in ("application/octet-stream", "binary/octet-stream", ""):
            logger.warning(f"Unusual content type for upload: {content_type}")

    # 5. Generate safe unique filename (preserve real .pdf — never force .jpg)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if file_ext not in ALLOWED_EXTENSIONS:
        file_ext = ".pdf" if looks_pdf else ".jpg"

    unique_id = f"{timestamp}_{uuid.uuid4().hex[:10]}"
    safe_photo_filename = f"{unique_id}{file_ext}"
    photo_path = UPLOAD_DIR / safe_photo_filename

    # 6. Save the file
    try:
        with open(photo_path, "wb") as buffer:
            buffer.write(content)
        logger.info(f"File saved: {safe_photo_filename} ({file_size} bytes) type={content_type or 'unknown'}")
    except Exception as e:
        logger.error(f"Failed to save file: {e}")
        raise HTTPException(status_code=500, detail="Failed to save file. Please try again.")

    # 7. Create and save metadata JSON
    uploaded_at = datetime.datetime.now().isoformat()
    metadata = {
        "id": unique_id,
        "store": store_name,
        "invoice_date": invoice_date,
        "invoice_number": invoice_number.strip(),
        "photo_filename": safe_photo_filename,
        "original_filename": photo.filename,
        "uploaded_at": uploaded_at,
        "file_size_bytes": file_size,
        "content_type": content_type,
        "ocr": {"status": "queued"},
    }

    json_filename = f"{unique_id}.json"
    json_path = UPLOAD_DIR / json_filename

    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2, ensure_ascii=False)
        logger.info(f"Metadata saved: {json_filename}")
    except Exception as e:
        logger.error(f"Failed to save metadata JSON: {e}")

    # 8. Queue Gemini OCR → Sheets (async — do not block response)
    #    Sheet rows are written by OCR (one per line item). No blank placeholder row.
    background_tasks.add_task(
        ocr_integration.process_upload_ocr,
        photo_path=str(photo_path),
        meta_path=str(json_path),
        store=store_name,
        invoice_number=invoice_number.strip(),
        invoice_date=invoice_date,
        timestamp=uploaded_at,
        known_stores=list(dict.fromkeys(PIN_TO_STORE.values())),
    )
    logger.info("OCR queued for id=%s store=%s", unique_id, store_name)

    # 9. Success response (clear and actionable)
    logger.info(f"Upload completed successfully for store '{store_name}' - ID: {unique_id}")
    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": "Invoice uploaded successfully! OCR is processing in the background.",
            "id": unique_id,
            "store": store_name,
            "invoice_date": invoice_date,
            "invoice_number": invoice_number,
            "photo_filename": safe_photo_filename,
            "uploaded_at": uploaded_at,
            "ocr": {"status": "queued"},
        }
    )

# Optional: simple listing endpoint for admin/debug (can be removed or protected later)
@app.get("/uploads")
async def list_uploads(store_name: str = Depends(require_session)):
    """List recent uploads (metadata only). Requires a valid session."""
    json_files = sorted(UPLOAD_DIR.glob("*.json"), reverse=True)[:20]
    uploads = []
    for jf in json_files:
        try:
            with open(jf, "r", encoding="utf-8") as f:
                data = json.load(f)
                uploads.append({
                    "id": data.get("id"),
                    "store": data.get("store"),
                    "invoice_date": data.get("invoice_date"),
                    "invoice_number": data.get("invoice_number"),
                    "photo_filename": data.get("photo_filename"),
                    "uploaded_at": data.get("uploaded_at"),
                })
        except Exception:
            continue
    return {"count": len(uploads), "uploads": uploads}

# Mount static if you add assets later (currently not needed)
# app.mount("/static", StaticFiles(directory="."), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=True)

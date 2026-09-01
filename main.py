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
from typing import Dict, List, Optional, Tuple
import io

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
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_FILES = 12
MAX_TOTAL_SIZE = 36 * 1024 * 1024  # 36 MB combined packet (nginx should be ≥40m)
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

def _is_pdf_bytes(content: bytes, filename: str, content_type: str) -> bool:
    ext = Path(filename or "").suffix.lower()
    ct = (content_type or "").lower()
    return content.startswith(b"%PDF") or ext == ".pdf" or "pdf" in ct


def _guess_ext(filename: str, content: bytes, content_type: str) -> str:
    if _is_pdf_bytes(content, filename, content_type):
        return ".pdf"
    ext = Path(filename or "").suffix.lower()
    if ext in ALLOWED_EXTENSIONS:
        return ext
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    if "heic" in ct or "heif" in ct:
        return ".heic"
    return ".jpg"


def _images_to_multipage_pdf(image_blobs: List[bytes]) -> bytes:
    """Merge one or more image bytes into a multipage PDF for Gemini OCR."""
    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError("Pillow required for multi-photo merge") from e

    imgs: List = []
    try:
        for blob in image_blobs:
            im = Image.open(io.BytesIO(blob))
            im.load()
            if im.mode in ("RGBA", "P", "LA"):
                im = im.convert("RGB")
            elif im.mode != "RGB":
                im = im.convert("RGB")
            imgs.append(im)
        if not imgs:
            raise ValueError("no images to merge")
        out = io.BytesIO()
        first, rest = imgs[0], imgs[1:]
        first.save(out, format="PDF", save_all=bool(rest), append_images=rest)
        return out.getvalue()
    finally:
        for im in imgs:
            try:
                im.close()
            except Exception:
                pass


# Main upload endpoint
@app.post("/upload")
async def upload_invoice(
    background_tasks: BackgroundTasks,
    store_name: str = Depends(require_session),
    invoice_date: str = Form(..., description="Invoice date (YYYY-MM-DD)"),
    invoice_number: str = Form(..., description="Invoice number or reference"),
    photos: Optional[List[UploadFile]] = File(None),
    photo: Optional[UploadFile] = File(None),
):
    """
    Accepts one or more invoice photos/PDFs + metadata (session store).

    Multi-page: several images are merged into one multipage PDF for OCR.
    A single PDF is kept as-is. Mixing PDF + images in one packet is rejected.
    """
    if not invoice_date or not invoice_number:
        raise HTTPException(status_code=400, detail="Invoice date and number are required.")

    # Collect files: multi `photos` and legacy single `photo`
    uploads: List[UploadFile] = []
    if photos:
        if isinstance(photos, list):
            uploads.extend([p for p in photos if p is not None])
        else:
            uploads.append(photos)
    if photo is not None and (photo.filename or "").strip():
        uploads.append(photo)
    # Drop empties
    uploads = [u for u in uploads if u is not None and (u.filename or "").strip()]
    if not uploads:
        raise HTTPException(status_code=400, detail="At least one invoice photo or PDF is required.")
    if len(uploads) > MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files. Maximum is {MAX_FILES} photos/PDFs per upload.",
        )

    # Read all payloads
    parts: List[Tuple[str, bytes, str]] = []  # orig_name, content, content_type
    total_size = 0
    try:
        for uf in uploads:
            content = await uf.read()
            size = len(content)
            if size == 0:
                raise HTTPException(status_code=400, detail=f"Empty file: {uf.filename}")
            if size > MAX_FILE_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"File too large ({uf.filename}). Max {MAX_FILE_SIZE // (1024*1024)} MB each.",
                )
            total_size += size
            if total_size > MAX_TOTAL_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail=f"Combined upload too large. Max {MAX_TOTAL_SIZE // (1024*1024)} MB total.",
                )
            parts.append(
                (
                    uf.filename or "upload.bin",
                    content,
                    (uf.content_type or "").strip().lower(),
                )
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to read uploaded file(s): {e}")
        raise HTTPException(status_code=400, detail="Failed to read uploaded file(s).")

    pdf_flags = [_is_pdf_bytes(c, n, t) for n, c, t in parts]
    n_pdf = sum(1 for f in pdf_flags if f)
    n_img = len(parts) - n_pdf

    if n_pdf and n_img:
        raise HTTPException(
            status_code=400,
            detail="Upload either photos or one PDF — not both in the same packet.",
        )
    if n_pdf > 1:
        raise HTTPException(
            status_code=400,
            detail="Upload one PDF at a time (or use multiple photos of the pages instead).",
        )

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = f"{timestamp}_{uuid.uuid4().hex[:10]}"
    page_filenames: List[str] = []
    page_meta: List[dict] = []

    # Save individual pages for audit
    try:
        for i, (orig_name, content, content_type) in enumerate(parts, start=1):
            ext = _guess_ext(orig_name, content, content_type)
            if ext not in ALLOWED_EXTENSIONS:
                ext = ".pdf" if _is_pdf_bytes(content, orig_name, content_type) else ".jpg"
            page_name = f"{unique_id}_p{i:02d}{ext}" if len(parts) > 1 else f"{unique_id}{ext}"
            page_path = UPLOAD_DIR / page_name
            with open(page_path, "wb") as buffer:
                buffer.write(content)
            page_filenames.append(page_name)
            page_meta.append(
                {
                    "filename": page_name,
                    "original_filename": orig_name,
                    "file_size_bytes": len(content),
                    "content_type": content_type or None,
                }
            )
            logger.info(
                "File saved: %s (%s bytes) type=%s page=%s/%s",
                page_name,
                len(content),
                content_type or "unknown",
                i,
                len(parts),
            )
    except Exception as e:
        logger.error(f"Failed to save file(s): {e}")
        raise HTTPException(status_code=500, detail="Failed to save file. Please try again.")

    # OCR media path: single file as-is; multi-image → multipage PDF
    ocr_filename: str
    ocr_path: Path
    content_type_primary: str
    file_size_primary: int

    if n_pdf == 1:
        ocr_filename = page_filenames[0]
        ocr_path = UPLOAD_DIR / ocr_filename
        content_type_primary = "application/pdf"
        file_size_primary = parts[0][1].__len__()
    elif len(parts) == 1:
        ocr_filename = page_filenames[0]
        ocr_path = UPLOAD_DIR / ocr_filename
        content_type_primary = parts[0][2] or "image/jpeg"
        file_size_primary = len(parts[0][1])
    else:
        # Multi photo → one multipage PDF for Gemini
        try:
            pdf_bytes = _images_to_multipage_pdf([c for _, c, _ in parts])
        except Exception as e:
            logger.error(f"Multi-photo PDF merge failed: {e}")
            raise HTTPException(
                status_code=400,
                detail="Could not merge photos into multipage PDF. Try JPG/PNG, or upload a single PDF.",
            )
        ocr_filename = f"{unique_id}_packet.pdf"
        ocr_path = UPLOAD_DIR / ocr_filename
        try:
            with open(ocr_path, "wb") as f:
                f.write(pdf_bytes)
        except Exception as e:
            logger.error(f"Failed to save packet PDF: {e}")
            raise HTTPException(status_code=500, detail="Failed to save merged PDF.")
        content_type_primary = "application/pdf"
        file_size_primary = len(pdf_bytes)
        logger.info(
            "Merged multipage PDF: %s (%s bytes) from %s photos",
            ocr_filename,
            file_size_primary,
            len(parts),
        )

    uploaded_at = datetime.datetime.now().isoformat()
    metadata = {
        "id": unique_id,
        "store": store_name,
        "invoice_date": invoice_date,
        "invoice_number": invoice_number.strip(),
        "photo_filename": ocr_filename,
        "page_count": len(parts),
        "pages": page_meta,
        "original_filename": parts[0][0] if len(parts) == 1 else f"{len(parts)}-page packet",
        "uploaded_at": uploaded_at,
        "file_size_bytes": file_size_primary,
        "content_type": content_type_primary,
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

    background_tasks.add_task(
        ocr_integration.process_upload_ocr,
        photo_path=str(ocr_path),
        meta_path=str(json_path),
        store=store_name,
        invoice_number=invoice_number.strip(),
        invoice_date=invoice_date,
        timestamp=uploaded_at,
        known_stores=list(dict.fromkeys(PIN_TO_STORE.values())),
    )
    logger.info(
        "OCR queued for id=%s store=%s pages=%s media=%s",
        unique_id,
        store_name,
        len(parts),
        ocr_filename,
    )

    logger.info(f"Upload completed successfully for store '{store_name}' - ID: {unique_id}")
    return JSONResponse(
        status_code=200,
        content={
            "success": True,
            "message": (
                "Invoice received. Line items are extracting and will appear on your Inv sheet shortly."
                if len(parts) == 1
                else (
                    f"Invoice packet ({len(parts)} pages) received. "
                    "Line items are extracting and will appear on your Inv sheet shortly."
                )
            ),
            "confirmed": True,
            "id": unique_id,
            "store": store_name,
            "invoice_date": invoice_date,
            "invoice_number": invoice_number.strip(),
            "photo_filename": ocr_filename,
            "page_count": len(parts),
            "uploaded_at": uploaded_at,
            "ocr": {"status": "queued"},
        },
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

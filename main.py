"""
FastAPI uygulaması: web arayüzü + REST web servisi.
FastAPI application: web UI + REST web service.
"""
from __future__ import annotations

import os
import json

from fastapi import FastAPI, UploadFile, File, Request, Header, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from analyze import analyze_document, ENGINE_VERSION
from i18n import t as i18n_t

BASE = os.path.dirname(os.path.abspath(__file__))
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))  # 25 MB
API_KEYS = {k.strip() for k in os.environ.get("DEKONT_API_KEYS", "").split(",") if k.strip()}

app = FastAPI(
    title="Dekont Doğrulama & Adli Analiz API",
    description="Banka dekontlarında tahrifat, oynama ve AI-izi tespiti / "
                "Tamper, manipulation and AI-trace detection for bank receipts.",
    version=ENGINE_VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _lang(request: Request, lang: str | None) -> str:
    if lang in ("tr", "en"):
        return lang
    al = request.headers.get("accept-language", "").lower()
    return "en" if al.startswith("en") else "tr"


# ----------------------------- WEB ARAYÜZÜ -----------------------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, lang: str | None = None):
    L = _lang(request, lang)
    return templates.TemplateResponse("index.html", {
        "request": request, "L": L, "T": i18n_t(L), "version": ENGINE_VERSION,
    })


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_web(request: Request, file: UploadFile = File(...), lang: str = Form("tr")):
    L = _lang(request, lang)
    T = i18n_t(L)
    data = await file.read()
    if not data:
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION, "error": T["err_no_file"]},
            status_code=400)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large")
    if not (data[:5] == b"%PDF-" or (file.filename or "").lower().endswith(".pdf")):
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION, "error": T["err_not_pdf"]},
            status_code=400)
    try:
        report = analyze_document(data, file.filename or "document.pdf")
    except Exception as e:
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION,
             "error": f"{T['err_failed']} ({e})"}, status_code=500)
    return templates.TemplateResponse("report.html", {
        "request": request, "L": L, "T": T, "version": ENGINE_VERSION,
        "r": report, "report_json": json.dumps(report, ensure_ascii=False, indent=2),
    })


# ----------------------------- REST WEB SERVİSİ -----------------------------
def _check_api_key(x_api_key: str | None):
    if API_KEYS and x_api_key not in API_KEYS:
        raise HTTPException(status_code=401, detail="Invalid or missing API key (X-API-Key).")


@app.get("/api/v1/health")
async def health():
    import ocr
    return {"status": "ok", "engine_version": ENGINE_VERSION,
            "ocr_available": ocr.ocr_available(), "ocr_lang": ocr.best_lang(),
            "api_key_required": bool(API_KEYS)}


@app.post("/api/v1/analyze")
async def analyze_api(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    """
    Bir PDF dekontu analiz eder ve yapılandırılmış JSON rapor döndürür.
    Analyze a PDF receipt and return a structured JSON report.

    - **file**: multipart/form-data PDF dosyası
    - **X-API-Key**: (opsiyonel) sunucu yapılandırılmışsa gereklidir
    """
    _check_api_key(x_api_key)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large.")
    if not (data[:5] == b"%PDF-" or (file.filename or "").lower().endswith(".pdf")):
        raise HTTPException(415, "Only PDF files are supported.")
    try:
        report = analyze_document(data, file.filename or "document.pdf")
    except Exception as e:
        raise HTTPException(500, f"Analysis failed: {e}")
    return JSONResponse(report)


@app.get("/api/v1/version", response_class=PlainTextResponse)
async def version():
    return ENGINE_VERSION

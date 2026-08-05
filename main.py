"""
FastAPI uygulaması: web arayüzü + REST web servisi.
FastAPI application: web UI + REST web service.
"""
from __future__ import annotations

import os
import json
from typing import Any, Optional

from fastapi import FastAPI, UploadFile, File, Request, Header, HTTPException, Form
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from analyze import analyze_document, prepare_input, ENGINE_VERSION
from api_response import build_summary
from i18n import t as i18n_t

BASE = os.path.dirname(__file__)
MAX_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 25 * 1024 * 1024))  # 25 MB
API_KEYS = {k.strip() for k in os.environ.get("DEKONT_API_KEYS", "").split(",") if k.strip()}

_API_DESC = """
Banka dekontlarında **tahrifat/oynama, yapay zeka izi ve sahtecilik** tespiti yapan
adli analiz servisi. Yüklenen PDF veya fotoğrafı analiz eder ve dekontun durumunu
değerlendirmeye yetecek **temiz bir JSON** döndürür.

### Kesin cevaplar (belge tipine göre)
Sonuçlar belgenin **doğrulanabilirliğine** göre verilir:
- **digital** — gerçek dijital PDF: içerik ve zaman KESİN doğrulanır (true/false).
- **pdf_photo** — PDF içinde fotoğraf/tarama: PDF yapısı doğrulanır, piksel içeriği doğrulanamaz.
- **photo** — doğrudan fotoğraf: yapısal doğrulama yoktur; doğrulanamayan sorular **neutral** kalır.

`kesin_cevaplar` alanındaki değerler: `true` (olumlu/temiz), `false` (sorun/tahrifat),
`neutral` (analiz edilemedi). `evrakta_oynama`: `yok` | `var` | `belirsiz`.

### Uçlar
- `POST /api/v1/analyze` — tek dekont analizi (ana uç).
- `POST /api/v1/compare` — çoklu dekont çapraz karşılaştırma.
- `GET  /api/v1/health` — servis/OCR/Vision/veritabanı durumu.
- `GET  /api/v1/store/stats` — numara veritabanı istatistikleri.
"""

app = FastAPI(
    title="Dekont Doğrulama & Adli Analiz API",
    description=_API_DESC,
    version=ENGINE_VERSION,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)


# ----------------------------- CEVAP ŞEMALARI (OpenAPI) -----------------------------
class Dosya(BaseModel):
    ad: str = Field("", description="Dosya adı")
    boyut_bytes: int = Field(0, description="Boyut (bayt)")
    sha256: str = Field("", description="Dosya SHA-256 özeti")
    tur: str = Field("", description="Dosya türü: pdf | gorsel")


class Degerlendirme(BaseModel):
    skor: Optional[int] = Field(None, description="Doğruluk/güven puanı (0-100)")
    azami_skor: Optional[int] = Field(None, description="Bu belge tipi için ulaşılabilir en yüksek puan")
    risk_seviyesi: str = Field("", description="authentic | low | medium | high | critical")
    guvenilir_mi: str = Field("neutral", description="Genel karar: true | false | neutral")
    aciklama: str = Field("", description="Değerleme açıklaması (skor gerekçesi)")
    genel_karar_aciklama: str = Field("", description="Genel karar açıklaması")


class KesinCevaplar(BaseModel):
    gecerli_dekont: str = Field("neutral", description="Geçerli bir banka dekontu mu? true | false")
    evrakta_oynama: str = Field("belirsiz", description="Evrakta oynama/işlem var mı? yok | var | belirsiz")
    zaman_tutarli: str = Field("neutral", description="İşlem ↔ dekont zamanı tutarlı mı? true | false | neutral")
    veri_tutarli: str = Field("neutral", description="Tutar/veri hesapları tutarlı mı? true | false | neutral")
    numara_celiskisi_yok: str = Field("neutral", description="Numara geçmişiyle çelişki yok mu? true | false | neutral")


class Bilgiler(BaseModel):
    banka: str = ""
    gonderici_ad_soyad: str = ""
    gonderici_iban: str = ""
    gonderici_banka: str = ""
    alici_ad_soyad: str = ""
    alici_iban: str = ""
    alici_banka: str = ""
    tutar: Optional[float] = Field(None, description="İşlem tutarı")
    para_birimi: str = ""
    masraf: Optional[float] = None
    toplam: Optional[float] = None
    islem_tarihi: str = Field("", description="gg.aa.yyyy")
    islem_saati: str = Field("", description="ss:dd:ss")
    islem_numarasi: str = ""
    referans_no: str = ""
    islem_turu: str = ""
    islem_kanali: str = ""
    aciklama: str = ""


class Zaman(BaseModel):
    islem_zamani: str = ""
    dekont_olusturma: str = ""
    dekont_degistirme: str = ""


class Bulgu(BaseModel):
    kod: str = ""
    onem: str = Field("", description="critical | high | medium | low | info")
    aciklama: str = ""


class AnalyzeResponse(BaseModel):
    basarili: bool = True
    motor_surumu: str = ""
    analiz_zamani: str = ""
    dosya: Dosya
    dekont_mu: bool = Field(..., description="Yüklenen dosya bir banka dekontu mu?")
    dogrulama_modu: str = Field("", description="digital | pdf_photo | photo")
    dogrulama_modu_aciklama: str = ""
    degerlendirme: Degerlendirme
    kesin_cevaplar: KesinCevaplar
    islem_tespit_edildi: bool = Field(False, description="Evrakta bir işlem/dekont içeriği tespit edildi mi?")
    bilgiler: Bilgiler
    zaman: Zaman
    bulgular: list[Bulgu] = []
    detay: dict[str, Any] = Field(default_factory=dict, description="Tam ayrıntılı iç rapor (isteğe bağlı)")

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
    try:
        prepared, kind = prepare_input(data, file.filename or "")
    except Exception:
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION, "error": T["err_not_pdf"]},
            status_code=400)
    try:
        report = analyze_document(prepared, file.filename or "document.pdf", input_kind=kind)
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
    import ocr, vision_ocr, store
    return {"status": "ok", "engine_version": ENGINE_VERSION,
            "ocr_available": ocr.ocr_available(), "ocr_lang": ocr.best_lang(),
            "vision_configured": vision_ocr.is_configured(),
            "store_enabled": store.enabled(),
            "store_count": store.stats().get("count", 0),
            "api_key_required": bool(API_KEYS)}


@app.get("/api/v1/store/stats")
async def store_stats(x_api_key: str | None = Header(default=None)):
    _check_api_key(x_api_key)
    import store
    return store.stats()


@app.post("/api/v1/analyze", response_model=AnalyzeResponse,
          summary="Tek dekont analizi",
          response_description="Dekont durumunu değerlendirmeye yeten temiz sonuç")
async def analyze_api(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    """
    Yüklenen bir dekontu (PDF veya fotoğraf) analiz eder ve **dekontun durumunu
    değerlendirmeye yetecek** temiz bir JSON döndürür.

    Cevaptaki başlıca alanlar:
    - **dekont_mu** — dosya bir banka dekontu mu (true/false)
    - **degerlendirme.skor / azami_skor / aciklama** — değerleme skoru ve açıklaması
    - **degerlendirme.guvenilir_mi** — genel karar (true | false | neutral)
    - **kesin_cevaplar.evrakta_oynama** — evrakta oynama var mı (yok | var | belirsiz)
    - **kesin_cevaplar.zaman_tutarli** — işlem ↔ dekont zamanı tutarlı mı
    - **islem_tespit_edildi** — evrakta işlem/dekont içeriği tespit edildi mi
    - **bilgiler** — gönderici/alıcı ad-IBAN, tutar, işlem tarihi/saati, işlem numarası vb.
    - **detay** — tam ayrıntılı iç rapor (isteğe bağlı kullanım)

    Parametreler:
    - **file**: multipart/form-data PDF veya görsel (JPG/PNG) dosyası
    - **X-API-Key**: (opsiyonel) sunucu yapılandırılmışsa zorunludur
    """
    _check_api_key(x_api_key)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large.")
    try:
        prepared, kind = prepare_input(data, file.filename or "")
    except Exception:
        raise HTTPException(415, "Only PDF or image (JPG/PNG) files are supported.")
    try:
        report = analyze_document(prepared, file.filename or "document.pdf", input_kind=kind)
    except Exception as e:
        raise HTTPException(500, f"Analysis failed: {e}")
    return JSONResponse(build_summary(report))


@app.post("/api/v1/compare")
async def compare_api(files: list[UploadFile] = File(...), x_api_key: str | None = Header(default=None)):
    """
    Birden çok dekontu birlikte analiz eder ve ÇAPRAZ tutarlılık (işlem numarası ↔ zaman)
    değerlendirmesi döndürür. Aynı gönderenin sonraki işleminde numara artmalıdır.

    Analyze multiple receipts together and return a CROSS consistency assessment
    (transaction number ↔ time). A later transaction from the same sender must carry
    a higher number.

    - **files**: 2+ PDF veya görsel dosya (multipart)
    """
    _check_api_key(x_api_key)
    if not files or len(files) < 2:
        raise HTTPException(400, "Provide at least 2 files to compare.")
    from compare import compare_receipts
    reports = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"File too large: {f.filename}")
        try:
            prepared, kind = prepare_input(data, f.filename or "")
            reports.append(analyze_document(prepared, f.filename or "document.pdf", input_kind=kind, use_store=False))
        except Exception as e:
            raise HTTPException(500, f"Analysis failed for {f.filename}: {e}")
    if len(reports) < 2:
        raise HTTPException(400, "Need at least 2 valid files.")
    comparison = compare_receipts(reports)
    return JSONResponse({
        "basarili": True,
        "motor_surumu": ENGINE_VERSION,
        "capraz_degerlendirme": {
            "karar": comparison.get("verdict"),
            "aciklama": comparison.get("verdict_tr"),
            "kritik_sayisi": comparison.get("critical_count", 0),
            "bulgular": comparison.get("findings", []),
            "gruplar": comparison.get("groups", []),
        },
        "dekontlar": [build_summary(r) for r in reports],
    })


@app.post("/compare", response_class=HTMLResponse)
async def compare_web(request: Request, files: list[UploadFile] = File(...), lang: str = Form("tr")):
    L = _lang(request, lang)
    T = i18n_t(L)
    if not files or len([f for f in files if f.filename]) < 2:
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION,
             "error": T.get("err_need_two", "Karşılaştırma için en az 2 dosya yükleyin.")},
            status_code=400)
    from compare import compare_receipts
    reports = []
    for f in files:
        data = await f.read()
        if not data:
            continue
        if len(data) > MAX_BYTES:
            raise HTTPException(413, "File too large")
        try:
            prepared, kind = prepare_input(data, f.filename or "")
            reports.append(analyze_document(prepared, f.filename or "document.pdf", input_kind=kind, use_store=False))
        except Exception:
            continue
    if len(reports) < 2:
        return templates.TemplateResponse("index.html",
            {"request": request, "L": L, "T": T, "version": ENGINE_VERSION,
             "error": T.get("err_need_two", "Karşılaştırma için en az 2 geçerli dosya gerekir.")},
            status_code=400)
    comparison = compare_receipts(reports)
    return templates.TemplateResponse("compare.html", {
        "request": request, "L": L, "T": T, "version": ENGINE_VERSION,
        "c": comparison, "reports": reports,
        "comparison_json": json.dumps(comparison, ensure_ascii=False, indent=2),
    })


@app.get("/api/v1/version", response_class=PlainTextResponse)
async def version():
    return ENGINE_VERSION

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
    gecerli_belge: str = Field("neutral", description="Geçerli bir dekont/hesap hareketi belgesi mi? true | false")
    evrakta_oynama: str = Field("belirsiz", description="Evrakta oynama/işlem var mı? yok | var | belirsiz")
    zaman_tutarli: str = Field("neutral", description="İşlem ↔ belge zamanı tutarlı mı? true | false | neutral")
    veri_tutarli: str = Field("neutral", description="Tutar/veri hesapları tutarlı mı? true | false | neutral")
    bakiye_zinciri_tutarli: str = Field("neutral", description="(Hesap hareketi) yürüyen bakiye zinciri tutarlı mı? true | false | neutral")
    numara_celiskisi_yok: str = Field("neutral", description="Numara geçmişiyle çelişki yok mu? true | false | neutral")


class BakiyeKirilmasi(BaseModel):
    tarih: str = ""
    tutar: Optional[float] = None
    bakiye: Optional[float] = None
    beklenen_onceki_bakiye: Optional[float] = None
    gercek_onceki_bakiye: Optional[float] = None
    fark: Optional[float] = None
    satir: str = ""


class HesapHareketi(BaseModel):
    hesap_sahibi: str = ""
    iban: str = ""
    hesap_tipi: str = ""
    hesap_no: str = ""
    sube: str = ""
    donem_baslangic: str = ""
    donem_bitis: str = ""
    seri_sira_no: str = ""
    islem_sayisi: int = 0
    acilis_bakiye: Optional[float] = None
    kapanis_bakiye: Optional[float] = None
    net_degisim: Optional[float] = None
    bakiye_zinciri_tutarli: Optional[bool] = Field(None, description="Yürüyen bakiye zinciri tutarlı mı (true/false/null)")
    bakiye_kirilma_sayisi: int = 0
    bakiye_kirilmalari: list[BakiyeKirilmasi] = []
    beyan_edilen_kayit: Optional[int] = Field(None, description="Belgede beyan edilen kayıt sayısı")
    satir_sayisi_tutarli: Optional[bool] = Field(None, description="Beyan edilen ↔ gerçek satır sayısı tutarlı mı")


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
    kategori: str = Field("", description="content | metadata | fonts | image")
    agirlik: float = Field(0, description="Puan etkisi (+ceza / −doğrulayıcı)")
    aciklama: str = Field("", description="Türkçe açıklama (web arayüzdeki metnin aynısı)")
    aciklama_en: str = Field("", description="İngilizce açıklama")
    detay: str = Field("", description="Teknik detay (ör. producer=PDFium, rail=fast fee=8.37)")


class TahrifatSatiri(BaseModel):
    alan: str = Field("", description="Değiştirilen/çelişen alan")
    orijinal: str = Field("", description="Orijinal / olması gereken değer")
    degistirilmis: str = Field("", description="Değiştirilmiş / belgedeki değer")
    durum: str = ""
    kaynak: str = Field("", description="İçerik (revizyon) | Metadata (tarih) | İçerik (bakiye zinciri)")
    onem: str = ""


class AnalyzeResponse(BaseModel):
    basarili: bool = True
    motor_surumu: str = ""
    analiz_zamani: str = ""
    dosya: Dosya
    belge_turu: str = Field("", description="dekont | hesap_hareketi | diger")
    belge_turu_aciklama: str = ""
    dekont_mu: bool = Field(..., description="Yüklenen dosya bir banka dekontu mu?")
    hesap_hareketi_mi: bool = Field(False, description="Yüklenen dosya bir hesap hareketi/özeti mi?")
    dogrulama_modu: str = Field("", description="digital | pdf_photo | photo")
    dogrulama_modu_aciklama: str = ""
    degerlendirme: Degerlendirme
    kesin_cevaplar: KesinCevaplar
    islem_tespit_edildi: bool = Field(False, description="Evrakta bir işlem/dekont içeriği tespit edildi mi?")
    bilgiler: Bilgiler
    zaman: Zaman
    hesap_hareketi: Optional[HesapHareketi] = Field(None, description="Belge bir hesap hareketiyse doldurulur")
    tahrifat_karsilastirmasi: list[TahrifatSatiri] = Field(default_factory=list,
        description="Değiştirilen/çelişen alanlar: alan · orijinal · değiştirilmiş · durum")
    bulgular: list[Bulgu] = []
    kara_liste: dict[str, Any] = Field(default_factory=dict,
        description="Kara-liste eşleşmesi (SAHTE hükmü DEĞİL; yalnızca 'daha önce işaretlenmişti' bilgisi). "
                    "Alanlar: eslesme (bool), aciklama, detay, not")
    detay: dict[str, Any] = Field(default_factory=dict, description="Tam ayrıntılı iç rapor (isteğe bağlı)")


class UrlBody(BaseModel):
    url: str = Field(..., description="İndirilecek public dosya adresi (PDF veya görsel), ör. S3 URL'si",
                     examples=["https://bucket.s3.amazonaws.com/dekont.pdf"])


class UrlsBody(BaseModel):
    urls: list[str] = Field(..., min_length=2,
                            description="Karşılaştırılacak 2+ public dosya adresi")


app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


def _asset_version() -> str:
    """Statik CSS içeriğinin kısa hash'i — tarayıcı önbelleğini otomatik kırmak için."""
    import hashlib
    try:
        with open(os.path.join(BASE, "static", "style.css"), "rb") as _f:
            return hashlib.sha1(_f.read()).hexdigest()[:8]
    except Exception:
        return str(ENGINE_VERSION)


templates.env.globals["asset_v"] = _asset_version()


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
    import ocr, vision_ocr, store, ai_adjudicator
    return {"status": "ok", "engine_version": ENGINE_VERSION,
            "ocr_available": ocr.ocr_available(), "ocr_lang": ocr.best_lang(),
            "vision_configured": vision_ocr.is_configured(),
            "ai_adjudicator_enabled": ai_adjudicator.is_enabled(),
            "store_enabled": store.enabled(),
            "store_count": store.stats().get("count", 0),
            "api_key_required": bool(API_KEYS)}


@app.get("/api/v1/scan_log")
async def scan_log_api(q: str | None = None, limit: int = 20):
    """Web/API taramalarının kaydı: sorgu/ref no, gönderici/alıcı adı, banka ya da sha (parça) ile ara.
    q boşsa en son taramalar. Kullanıcı dekontu yükleyince 'web ne demişti vs gerçek ne' karşılaştırması için."""
    import store as _st
    limit = max(1, min(int(limit or 20), 100))
    rows = _st.scan_log_search(q, limit) if q else _st.scan_log_recent(limit)
    return {"query": q or "", "count": len(rows), "results": rows}


@app.get("/api/v1/self_check")
async def self_check_api():
    """Motorun ÖZ-DENETİMİ: canlı koda karşı tüm değişmez (invariant) testlerini çalıştırır.
    all_ok=false ise bir iyileştirme ezilmiş demektir. Zamanlanmış görev bunu periyodik yoklar."""
    import self_check
    return self_check.run()


@app.get("/gunluk", response_class=HTMLResponse)
async def gunluk_page():
    """Geliştirme günlüğü + canlı öz-denetim durumu (web'de görülebilir kayıt defteri)."""
    import self_check
    r = self_check.run()
    return HTMLResponse(_render_gunluk(r))


def _render_gunluk(r: dict) -> str:
    import html as _h
    ok_all = r["all_ok"]
    banner_bg = "#0f5132" if ok_all else "#842029"
    banner_tx = ("TÜM İYİLEŞTİRMELER KORUNUYOR" if ok_all
                 else "DİKKAT — BİR İYİLEŞTİRME EZİLMİŞ")
    # Öz-denetim kartları
    checks_html = ""
    for c in r["checks"]:
        badge = "#198754" if c["ok"] else "#dc3545"
        mark = "GEÇTİ" if c["ok"] else "EZİLMİŞ"
        detail = f'<div class="det">{_h.escape(str(c["detail"]))}</div>' if not c["ok"] else ""
        cdate = _h.escape(str(c.get("date") or "önceki"))
        checks_html += (
            f'<div class="chk"><span class="dot" style="background:{badge}"></span>'
            f'<span class="cid">#{c["id"]}</span><span class="cn">{_h.escape(c["name"])}</span>'
            f'<span class="cdate">{cdate}</span>'
            f'<span class="cm" style="color:{badge}">{mark}</span>{detail}</div>')
    # Geliştirme günlüğü kartları
    imp_html = ""
    for it in r["improvements"]:
        tno = f'#{it["test"]}' if it.get("test") else "—"
        imp_html += (
            f'<div class="imp"><div class="imp-h"><span class="tag">{_h.escape(it["id"])}</span>'
            f'<span class="area">{_h.escape(it["area"])}</span>'
            f'<span class="date">{_h.escape(it["date"])}</span>'
            f'<span class="tno">test {tno}</span></div>'
            f'<div class="row"><b>Bulunan Hata</b><p>{_h.escape(it["bug"])}</p></div>'
            f'<div class="row"><b>Yapılan Değişiklik</b><p>{_h.escape(it["fix"])}</p></div></div>')
    rules_html = "".join(f"<li>{_h.escape(x)}</li>" for x in r["invariant_rules"])
    return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Geliştirme Günlüğü — dekont-analiz</title>
<style>
*{{box-sizing:border-box}}body{{margin:0;background:#0d1117;color:#e6edf3;
font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5}}
.wrap{{max-width:960px;margin:0 auto;padding:24px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:#8b949e;font-size:14px;margin-bottom:20px}}
.banner{{background:{banner_bg};padding:14px 18px;border-radius:10px;font-weight:700;
display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}}
.banner .cnt{{font-weight:400;opacity:.85;font-size:14px}}
.sec{{margin-top:26px;font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e}}
.chk{{display:flex;align-items:center;gap:10px;padding:9px 12px;border:1px solid #21262d;
border-radius:8px;margin:6px 0;flex-wrap:wrap;background:#161b22}}
.dot{{width:10px;height:10px;border-radius:50%;display:inline-block;flex:0 0 auto}}
.cid{{color:#8b949e;font-variant-numeric:tabular-nums;min-width:26px}}
.cn{{flex:1;min-width:180px}}.cm{{font-weight:700;font-size:13px;min-width:64px;text-align:right}}
.cdate{{color:#8b949e;font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}}
.det{{flex-basis:100%;color:#f0a;font-size:13px;padding-left:20px}}
.imp{{border:1px solid #21262d;border-radius:10px;padding:14px 16px;margin:10px 0;background:#161b22}}
.imp-h{{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:8px}}
.tag{{background:#1f6feb;color:#fff;border-radius:6px;padding:1px 9px;font-weight:700;font-size:13px}}
.area{{font-weight:600}}.date,.tno{{color:#8b949e;font-size:13px}}.tno{{margin-left:auto}}
.row{{margin:6px 0}}.row b{{display:block;color:#8b949e;font-size:12px;text-transform:uppercase;letter-spacing:.4px}}
.row p{{margin:2px 0 0}}
ul{{color:#8b949e;font-size:14px}} a{{color:#58a6ff}}
.foot{{margin-top:28px;color:#8b949e;font-size:13px}}
</style></head><body><div class="wrap">
<h1>Geliştirme Günlüğü &amp; Öz-Denetim</h1>
<div class="sub">Bulunan Hata → Yapılan Değişiklik · her iyileştirme bir değişmez testiyle korunuyor</div>
<div class="banner"><span>{banner_tx}</span><span class="cnt">{r['passed']}/{r['total']} test · en son kontrol: {_h.escape(str(r.get('generated_at','')))} (TR)</span></div>
<div class="sec">Canlı Öz-Denetim (invariant testleri)</div>
{checks_html}
<div class="sec">Değişiklik Kayıt Defteri</div>
{imp_html}
<div class="sec">Değişmez Kurallar</div>
<ul>{rules_html}</ul>
<div class="foot">JSON: <a href="/api/v1/self_check">/api/v1/self_check</a> · Sağlık: <a href="/api/v1/health">/api/v1/health</a></div>
</div></body></html>"""


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


# =====================================================================
#  VİDEO ADLİ ANALİZİ — TAMAMEN AYRI YENİ SERVİS
#  ÖNEMLİ: Mevcut dekont uçları (/api/v1/analyze, /compare vb.) ve onların cevap
#  ANAHTARLARI hiç değişmez. Bu yalnızca YENİ bir URL ve KENDİ şemasını döndürür.
# =====================================================================
MAX_VIDEO_BYTES = int(os.environ.get("DEKONT_VIDEO_MAX_BYTES", 200 * 1024 * 1024))  # 200 MB
_VIDEO_EXT = (".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi", ".3gp", ".3g2", ".ts", ".mpg", ".mpeg")


@app.post("/api/v1/analyze-video",
          summary="Video adli analizi (sahte/düzenlenmiş/yeniden-kodlanmış/AI video tespiti)",
          response_description="Videonun ham çekim mi yoksa üretilmiş/düzenlenmiş mi olduğuna dair ayrı JSON")
async def analyze_video_api(file: UploadFile = File(...), x_api_key: str | None = Header(default=None)):
    """
    Yüklenen bir VİDEOYU adli olarak değerlendirir: konteyner/encoder metadata'sı (FFmpeg/düzenleyici/
    AI üretici imzaları, cihaz oluşturma zamanı) + kare-düzeyi analiz (ELA, gürültü, moiré/yeniden-çekim).
    Videonun ham cihaz çekimi mi yoksa ÜRETİLMİŞ/DÜZENLENMİŞ/YENİDEN-KODLANMIŞ mı olduğuna dair
    KENDİ şemasında JSON döndürür (score/risk/verdict/signals/container/encoding/frames).

    Bu uç mevcut dekont servislerinden BAĞIMSIZDIR; onların URL'lerini ve cevap anahtarlarını etkilemez.
    """
    _check_api_key(x_api_key)
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "Video too large.")
    import tempfile
    import video_forensics as _vf
    suffix = ".mp4"
    _fn = (file.filename or "").lower()
    for e in _VIDEO_EXT:
        if _fn.endswith(e):
            suffix = e
            break
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        result = _vf.analyze_video(tmp.name)
    except Exception as e:
        raise HTTPException(500, f"Video analysis failed: {e}")
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    result["file"] = {"name": file.filename or "video.mp4", "size_bytes": len(data)}
    return JSONResponse(result)


@app.get("/video", response_class=HTMLResponse, summary="Video analizi (tarayıcı formu)")
async def video_page(request: Request):
    """Videoyu tarayıcıdan yükleyip analiz etmek için basit sayfa (yeni URL, mevcut sayfaları etkilemez)."""
    return HTMLResponse(_VIDEO_HTML)


@app.post("/video", response_class=HTMLResponse)
async def video_web(file: UploadFile = File(...)):
    data = await file.read()
    if not data:
        return HTMLResponse("<p>Boş dosya.</p>", status_code=400)
    if len(data) > MAX_VIDEO_BYTES:
        return HTMLResponse("<p>Video çok büyük.</p>", status_code=413)
    import tempfile
    import video_forensics as _vf
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        r = _vf.analyze_video(tmp.name)
    except Exception as e:
        return HTMLResponse(f"<p>Analiz hatası: {e}</p>", status_code=500)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass
    return HTMLResponse(_render_video_report(r))


_VIDEO_CSS = """<style>
:root{--brand:#5b3df5;--brand-d:#4a30d0;--ink:#0f172a;--muted:#64748b;--line:#e6e8ef;--bg:#f5f6fb;--card:#fff}
*{box-sizing:border-box}
html,body{margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,system-ui,sans-serif;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
a{color:var(--brand)}
.topbar{background:#fff;border-bottom:1px solid var(--line)}
.topbar .in{max-width:940px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:11px}
.logo{width:32px;height:32px;border-radius:9px;background:var(--brand);color:#fff;display:grid;place-items:center;font-weight:800;font-size:17px;box-shadow:0 4px 12px rgba(91,61,245,.28)}
.brand{font-weight:700;font-size:16px}
.brand small{display:block;font-weight:500;font-size:11px;color:var(--muted);margin-top:1px}
.wrap{max-width:940px;margin:0 auto;padding:24px 20px 48px}
.hero{background:var(--card);border:1px solid var(--line);border-radius:20px;padding:26px;display:flex;gap:28px;align-items:center;flex-wrap:wrap;box-shadow:0 1px 3px rgba(15,23,42,.04)}
.ring{width:140px;height:140px;border-radius:50%;background:conic-gradient(var(--rc) calc(var(--rv)*1deg),#eceef4 0);display:grid;place-items:center;flex:0 0 auto}
.ring .core{width:110px;height:110px;border-radius:50%;background:#fff;display:grid;place-items:center;text-align:center;box-shadow:inset 0 0 0 1px #eceef4}
.ring .sc{font-size:34px;font-weight:800;line-height:1;color:var(--ink)}
.ring .sc span{font-size:13px;color:var(--muted);font-weight:600}
.hero-main{flex:1 1 340px;min-width:280px}
.badge{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:800;font-size:12px;letter-spacing:.05em;color:#fff}
.h-title{font-size:13px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:0 0 10px}
.verdict{margin:13px 0 0;color:#334155;line-height:1.6;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin-top:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px}
.card .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:700}
.card .v{font-size:17px;font-weight:700;margin-top:5px;color:var(--ink)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:7px}
.chip{display:inline-flex;align-items:center;gap:5px;padding:4px 10px;border-radius:999px;font-size:12px;font-weight:700;line-height:1}
.chip.ok{background:#dcfce7;color:#15803d}.chip.warn{background:#fef3c7;color:#b45309}.chip.bad{background:#fee2e2;color:#b91c1c}.chip.neu{background:#eef1f6;color:#475569}.chip.info{background:#e0edff;color:#1d4ed8}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:#475569;background:#f1f4f9;border:1px solid var(--line);padding:9px 11px;border-radius:9px;word-break:break-all;margin-top:8px;line-height:1.5}
.sec{font-size:13px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:26px 0 12px}
.sig{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--sc,#94a3b8);border-radius:12px;padding:15px 17px;margin-bottom:11px}
.sig .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sig .code{font-weight:800;font-size:14px;letter-spacing:.02em}
.sig .desc{color:#475569;line-height:1.6;margin-top:8px;font-size:14px}
.empty{background:#dcfce7;border:1px solid #bbf7d0;color:#166534;border-radius:12px;padding:15px 17px;font-weight:600}
.btn{display:inline-flex;align-items:center;gap:8px;background:var(--brand);color:#fff;border:0;padding:12px 22px;border-radius:11px;font-size:15px;font-weight:700;cursor:pointer;text-decoration:none;box-shadow:0 6px 16px rgba(91,61,245,.26)}
.btn:hover{background:var(--brand-d)}
.foot{color:var(--muted);font-size:12px;margin-top:22px;line-height:1.55}
.drop{display:block;background:var(--card);border:2px dashed #cdd3e3;border-radius:18px;padding:40px 28px;text-align:center;cursor:pointer;transition:border-color .15s,background .15s}
.drop input[type=file]{display:block;margin:0 auto}
.drop:hover{border-color:var(--brand);background:#fbfaff}
.drop .ic{width:52px;height:52px;margin:0 auto 12px;color:var(--brand)}
.drop h3{margin:0 0 6px;font-size:18px}
.drop .sub{color:var(--muted);font-size:13px;margin-bottom:18px}
.file{font-size:14px}
</style>"""

_VIDEO_HTML = """<!DOCTYPE html><html lang='tr'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Video Adli Analizi</title>""" + _VIDEO_CSS + """</head><body>
<header class='topbar'><div class='in'>
  <div class='logo'>D</div>
  <div class='brand'>Dekont Adli Analiz<small>Video doğrulama</small></div>
</div></header>
<div class='wrap'>
  <p class='h-title'>Video Adli Analizi</p>
  <p style='color:#475569;line-height:1.6;margin:0 0 20px'>Sahte, düzenlenmiş, yeniden-kodlanmış veya yapay zeka ile üretilmiş videoları tespit eder. Videoyu yükleyin; konteyner/encoder imzası, kare örnekleme ve yeniden-çekim analizini otomatik çalıştıralım.</p>
  <form action='/video' method='post' enctype='multipart/form-data'>
    <label class='drop' for='vf'>
      <div class='ic'><svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='m22 8-6 4 6 4V8Z'/><rect x='2' y='6' width='14' height='12' rx='2'/></svg></div>
      <h3>Video yükleyin</h3>
      <div class='sub'>MP4, MOV, M4V, WEBM, AVI · en fazla 200 MB</div>
      <input class='file' type='file' id='vf' name='file' accept='video/*' required>
    </label>
    <div style='text-align:center;margin-top:18px'>
      <button class='btn' type='submit'>
        <svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M14 3v4a1 1 0 0 0 1 1h4'/><path d='M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2z'/></svg>
        Analiz Et
      </button>
    </div>
  </form>
  <p class='foot'>Dosyanız yalnızca analiz için işlenir, saklanmaz ve üçüncü taraflarla paylaşılmaz. Video analizi olasılıksaldır; tek başına kesin delil değildir.</p>
</div></body></html>"""


# --- Video rapor sayfası (yalnızca SUNUM; API JSON'una / anahtarlarına dokunmaz) ---
_VRISK = {
    "kritik":  ("#dc2626", "KRİTİK"),
    "yüksek":  ("#ea580c", "YÜKSEK"),
    "orta":    ("#d97706", "ORTA"),
    "düşük":   ("#16a34a", "DÜŞÜK"),
}
_VSEV = {
    "critical": ("#dc2626", "bad",  "KRİTİK"),
    "kritik":   ("#dc2626", "bad",  "KRİTİK"),
    "high":     ("#ea580c", "warn", "YÜKSEK"),
    "yüksek":   ("#ea580c", "warn", "YÜKSEK"),
    "medium":   ("#d97706", "warn", "ORTA"),
    "orta":     ("#d97706", "warn", "ORTA"),
    "low":      ("#2563eb", "info", "DÜŞÜK"),
    "düşük":    ("#2563eb", "info", "DÜŞÜK"),
    "info":     ("#3b82f6", "info", "BİLGİ"),
    "positive": ("#16a34a", "ok",   "OLUMLU"),
    "olumlu":   ("#16a34a", "ok",   "OLUMLU"),
}


def _vchip(cls: str, text: str) -> str:
    import html as _h
    return f"<span class='chip {cls}'>{_h.escape(str(text))}</span>"


def _render_video_report(r: dict) -> str:
    import html as _h
    cont = r.get("container", {}) or {}
    enc = r.get("encoding", {}) or {}
    frm = r.get("frames", {}) or {}
    score = r.get("score", 0)
    try:
        _sv = max(0, min(100, float(score)))
    except Exception:
        _sv = 0
    rc, rlabel = _VRISK.get(str(r.get("risk", "")).lower(), ("#64748b", str(r.get("risk", "")).upper()))
    verdict = _h.escape(r.get("verdict_tr", "") or "")

    # Konteyner/encoder bilgi kartları
    ct = bool(cont.get("creation_time_present"))
    ct_chip = _vchip("ok", "var") if ct else _vchip("warn", "yok")
    ffmpeg = bool(enc.get("ffmpeg_encode"))
    ff_chip = _vchip("warn", "yeniden-kodlanmış") if ffmpeg else _vchip("ok", "ham/orijinal")
    editors = enc.get("editor_hits") or []
    ed_chip = _vchip("bad", ", ".join(map(str, editors))) if editors else _vchip("ok", "yok")
    ais = enc.get("ai_hits") or []
    ai_chip = _vchip("bad", ", ".join(map(str, ais))) if ais else _vchip("ok", "yok")

    recap = bool(frm.get("recapture_suspected"))
    recap_chip = _vchip("ok", "evet (olumlu)") if recap else _vchip("neu", "hayır")
    localedit = bool(frm.get("ela_hotspot_concentrated"))
    le_chip = _vchip("bad", "evet") if localedit else _vchip("ok", "hayır")

    codec = _h.escape(str(enc.get("codec", "—")))
    w, hgt = enc.get("width", "?"), enc.get("height", "?")
    dur = r.get("duration_sec", "—")
    sampled = frm.get("sampled", "—")
    moire = frm.get("moire_max", "—")

    # Encoder metni: '[0][0][0][0]' gibi gürültüyü temizle
    enc_txt = str(cont.get("encoder_text", "") or "")
    enc_txt = enc_txt.replace("[0][0][0][0]", "").replace("  ", " ").strip() or "—"
    enc_txt = _h.escape(enc_txt[:160])

    cards = f"""
      <div class='card'><div class='k'>Süre</div><div class='v'>{_h.escape(str(dur))} sn</div></div>
      <div class='card'><div class='k'>Çözünürlük</div><div class='v'>{w}×{hgt}</div><div class='chips'>{_vchip('neu', codec)}</div></div>
      <div class='card'><div class='k'>Oluşturma zamanı</div><div class='chips'>{ct_chip}</div></div>
      <div class='card'><div class='k'>Kodlama</div><div class='chips'>{ff_chip}</div></div>
      <div class='card'><div class='k'>Düzenleyici izi</div><div class='chips'>{ed_chip}</div></div>
      <div class='card'><div class='k'>Yapay zeka izi</div><div class='chips'>{ai_chip}</div></div>
      <div class='card'><div class='k'>Kare örnekleme</div><div class='v'>{_h.escape(str(sampled))} kare</div><div class='chips'>{_vchip('neu', f'moiré {moire}')}</div></div>
      <div class='card'><div class='k'>Yeniden-çekim</div><div class='chips'>{recap_chip}</div></div>
      <div class='card'><div class='k'>Lokal düzenleme</div><div class='chips'>{le_chip}</div></div>
    """

    sigs = r.get("signals", []) or []
    if sigs:
        sig_html = ""
        for s in sigs:
            sc, cls, lbl = _VSEV.get(str(s.get("severity", "")).lower(), ("#94a3b8", "neu", str(s.get("severity", "")).upper()))
            sig_html += (
                f"<div class='sig' style='--sc:{sc}'>"
                f"<div class='top'><span class='code'>{_h.escape(str(s.get('code','')))}</span>{_vchip(cls, lbl)}</div>"
                f"<div class='desc'>{_h.escape(str(s.get('tr','')))}</div></div>"
            )
    else:
        sig_html = "<div class='empty'>Belirgin bir sahtecilik/düzenleme sinyali bulunamadı.</div>"

    return f"""<!DOCTYPE html><html lang='tr'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Video Adli Analizi — Sonuç</title>{_VIDEO_CSS}</head><body>
<header class='topbar'><div class='in'>
  <div class='logo'>D</div>
  <div class='brand'>Dekont Adli Analiz<small>Video doğrulama</small></div>
</div></header>
<div class='wrap'>
  <p class='h-title'>Video Adli Analizi</p>
  <div class='hero'>
    <div class='ring' style='--rc:{rc};--rv:{_sv*3.6:.1f}'>
      <div class='core'><div class='sc'>{score}<span>/100</span></div></div>
    </div>
    <div class='hero-main'>
      <span class='badge' style='background:{rc}'>{rlabel}</span>
      <p class='verdict'>{verdict}</p>
    </div>
  </div>

  <div class='sec'>Teknik özet</div>
  <div class='grid'>{cards}</div>
  {f"<div class='mono'>encoder: {enc_txt}</div>" if enc_txt != "—" else ""}

  <div class='sec'>Sinyaller</div>
  {sig_html}

  <a class='btn' href='/video' style='margin-top:22px'>
    <svg width='18' height='18' viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><path d='M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8'/><path d='M3 3v5h5'/></svg>
    Yeni analiz
  </a>
  <p class='foot'>Video analizi olasılıksaldır; tek başına kesin delil değildir. Kesinlik için orijinal kayıt ve zincir-of-custody bilgisi isteyin.</p>
</div></body></html>"""


@app.post("/api/v1/analyze-url", response_model=AnalyzeResponse,
          summary="URL ile tek dekont analizi",
          response_description="Dekont durumunu değerlendirmeye yeten temiz sonuç")
async def analyze_url_api(body: UrlBody, x_api_key: str | None = Header(default=None)):
    """
    Dosya yüklemek yerine **public bir URL** (ör. S3) verildiğinde, dosyayı sunucu indirip
    aynı analizi yapar ve `/api/v1/analyze` ile AYNI temiz JSON'u döndürür.

    Güvenlik: yalnızca http/https; iç/özel ağ adreslerine erişim engellenir (SSRF koruması);
    boyut ve zaman aşımı sınırı uygulanır. İzinli host'lar `DEKONT_URL_ALLOW_HOSTS` ile
    kısıtlanabilir.

    Gövde (JSON): `{ "url": "https://bucket.s3.amazonaws.com/dekont.pdf" }`
    """
    _check_api_key(x_api_key)
    import url_fetch
    try:
        data, fname = url_fetch.fetch(body.url, MAX_BYTES)
    except ValueError as e:
        raise HTTPException(400, str(e))
    try:
        prepared, kind = prepare_input(data, fname)
    except Exception:
        raise HTTPException(415, "URL yalnızca PDF veya görsel (JPG/PNG) içermelidir.")
    try:
        report = analyze_document(prepared, fname or "document.pdf", input_kind=kind)
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


@app.post("/api/v1/compare-url", summary="URL'lerle çapraz karşılaştırma")
async def compare_url_api(body: UrlsBody, x_api_key: str | None = Header(default=None)):
    """
    Birden çok public URL (S3 vb.) verildiğinde dosyaları sunucu indirip ÇAPRAZ karşılaştırma
    yapar. `/api/v1/compare` ile aynı sonucu döndürür. SSRF koruması uygulanır.

    Gövde (JSON): `{ "urls": ["https://.../dekont1.pdf", "https://.../dekont2.pdf"] }`
    """
    _check_api_key(x_api_key)
    if not body.urls or len(body.urls) < 2:
        raise HTTPException(400, "En az 2 URL gerekir.")
    from compare import compare_receipts
    import url_fetch
    reports = []
    for u in body.urls:
        try:
            data, fname = url_fetch.fetch(u, MAX_BYTES)
            prepared, kind = prepare_input(data, fname)
            reports.append(analyze_document(prepared, fname or "document.pdf", input_kind=kind, use_store=False))
        except ValueError as e:
            raise HTTPException(400, f"{u}: {e}")
        except Exception as e:
            raise HTTPException(500, f"Analysis failed for {u}: {e}")
    if len(reports) < 2:
        raise HTTPException(400, "En az 2 geçerli dosya gerekir.")
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

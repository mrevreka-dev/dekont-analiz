"""
Düşük seviyeli PDF yapı analizi / Low-level PDF structure analysis.

Bu modül, bir PDF'in ham baytları ve nesne yapısı üzerinden adli (forensic)
ipuçları çıkarır: incremental update (artımlı güncelleme) sayısı, xref/trailer
zinciri, üretici/oluşturucu bilgileri, tarih damgaları, XMP geçmişi, imza,
gömülü görseller ve yazı tipi (font) tutarlılığı.

Sadece dosya yapısına bakar; alan çıkarımı ve skorlama başka modüllerde.
"""
from __future__ import annotations

import re
import io
import hashlib
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any

import pikepdf


# Bir belgeyi ÜRETEN yazılımlar (banka/rapor sistemleri, gerçek üretimler)
GENERATOR_PRODUCERS = [
    "jasperreports", "openpdf", "itext", "adobe experience manager",
    "adobe livecycle", "reportlab", "pdfbox", "prince", "wkhtmltopdf",
    "pdftools sdk", "oracle", "sap", "crystal reports", "birt",
    "microsoft report", "telerik", "fastreport", "aspose", "docotic",
    "dynamicpdf", "spire.pdf", "syncfusion", "evopdf", "html to pdf",
    "pdfsharp", "migradoc", "flying saucer", "weasyprint", "puppeteer",
]

# Bir belgeyi DÜZENLEYEN / YENİDEN KAYDEDEN yazılımlar (oynama riski yüksek)
EDITOR_PRODUCERS = {
    "quartz pdfcontext": "macOS Önizleme/Quartz ile yeniden kaydedilmiş (macOS Preview re-save)",
    "cairo": "Cairo (GIMP/Inkscape/LibreOffice export) ile işlenmiş",
    "ilovepdf": "iLovePDF çevrimiçi aracı ile işlenmiş",
    "smallpdf": "Smallpdf çevrimiçi aracı ile işlenmiş",
    "pdfescape": "PDFescape editörü ile düzenlenmiş",
    "sejda": "Sejda editörü ile düzenlenmiş",
    "foxit": "Foxit editörü ile düzenlenmiş",
    "nitro": "Nitro PDF editörü ile düzenlenmiş",
    "pdf-xchange": "PDF-XChange editörü ile düzenlenmiş",
    "microsoft: print to pdf": "Microsoft Print to PDF ile yeniden basılmış",
    "ghostscript": "Ghostscript ile yeniden işlenmiş",
    "pypdf": "pypdf/PyPDF2 (script) ile programatik olarak değiştirilmiş",
    "pdf-lib": "pdf-lib (JS) ile programatik olarak değiştirilmiş",
    "pikepdf": "pikepdf (script) ile programatik olarak değiştirilmiş",
    "qpdf": "qpdf ile yeniden yazılmış",
    "acrobat": "Adobe Acrobat ile düzenlenmiş (üretim değil düzenleme)",
    "photoshop": "Adobe Photoshop üzerinden geçmiş (görsel düzenleme)",
    "canva": "Canva ile oluşturulmuş/düzenlenmiş",
    "microsoft office word": "Microsoft Word üzerinden dışa aktarılmış",
    "microsoft word": "Microsoft Word üzerinden dışa aktarılmış",
    "libreoffice": "LibreOffice üzerinden dışa aktarılmış",
}

# Tarayıcı motorları: birçok banka mobil/internet dekontunu sunucuda headless Chrome
# (Skia) ile üretir. Bu TEK BAŞINA tahrifat değildir; yalnızca artımlı kayıt / önce-üretici-
# sonra-editör / içerik revizyonu gibi başka sinyallerle birlikte anlam kazanır.
BROWSER_PRODUCERS = {
    "skia/pdf": "Tarayıcı motoru (Chrome/Skia) ile üretilmiş — banka mobil/İnternet dekontlarında olağan",
    "chromium": "Chromium tabanlı tarayıcı motoru ile üretilmiş — olağan",
}


def _clean(s: Any) -> str:
    if s is None:
        return ""
    return str(s).strip()


def _parse_pdf_date(raw: Any) -> _dt.datetime | None:
    """PDF tarih dizesini (D:YYYYMMDDHHmmSSOHH'mm') datetime'a çevirir."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s.startswith("D:"):
        s = s[2:]
    m = re.match(r"(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?", s)
    if not m:
        return None
    parts = [int(x) if x else (1 if i in (1, 2) else 0) for i, x in enumerate(m.groups())]
    y, mo, d, h, mi, se = parts
    try:
        base = _dt.datetime(y, max(mo, 1), max(d, 1), h, mi, se)
    except ValueError:
        return None
    tz = s[m.end():]
    tzm = re.match(r"([+\-Zz])(\d{2})?'?(\d{2})?", tz)
    if tzm and tzm.group(1) in "+-":
        sign = 1 if tzm.group(1) == "+" else -1
        oh = int(tzm.group(2) or 0)
        om = int(tzm.group(3) or 0)
        base = base - sign * _dt.timedelta(hours=oh, minutes=om)
    return base


@dataclass
class StructureReport:
    file_size: int = 0
    sha256: str = ""
    md5: str = ""
    pdf_version: str = ""
    page_count: int = 0
    page_sizes: list = field(default_factory=list)

    # Metadata
    producer: str = ""
    creator: str = ""
    author: str = ""
    title: str = ""
    creation_date: str = ""
    mod_date: str = ""
    creation_dt: Any = None
    mod_dt: Any = None
    xmp_present: bool = False
    xmp_create_date: str = ""
    xmp_modify_date: str = ""
    xmp_metadata_date: str = ""
    xmp_history: list = field(default_factory=list)
    doc_id_permanent: str = ""
    doc_id_changing: str = ""

    # Yapı / Structure
    eof_count: int = 0
    startxref_count: int = 0
    incremental_updates: int = 0        # ek revizyon sayısı (0 = tek kayıt)
    has_incremental_updates: bool = False
    uses_object_streams: bool = False
    linearized: bool = False
    has_prev_in_trailer: bool = False
    trailer_prev_offsets: list = field(default_factory=list)

    # İçerik göstergeleri
    text_char_count: int = 0
    is_encrypted: bool = False
    has_signature: bool = False
    signature_covers_whole: Any = None
    has_acroform: bool = False
    fields_flattened: Any = None
    annotation_count: int = 0
    markup_annotation_count: int = 0
    image_count: int = 0
    images: list = field(default_factory=list)
    fonts: list = field(default_factory=list)
    embedded_files: list = field(default_factory=list)
    js_present: bool = False

    # Türetilmiş bayraklar / signals (forensics modülü doldurur)
    doc_type: str = ""     # "digital_native" | "image_only" | "hybrid" | "scanned"

    raw_warnings: list = field(default_factory=list)


def analyze_structure(path: str) -> StructureReport:
    with open(path, "rb") as fh:
        data = fh.read()
    return analyze_structure_bytes(data)


def analyze_structure_bytes(data: bytes) -> StructureReport:
    rep = StructureReport()

    rep.file_size = len(data)
    rep.sha256 = hashlib.sha256(data).hexdigest()
    rep.md5 = hashlib.md5(data).hexdigest()

    # --- Ham bayt taraması (incremental update tespiti) ---
    rep.eof_count = len(re.findall(rb"%%EOF", data))
    rep.startxref_count = len(re.findall(rb"startxref", data))
    # Her ek %%EOF bir artımlı revizyon demektir
    rep.incremental_updates = max(0, rep.eof_count - 1)
    rep.has_incremental_updates = rep.incremental_updates > 0
    rep.linearized = b"/Linearized" in data[:2048]
    rep.uses_object_streams = b"/ObjStm" in data
    # Trailer içindeki /Prev -> önceki xref'e işaret (revizyon zinciri)
    prevs = re.findall(rb"/Prev\s+(\d+)", data)
    rep.has_prev_in_trailer = len(prevs) > 0
    rep.trailer_prev_offsets = [int(x) for x in prevs[:10]]
    rep.js_present = bool(re.search(rb"/JavaScript|/JS\b", data))

    # Linearized dosyalarda ilk kayıt normal ikinci %%EOF üretebilir; düzelt
    if rep.linearized and rep.incremental_updates > 0:
        rep.incremental_updates -= 1
        rep.has_incremental_updates = rep.incremental_updates > 0

    # --- pikepdf ile nesne düzeyi ---
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception as e:
        rep.raw_warnings.append(f"pikepdf açılamadı: {e}")
        return rep

    try:
        rep.pdf_version = str(pdf.pdf_version)
        rep.page_count = len(pdf.pages)
        rep.is_encrypted = pdf.is_encrypted
        for pg in pdf.pages:
            try:
                mb = pg.mediabox
                w = round(float(mb[2]) - float(mb[0]), 1)
                h = round(float(mb[3]) - float(mb[1]), 1)
                rep.page_sizes.append([w, h])
            except Exception:
                pass

        # Document Info sözlüğü
        docinfo = pdf.docinfo if pdf.docinfo is not None else {}
        rep.producer = _clean(docinfo.get("/Producer"))
        rep.creator = _clean(docinfo.get("/Creator"))
        rep.author = _clean(docinfo.get("/Author"))
        rep.title = _clean(docinfo.get("/Title"))
        rep.creation_date = _clean(docinfo.get("/CreationDate"))
        rep.mod_date = _clean(docinfo.get("/ModDate"))
        rep.creation_dt = _parse_pdf_date(rep.creation_date)
        rep.mod_dt = _parse_pdf_date(rep.mod_date)

        # Belge kimlikleri (/ID) — kalıcı ve değişen parça
        try:
            if "/ID" in pdf.trailer:
                ids = pdf.trailer["/ID"]
                rep.doc_id_permanent = bytes(ids[0]).hex()
                rep.doc_id_changing = bytes(ids[1]).hex()
        except Exception:
            pass

        # XMP metadata
        try:
            with pdf.open_metadata(set_pikepdf_as_editor=False) as meta:
                keys = dict(meta)
                if keys:
                    rep.xmp_present = True
                    rep.xmp_create_date = _clean(keys.get("xmp:CreateDate"))
                    rep.xmp_modify_date = _clean(keys.get("xmp:ModifyDate"))
                    rep.xmp_metadata_date = _clean(keys.get("xmp:MetadataDate"))
        except Exception:
            pass
        # XMP History (xmpMM:History) — ham XML'den çek
        try:
            xml = str(pdf.Root.Metadata.read_bytes(), "utf-8", "ignore") if "/Metadata" in pdf.Root else ""
            for m in re.finditer(r"stEvt:action>([^<]+)<.*?stEvt:softwareAgent>([^<]*)<", xml, re.S):
                rep.xmp_history.append({"action": m.group(1).strip(), "agent": m.group(2).strip()})
            if not rep.xmp_history:
                for act in re.findall(r"stEvt:action>([^<]+)<", xml):
                    rep.xmp_history.append({"action": act.strip(), "agent": ""})
        except Exception:
            pass

        # AcroForm / imza / annotation
        root = pdf.Root
        rep.has_acroform = "/AcroForm" in root
        if rep.has_acroform:
            try:
                af = root["/AcroForm"]
                flds = af.get("/Fields", [])
                rep.fields_flattened = (len(flds) == 0)
                for f in flds:
                    if f.get("/FT") == "/Sig" and f.get("/V") is not None:
                        rep.has_signature = True
            except Exception:
                pass
        # Sayfa annotation'ları.
        # Link (köprü, ör. bankanın kendi web adresi), Popup ve Widget (form alanı) OLAĞANDIR;
        # oynama şüphesi asıl MARKUP/üzerine-ekleme türlerindedir (metin kutusu, damga, vurgu,
        # karalama, redaksiyon, yapışkan not, dosya eki ...). Bu ayrım yanlış alarmı önler.
        _BENIGN_ANNOTS = {"/Link", "/Popup", "/Widget"}
        try:
            for pg in pdf.pages:
                annots = pg.get("/Annots", [])
                rep.annotation_count += len(annots)
                for a in annots:
                    try:
                        sub = str(a.get("/Subtype", ""))
                    except Exception:
                        sub = ""
                    if sub and sub not in _BENIGN_ANNOTS:
                        rep.markup_annotation_count += 1
        except Exception:
            pass

        # Gömülü dosyalar
        try:
            names = root.get("/Names", {})
            ef = names.get("/EmbeddedFiles") if names else None
            if ef and "/Names" in ef:
                arr = ef["/Names"]
                for i in range(0, len(arr), 2):
                    rep.embedded_files.append(_clean(arr[i]))
        except Exception:
            pass

        # Görseller ve fontlar (sayfa kaynaklarından)
        _collect_images_fonts(pdf, rep)

    finally:
        pdf.close()

    return rep


def _collect_images_fonts(pdf: "pikepdf.Pdf", rep: StructureReport) -> None:
    seen_imgs = set()
    seen_fonts = {}
    for pg in pdf.pages:
        res = pg.get("/Resources", {})
        # XObject görseller
        xobjs = res.get("/XObject", {}) if res else {}
        try:
            for name, xobj in dict(xobjs).items():
                try:
                    if xobj.get("/Subtype") == "/Image":
                        oid = (int(xobj.objgen[0]), int(xobj.objgen[1]))
                        if oid in seen_imgs:
                            continue
                        seen_imgs.add(oid)
                        w = int(xobj.get("/Width", 0))
                        h = int(xobj.get("/Height", 0))
                        filt = xobj.get("/Filter")
                        filt = str(filt) if filt is not None else ""
                        bpc = int(xobj.get("/BitsPerComponent", 0) or 0)
                        cs = xobj.get("/ColorSpace")
                        rep.images.append({
                            "width": w, "height": h, "filter": filt,
                            "bpc": bpc, "colorspace": str(cs)[:40] if cs else "",
                        })
                except Exception:
                    continue
        except Exception:
            pass
        # Fontlar
        fonts = res.get("/Font", {}) if res else {}
        try:
            for name, fnt in dict(fonts).items():
                try:
                    base = _clean(fnt.get("/BaseFont"))
                    subtype = _clean(fnt.get("/Subtype"))
                    embedded = False
                    fd = fnt.get("/FontDescriptor")
                    if fd is None and "/DescendantFonts" in fnt:
                        try:
                            fd = fnt["/DescendantFonts"][0].get("/FontDescriptor")
                        except Exception:
                            fd = None
                    if fd is not None:
                        embedded = any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                    key = base or str(name)
                    if key not in seen_fonts:
                        seen_fonts[key] = {
                            "basefont": base, "subtype": subtype,
                            "embedded": embedded,
                            "subset": bool(re.match(r"^[A-Z]{6}\+", base)),
                        }
                except Exception:
                    continue
        except Exception:
            pass
    rep.image_count = len(rep.images)
    rep.fonts = list(seen_fonts.values())


def classify_producer(producer: str, creator: str) -> dict:
    """Üretici/oluşturucu dizesini ALAN BAZLI sınıflandırır.

    Producer = PDF'i EN SON yazan yazılım; Creator = belgeyi ilk oluşturan uygulama.
    Yeniden-kayıt (oynama) sinyali için ÖNEMLİ olan SON YAZANDIR (producer). Örneğin
    Aspose.Words ürettiği PDF'te Creator'ı varsayılan "Microsoft Office Word" yazar —
    bu bir 'Word'de düzenleme' DEĞİLDİR; son yazan (producer) bir üretim kütüphanesidir."""
    p = (producer or "").lower()
    c = (creator or "").lower()
    combo = p + " || " + c

    def _hits(s, table):
        return [{"key": k, "desc": d} for k, d in table.items() if k in s]

    def _ghits(s):
        return [g for g in GENERATOR_PRODUCERS if g in s]

    prod_editor = _hits(p, EDITOR_PRODUCERS)
    prod_browser = _hits(p, BROWSER_PRODUCERS)
    prod_gen = _ghits(p)
    cre_editor = _hits(c, EDITOR_PRODUCERS)

    # geriye dönük uyum: editor_hits (birleşik) + kaynak-bilinçli bayraklar
    editor_all = prod_editor + [h for h in cre_editor if h not in prod_editor]
    result = {
        "editor_hits": editor_all,
        "generator_hits": list(dict.fromkeys(_ghits(p) + _ghits(c))),
        "browser_hits": prod_browser,
        "producer_is_editor": bool(prod_editor),      # SON YAZAN bir editör mü?
        "producer_is_generator": bool(prod_gen),      # SON YAZAN bir üretim kütüphanesi mi?
        "producer_is_browser": bool(prod_browser),    # SON YAZAN bir tarayıcı motoru mu?
        "producer_editor_hits": prod_editor,
        "append_mode": False,
    }
    if "appendmode" in combo.replace(" ", ""):
        result["append_mode"] = True
    return result

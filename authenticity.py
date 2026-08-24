"""
Dekont özgünlük denetimleri / Receipt authenticity checks.

Bir dekontu TEK BAŞINA (çapraz karşılaştırma olmadan) sahte olarak yakalayabilen iki güçlü,
banka-bilinçli sinyal üretir:

  1) BELGE/FİŞ NUMARASINDAKİ GÖMÜLÜ TARİH ↔ İŞLEM TARİHİ
     Türk bankalarında fiş/belge numarası çoğu zaman YYYYAAGG (üretim tarihi) ile başlar.
     Bu gömülü tarih, ekranda yazan işlem tarihinden farklıysa, belgenin tarihi sonradan
     değiştirilmiştir (ileri/geri tarihleme) — güçlü sahtecilik kanıtı.
       Örn. Enpara Fiş No "20260724..." (24 Tem) ama işlem tarihi 13.08.2026 -> SAHTE.

  2) ÜRETİCİ (PRODUCER) KÜTÜPHANESİ ↔ BANKANIN GERÇEK İMZASI
     Her banka dekontlarını belirli bir sunucu kütüphanesiyle üretir. Belge farklı bir
     üreticiyle (özellikle bir tarayıcı/editör ile) üretilmişse, bankanın sisteminden
     çıkmamış; düzenlenip yeniden dışa aktarılmış olabilir.
       Örn. Enpara gerçek dekontları iText ile üretilir; PDFium (tarayıcı) ile üretilmiş
       bir "Enpara dekontu" -> yeniden basılmış, olası SAHTE.
"""
from __future__ import annotations

import os
import re
import io
import datetime as _dt


# --- Banka görünen adı -> iç anahtar ---
_BANK_KEY = {
    "enpara.com (qnb)": "enpara",
    "qnb bank a.ş.": "qnb",
    "t.c. ziraat bankası": "ziraat",
    "yapı ve kredi bankası": "yapikredi",
    "türkiye iş bankası": "isbank",
    "vakıfbank": "vakif",
    "garanti bbva": "garanti",
    "akbank t.a.ş.": "akbank",
    "ing bank a.ş.": "ing",
    "fibabanka a.ş.": "fiba",
    "getirfinans (fibabanka)": "getir",
    "türk ekonomi bankası (teb)": "teb",
    "türkiye halk bankası": "halk",
    "ptt (pttbank)": "ptt",
    "kuveyt türk katılım": "kuveyt",
    "denizbank": "deniz",
    "ziraat dinamik banka": "ziraatdinamik",
}


def bank_key(bank_display: str) -> str:
    # Türkçe 'İ'.lower() birleşik noktalı 'i̇' (i + U+0307) üretir; bunu temizle ki
    # 'Türkiye İş Bankası' -> 'türkiye iş bankası' anahtarıyla eşleşsin.
    s = (bank_display or "").strip().lower().replace("̇", "")
    return _BANK_KEY.get(s, "")


# --- (2) Bankanın GERÇEK dekont üretim imzası (producer alt-dizeleri, küçük harf) ---
# Not: Bu liste doğrulanmış örneklere dayanır; bir banka birden çok geçerli kütüphane
# kullanabilir. Eşleşme YOKSA belge bankanın sisteminden çıkmamış demektir.
EXPECTED_PRODUCERS = {
    "enpara":    ["itext", "1t3xt"],          # Enpara -> iText (6 doğrulanmış örnek)
    "qnb":       ["itext", "1t3xt"],          # QNB -> iText (Enpara ile aynı altyapı)
    "ziraat":    ["skia", "chromium"],        # Ziraat mobil -> tarayıcı motoru (Skia)
    "yapikredi": ["aspose"],                  # Yapı Kredi -> Aspose.Words
    "akbank":    ["openpdf", "itext"],        # Akbank -> OpenPDF (iText türevi)
    "ing":       ["evopdf", "html to pdf"],   # ING -> EvoPdf HTML-to-PDF
    "isbank":    ["openpdf", "jasperreports", "itext"],  # İş Bankası -> JasperReports + OpenPDF
    "fiba":      ["aspose"],                   # Fibabanka -> Aspose.Words
    "getir":     ["aspose"],                   # GetirFinans (Fibabanka) -> Aspose.Words
    "garanti":   ["adobe experience manager", "skia"],  # Garanti: web=Adobe AEM, mobil=Skia
    "vakif":     ["skia", "chromium", "quartz"],  # VakıfBank: web=Skia/Chromium, mobil=iOS Quartz
    "halk":      ["ironpdf"],                  # Halkbank -> IronPdf (HTML-to-PDF, .NET)
}

# Belgeyi yeniden basmak/düzenlemek için kullanılan tipik TARAYICI/EDİTÖR üreticileri.
_RERENDER_PRODUCERS = ["pdfium", "skia", "chromium", "quartz", "cairo", "microsoft: print",
                       "acrobat", "ghostscript", "wkhtmltopdf", "print to pdf"]

# Bankanın gerçek üretiminde TARAYICI motoru kullanılıp kullanılmadığı. Ziraat mobil gerçekten
# Skia/Chromium ile üretir; bu yüzden onda tarayıcı imzası NORMALDİR. Diğerleri sunucu
# kütüphanesi kullanır -> onlarda tarayıcı imzası = yeniden basım = SAHTE.
_BANK_USES_BROWSER = {"ziraat"}


def check_producer(bkey: str, producer: str) -> dict | None:
    """Belgenin üreticisini bankanın gerçek imzasıyla karşılaştırır.
    Eşleşmezse bulgu döndürür. Banka tarayıcı KULLANMIYORSA ve belge bir tarayıcı/editörle
    üretilmişse -> KRİTİK (doğrudan güvenilmez)."""
    exp = EXPECTED_PRODUCERS.get(bkey)
    if not exp:
        return None                            # banka için beklenen üretici bilinmiyor
    pl = (producer or "").lower()
    if not pl:
        return None
    if any(e in pl for e in exp):
        return None                            # bankanın gerçek imzasıyla üretilmiş -> temiz
    rerender = any(x in pl for x in _RERENDER_PRODUCERS)
    exp_txt = " / ".join(exp)
    # Banka tarayıcı kullanmıyor + belge tarayıcı/editörle üretilmiş -> doğrudan güvenilmez
    if rerender and bkey not in _BANK_USES_BROWSER:
        return {
            "code": "BROWSER_RERENDER", "severity": "critical", "weight": 50, "rerender": True,
            "tr": f"TARAYICIYLA YENİDEN ÜRETİM: Bu bankanın (‘{bkey}’) gerçek dekontları sunucu "
                  f"kütüphanesiyle (‘{exp_txt}’) üretilir; ancak bu belge bir TARAYICI/EDİTÖR "
                  f"(‘{producer}’) ile üretilmiş. Bankanın sisteminden çıkmamış; düzenlenip yeniden "
                  f"PDF'e basılmış — GÜVENİLMEZ / olası SAHTE.",
            "en": f"BROWSER RE-RENDER: genuine receipts of '{bkey}' are produced by a server library "
                  f"('{exp_txt}'), but this file was produced by a browser/editor ('{producer}'). "
                  f"It did not come from the bank's system — untrusted / possible forgery.",
            "detail": f"producer={producer} expected={exp}",
        }
    return {
        "code": "PRODUCER_MISMATCH", "severity": "high", "weight": 30, "rerender": rerender,
        "tr": f"ÜRETİCİ UYUŞMAZLIĞI: Bu bankanın (‘{bkey}’) gerçek dekontları ‘{exp_txt}’ ile üretilir; "
              f"ancak bu belgenin üreticisi ‘{producer}’. Belge bankanın sisteminden çıkmamış; "
              f"büyük olasılıkla düzenlenip yeniden dışa aktarılmış (olası SAHTE).",
        "en": f"PRODUCER MISMATCH: genuine receipts of this bank ('{bkey}') are produced with '{exp_txt}', "
              f"but this file's producer is '{producer}'. The document did not come from the bank's system; "
              f"likely edited and re-exported (possible forgery).",
        "detail": f"producer={producer} expected={exp}",
    }


def check_pdfium(producer: str) -> dict | None:
    """GLOBAL kural (banka bağımsız): Üretici PDFium ise dekont SAHTE kabul edilir.

    PDFium, Chrome/Chromium'un PDF GÖRÜNTÜLEME ve YENİDEN-BASMA motorudur; mevcut bir PDF'i
    açıp yeniden kaydetmek/basmak için kullanılır. Hiçbir banka orijinal dekontunu PDFium ile
    ÜRETMEZ (HTML'den üreten bankalar 'Skia/PDF' kullanır — PDFium değil). Bu yüzden üreticisi
    PDFium olan bir "banka dekontu", mutlaka açılıp yeniden kaydedilmiş/düzenlenmiştir."""
    if "pdfium" not in (producer or "").lower():
        return None
    return {
        "code": "PDFIUM_PRODUCED", "severity": "critical", "weight": 55,
        "tr": f"SAHTE (PDFium ile üretilmiş): Belgenin üreticisi ‘{producer}’. PDFium, Chrome'un PDF "
              f"görüntüleme/yeniden-basma motorudur; hiçbir banka orijinal dekontu bununla ÜRETMEZ. "
              f"Bu, mevcut bir PDF'in açılıp yeniden kaydedildiğini/düzenlendiğini gösterir — belge SAHTEDİR.",
        "en": f"FORGERY (produced by PDFium): the producer is '{producer}'. PDFium is Chrome's PDF "
              f"viewer/re-print engine; no bank generates an original receipt with it. This means an "
              f"existing PDF was reopened and re-saved/edited — the document is a forgery.",
        "detail": f"producer={producer}",
    }


# --- (A) Font alt-küme parmak izi ---------------------------------------------------------
# Her banka dekontlarını belirli bir font kümesiyle ve belirli bir ALT-KÜME ÖNEK stiliyle
# üretir. iText/Aspose/OpenPDF gibi kütüphaneler RASTGELE 6-harfli önek (ör. INZCGU+) kullanır;
# Chrome/Skia (tarayıcı) ise SIRALI önek (AAAAAA+, BAAAAA+, ...) kullanır. Bir bankanın gerçek
# stili 'random' iken belge 'seq' (Skia) ise -> tarayıcıyla yeniden basılmış = SAHTE.
BANK_FONT_PROFILE = {
    "enpara":    {"style": "random", "require": {"arialmt"}},   # iText: ArialMT + Tahoma(+Bold)
    "qnb":       {"style": "random", "require": {"arialmt"}},   # QNB: iText, ArialMT
    "ziraat":    {"style": "seq"},                              # Skia (sıralı önek NORMAL)
    "yapikredi": {"style": "random"},                           # Aspose: Calibri + CourierNew
    "akbank":    {"style": "random"},                           # OpenPDF: ArialNarrow
    "ing":       {"style": "none"},                             # gömülü font yok
    "isbank":    {"style": "random", "require": {"arialmt"}},   # OpenPDF/JasperReports: ArialMT + Consolas
    "fiba":      {"style": "random"},                           # Aspose: FreeSans
    "getir":     {"style": "random"},                           # GetirFinans (Fibabanka): Aspose
    "halk":      {"style": "seq"},                              # IronPdf (Chromium): DejaVu, sıralı önek
}

_SEQ_PREFIXES = {"AAAAAA", "BAAAAA", "CAAAAA", "DAAAAA", "EAAAAA", "FAAAAA", "GAAAAA", "HAAAAA"}


def _font_signature(pdf_bytes: bytes):
    """PDF'ten (küçük-harf temel font adları kümesi, önek stili) döndürür.
    Stil: 'seq' (Skia/tarayıcı sıralı önek), 'random' (kütüphane rastgele önek), 'none'."""
    try:
        import pikepdf
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except Exception:
        return set(), "unknown"
    raw = set()
    try:
        for pg in pdf.pages:
            res = pg.get("/Resources", {})
            fo = res.get("/Font", {}) if res else {}
            for _, f in dict(fo).items():
                try:
                    bf = f.get("/BaseFont")
                    if bf:
                        raw.add(str(bf))
                    for d in (f.get("/DescendantFonts") or []):
                        b2 = d.get("/BaseFont")
                        if b2:
                            raw.add(str(b2))
                except Exception:
                    continue
    except Exception:
        pass
    base = set()
    prefixes = []
    for f in raw:
        s = f.lstrip("/")
        m = re.match(r"^([A-Z]{6})\+(.+)$", s)
        if m:
            prefixes.append(m.group(1))
            base.add(m.group(2).lower())
        else:
            base.add(s.lower())
    if prefixes:
        style = "seq" if all(p in _SEQ_PREFIXES for p in prefixes) else "random"
    else:
        style = "none"
    return base, style


def check_fonts(bkey: str, pdf_bytes: bytes) -> dict | None:
    """Belgenin font parmak izini bankanın gerçek imzasıyla karşılaştırır."""
    prof = BANK_FONT_PROFILE.get(bkey)
    if not prof or not pdf_bytes:
        return None
    base, style = _font_signature(pdf_bytes)
    if style == "unknown":
        return None
    exp_style = prof.get("style")
    # (1) Sunucu-kütüphanesi bankası (random) ama belge tarayıcı stili (seq) -> yeniden basım
    if exp_style == "random" and style == "seq":
        return {
            "code": "FONT_BROWSER_RERENDER", "severity": "critical", "weight": 50,
            "tr": f"FONT PARMAK İZİ (tarayıcı yeniden basımı): Bu bankanın gerçek dekontlarında fontlar "
                  f"kütüphane tarafından RASTGELE öneklerle gömülür; ancak bu belgede fontlar tarayıcı "
                  f"(Chrome/Skia) imzası olan SIRALI öneklerle (AAAAAA+, BAAAAA+ …) gömülmüş. Belge "
                  f"tarayıcıyla yeniden basılmış — üretici bilgisi taklit edilse bile bu font imzası "
                  f"bankanın sisteminden çıkmadığını gösterir. GÜVENİLMEZ / olası SAHTE.",
            "en": f"FONT FINGERPRINT (browser re-render): genuine receipts of this bank embed fonts with "
                  f"RANDOM subset prefixes; but this file uses SEQUENTIAL browser (Chrome/Skia) prefixes "
                  f"(AAAAAA+, BAAAAA+ …). The document was re-rendered by a browser — untrusted/forgery.",
            "detail": f"style={style} fonts={sorted(base)}",
        }
    # (2) Zorunlu fontlar eksik (ör. Enpara ArialMT)
    req = prof.get("require")
    if req and not (req & base) and base:
        return {
            "code": "FONT_SET_MISMATCH", "severity": "high", "weight": 28,
            "tr": f"FONT KÜMESİ UYUŞMAZLIĞI: Bu bankanın gerçek dekontlarında bulunması beklenen font(lar) "
                  f"({', '.join(sorted(req))}) belgede yok (mevcut: {', '.join(sorted(base)) or '—'}). "
                  f"Belge bankanın orijinal şablonuyla üretilmemiş olabilir (olası SAHTE).",
            "en": f"FONT SET MISMATCH: expected font(s) ({', '.join(sorted(req))}) are missing "
                  f"(present: {', '.join(sorted(base)) or '—'}). The document may not have been produced "
                  f"with the bank's original template (possible forgery).",
            "detail": f"style={style} fonts={sorted(base)} require={sorted(req)}",
        }
    return None


# --- (C) Belge içi + XMP çapraz-tarih tutarlılığı -----------------------------------------
_FULLDATE_RE = re.compile(r"\b(\d{2})[./](\d{2})[./](\d{4})\b")


def _dates_in(text: str):
    out = set()
    for d, mo, y in _FULLDATE_RE.findall(text or ""):
        try:
            out.add(_dt.date(int(y), int(mo), int(d)))
        except ValueError:
            continue
    return out


def xmp_dates(pdf_bytes: bytes):
    """XMP/gömülü metadata içindeki tam tarihleri döndürür."""
    try:
        import pikepdf
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        if "/Metadata" in pdf.Root:
            raw = bytes(pdf.Root.Metadata.read_bytes()).decode("latin1", "ignore")
            # XMP tarihleri genelde ISO (YYYY-MM-DD)
            out = set()
            for y, mo, d in re.findall(r"(\d{4})-(\d{2})-(\d{2})", raw):
                try:
                    out.add(_dt.date(int(y), int(mo), int(d)))
                except ValueError:
                    continue
            return out
    except Exception:
        pass
    return set()


def check_internal_dates(text: str, pdf_bytes: bytes, txn_dt, value_date_str: str = "",
                         use_meta: bool = True) -> dict | None:
    """Belge üzerindeki tüm tarihleri (ve güvenilirse XMP tarihlerini) işlem tarihiyle karşılaştırır.
    İşlem tarihinden > 3 gün sapan (valör hariç) bir iç tarih varsa tahrifat işaretidir.
    use_meta=False: üretici bir şablon/rapor üreticisidir (Aspose vb.) -> XMP tarihleri şablon
    artefaktıdır, karşılaştırmaya KATILMAZ (yanlış pozitifi önler)."""
    if txn_dt is None:
        return None
    txn_date = txn_dt.date() if hasattr(txn_dt, "date") else txn_dt
    cand = _dates_in(text)
    if use_meta:
        cand = cand | xmp_dates(pdf_bytes)
    # valör/değer tarihini hariç tut (meşru olarak birkaç gün farklı olabilir)
    vd = _dates_in(value_date_str)
    worst = None
    for d in cand:
        if d == txn_date or d in vd:
            continue
        gap = abs((d - txn_date).days)
        if gap > 3 and (worst is None or gap > worst[1]):
            worst = (d, gap)
    if worst:
        d, gap = worst
        return {
            "code": "INTERNAL_DATE_MISMATCH", "severity": "high", "weight": 30,
            "tr": f"BELGE İÇİ TARİH ÇELİŞKİSİ: İşlem tarihi {txn_date.strftime('%d.%m.%Y')} iken belgede/"
                  f"metadata'da {d.strftime('%d.%m.%Y')} tarihi de geçiyor ({gap} gün fark). Gerçek bir "
                  f"dekontta tüm tarihler (işlem, basım, alt-zaman damgası, XMP) aynı gün olmalıdır; "
                  f"uyumsuz tarih, belgenin tarihiyle oynandığını gösterir (olası SAHTE).",
            "en": f"INTERNAL DATE MISMATCH: transaction date is {txn_date.isoformat()} but the document/"
                  f"metadata also contains {d.isoformat()} ({gap} days apart). In a genuine receipt all "
                  f"dates should be the same day; an inconsistent date indicates date tampering.",
            "detail": f"txn={txn_date.isoformat()} other={d.isoformat()} gap={gap}",
        }
    return None


# --- (1) Fiş/belge numarasındaki gömülü tarih ---
# Hangi alanın tarih taşıdığı banka bazlı ayarlanabilir; yoksa genel sezgi uygulanır.
RECEIPT_DATE_SOURCE = {
    "enpara": ["document_no"],                 # Fiş No: YYYYAAGG + sayaç
    "qnb": ["document_no"],                    # QNB Fiş No: aynı YYYYAAGG + sayaç yapısı
}


def _embedded_date(num: str):
    """Numaranın BAŞINDAKİ 8 haneden geçerli bir tarih (YYYYAAGG) çıkarır; yoksa None."""
    d = re.sub(r"\D", "", num or "")
    if len(d) < 8:
        return None
    try:
        y, mo, da = int(d[:4]), int(d[4:6]), int(d[6:8])
        if 2015 <= y <= 2035 and 1 <= mo <= 12 and 1 <= da <= 31:
            return _dt.date(y, mo, da)
    except ValueError:
        pass
    return None


def check_receipt_number_date(bkey: str, document_no: str, ref_no: str,
                              seq_number: str, txn_dt) -> dict | None:
    """Belge/fiş numarasındaki gömülü tarihi işlem tarihiyle karşılaştırır.
    txn_dt: datetime | None. Fark > 2 gün ise sahtecilik bulgusu döndürür."""
    if txn_dt is None:
        return None
    txn_date = txn_dt.date() if hasattr(txn_dt, "date") else txn_dt
    # SADECE gömülü tarih taşıdığını DOĞRULADIĞIMIZ bankalarda çalış. Genel geri-dönüş
    # (her banka numarasını YYYYAAGG sanmak) yanlış-pozitif üretir: ör. VakıfBank 'İŞLEM NO'
    # 2026010724333986 tarih değildir; '20260107' olarak çözülüp işlem tarihiyle çelişir.
    srcs = RECEIPT_DATE_SOURCE.get(bkey)
    if not srcs:
        return None
    field_vals = {"document_no": document_no, "ref_no": ref_no, "seq_number": seq_number}
    cands = [(s, field_vals.get(s, "")) for s in srcs]
    for fname, num in cands:
        ed = _embedded_date(num)
        if not ed:
            continue
        gap = abs((ed - txn_date).days)
        if gap <= 2:
            return None                        # gömülü tarih ile işlem tarihi uyuşuyor -> temiz
        return {
            "code": "RECEIPT_NO_DATE_MISMATCH", "severity": "critical", "weight": 45,
            "tr": f"BELGE TARİHİ ÇELİŞKİSİ: Fiş/belge numarası ({num}) {ed.strftime('%d.%m.%Y')} tarihini "
                  f"kodluyor, ancak dekont üzerindeki işlem tarihi {txn_date.strftime('%d.%m.%Y')}. "
                  f"Aradaki fark {gap} gün. Fiş numarası bankanın verdiği ve DEĞİŞTİRİLEMEYEN bir "
                  f"tarih taşır; belge üzerindeki işlem tarihi bununla uyuşmuyorsa, tarih sonradan "
                  f"değiştirilmiştir (ileri/geri tarihleme) — güçlü SAHTECİLİK kanıtı.",
            "en": f"DOCUMENT DATE MISMATCH: the receipt/document number ({num}) encodes {ed.isoformat()}, "
                  f"but the transaction date shown is {txn_date.isoformat()} ({gap} days apart). The receipt "
                  f"number carries the bank's immutable date; a mismatch means the visible date was altered "
                  f"(back/forward-dating) — strong forgery evidence.",
            "detail": f"embedded={ed.isoformat()} txn={txn_date.isoformat()} field={fname} num={num}",
        }
    return None


# Belge/fiş numarasının YAPISI: baş 8 hane = iş günü (YYYYAAGG), kalan hane = artan
# global işlem sayacı (zamanla monoton artar; saatten türetilmez). Görüntüleme/denetim için.
RECEIPT_NO_STRUCTURE = {
    "enpara": {"date_len": 8, "total_len": 15},   # 20260812 + 7 haneli sayaç = 15
    "qnb": {"date_len": 8, "total_len": 15},      # QNB Fiş No: aynı yapı
}


def document_no_parts(bkey: str, document_no: str) -> dict | None:
    """Belge/doküman numarasını 'tarih kısmı' + 'sayaç' olarak ayrıştırır (banka kuralı varsa).
    Döner: {date_part, date_fmt, date_ok, counter, length, length_ok} ya da None (kural yoksa/rakam yoksa)."""
    spec = RECEIPT_NO_STRUCTURE.get(bkey)
    if not spec:
        return None
    digits = re.sub(r"\D", "", document_no or "")
    if not digits:
        return None
    dl = spec["date_len"]
    date_part = digits[:dl]
    counter = digits[dl:]
    ed = _embedded_date(date_part)
    return {
        "date_part": date_part,
        "date_fmt": ed.strftime("%d.%m.%Y") if ed else "",
        "date_ok": bool(ed),
        "counter": counter,
        "length": len(digits),
        "length_ok": len(digits) == spec["total_len"],
    }


# Bankaların gerçek PDF derleyicisi için insan-okur etiketler (gösterim amaçlı).
PRODUCER_LABEL = {
    "enpara":    "iText",
    "qnb":       "iText",
    "ziraat":    "Skia/PDF (Chromium tarayıcı motoru)",
    "yapikredi": "Aspose.Words",
    "akbank":    "OpenPDF",
    "ing":       "EvoPdf (HTML-to-PDF)",
    "isbank":    "OpenPDF / JasperReports",
    "fiba":      "Aspose.Words",
    "getir":     "Aspose.Words",
    "garanti":   "Adobe AEM (web) / Skia (mobil)",
    "vakif":     "Skia/Chromium (web) / iOS Quartz (mobil)",
    "halk":      "IronPdf",
}


def producer_assessment(bkey: str, producer: str, creator: str = "", resaved: bool = False) -> dict:
    """Yüklenen PDF'in ÜRETİCİSİ ile bankanın BEKLENEN derleyicisini karşılaştırır ve karar üretir.
    Döner: {actual, expected_label, expected_list, bank_known, status, tr, en}.

    status: match | resave | mismatch | no_producer | unknown_bank
    'resave': PDF, bankanın derleyicisinden çıkmış gibi gelmiyor; başka bir programda (tarayıcı/
    editör/Quartz vb.) açılıp yeniden kaydedilmiş. 'mismatch': bambaşka bir üretim kütüphanesi."""
    prod = (producer or "").strip()
    exp = EXPECTED_PRODUCERS.get(bkey)
    exp_label = PRODUCER_LABEL.get(bkey) or (", ".join(exp) if exp else "")
    out = {"actual": prod or "—", "expected_label": exp_label, "expected_list": exp or [],
           "bank_known": bool(exp), "status": "", "tr": "", "en": ""}
    if not exp:
        out["status"] = "unknown_bank"
        out["tr"] = "Bu banka için beklenen PDF derleyicisi henüz tanımlı değil; karşılaştırma yapılamadı."
        out["en"] = "No expected PDF compiler is defined for this bank yet; no comparison made."
        return out
    if not prod:
        out["status"] = "no_producer"
        out["tr"] = (f"PDF'te üretici bilgisi yok. {exp_label} ile üretilen orijinal banka dekontunda "
                     f"üretici bilgisi bulunur; eksik olması şüphelidir.")
        out["en"] = "No producer metadata; a genuine bank output carries a producer. Its absence is suspicious."
        return out
    pl = prod.lower()
    # ÜRETİCİ (son yazan) bankanın kütüphanesiyle eşleşiyorsa uyumludur. Creator alanı
    # ('Microsoft Office Word' gibi) DEĞERLENDİRİLMEZ — bazı üretim kütüphaneleri (Aspose)
    # Creator'ı Word yazar; bu bir 'Word'de düzenleme' değildir.
    matched = any(x in pl for x in exp)
    rerender = any(x in pl for x in _RERENDER_PRODUCERS) and bkey not in _BANK_USES_BROWSER
    if matched:
        out["status"] = "match"
        out["tr"] = f"Uyumlu: PDF, {exp_label} ile üretilmiş — bu bankanın kullandığı derleyici."
        out["en"] = f"Match: produced by {exp_label} — the compiler this bank uses."
    elif resaved or rerender:
        out["status"] = "resave"
        out["tr"] = (f"UYUMSUZ: Bu PDF, bankanın derleyicisinden ({exp_label}) çıkmış gibi GELMİYOR; "
                     f"‘{prod}’ ile açılıp yeniden kaydedilmiş. Orijinal dekont başka bir programda "
                     f"düzenlenmiş/yeniden basılmış olabilir — sahtecilik şüphesi.")
        out["en"] = (f"MISMATCH: this PDF does NOT come as delivered from the bank's compiler ({exp_label}); "
                     f"it was re-saved with '{prod}'. The original may have been edited/re-printed elsewhere.")
    else:
        out["status"] = "mismatch"
        out["tr"] = (f"UYUMSUZ: Bu banka {exp_label} kullanır; bu PDF ise ‘{prod}’ ile üretilmiş. "
                     f"Farklı bir derleyici — bankanın orijinal çıktısı değil, sahtecilik şüphesi.")
        out["en"] = (f"MISMATCH: this bank uses {exp_label}; this PDF was produced by '{prod}'. "
                     f"Different compiler — not the bank's original output.")
    return out


# --- (D) Deterministik IBAN / banka-tutarlılığı kontrolleri -------------------------------
# Bunlar olasılık değil KESİN kurallardır; elimizdeki tüm gerçek dekontlarda doğrulanmıştır
# (sıfır yanlış-pozitif). Yalnızca gerekli veri MEVCUTKEN çalışır.

# İhracçı banka anahtarı -> o bankaya ait geçerli IBAN kodları (hesap sahibinin IBAN'ı bunlardan
# biriyle başlamalıdır). Enpara/QNB tarihsel olarak 00111 (Finansbank) idi; Enpara artık 00157.
_ISSUER_IBAN_CODES = {
    "ziraat": {"00010", "00160", "00209"},
    "halk":   {"00012"},
    "vakif":  {"00015", "00210"},
    "akbank": {"00046"},
    "garanti": {"00062"},
    "isbank": {"00064"},
    "yapikredi": {"00067"},
    "ing":    {"00099"},
    "fiba":   {"00103"},
    "getir":  {"00103"},                       # GetirFinans = Fibabanka altyapısı (IBAN kodu 00103)
    "teb":    {"00032"},                        # Türk Ekonomi Bankası (TEB)
    "qnb":    {"00111"},
    "enpara": {"00157", "00111"},
    "ptt":    {"00807"},
    "kuveyt": {"00205"},
    "deniz":  {"00134"},
    "ziraatdinamik": {"00160"},
}

# TEK KAYNAK (single source of truth): banka kimliği extract.BANK_REGISTRY'de tanımlıdır;
# banka-anahtarı (_BANK_KEY) ve IBAN-kodları (_ISSUER_IBAN_CODES) ondan TÜRETİLİR. Böylece yeni
# banka eklerken YALNIZ registry'yi güncellemek yeterlidir (burada elle güncelleme gerekmez).
# Import başarısız olursa yukarıdaki gömülü tablolar (yedek) kullanılır — davranış bozulmaz.
try:
    import extract as _ext_reg
    if getattr(_ext_reg, "ISSUER_IBAN_CODES", None):
        _ISSUER_IBAN_CODES = {k: set(v) for k, v in _ext_reg.ISSUER_IBAN_CODES.items()}
    if getattr(_ext_reg, "BANK_LABEL_TO_KEY", None):
        _BANK_KEY = dict(_ext_reg.BANK_LABEL_TO_KEY)
except Exception:
    pass


def _canon_bank(text: str) -> str:
    """Serbest metin banka adını kanonik ada indirger (banks.NAME_KEYWORDS ile).
    'Yapı VE Kredi', 'A.Ş.' gibi ekler eşleşmeyi bozmasın diye sadeleştirilir."""
    import banks as _b
    t = (text or "").lower().replace("̇", "")
    t = re.sub(r"\bve\b", " ", t)                  # 'yapı ve kredi' -> 'yapı  kredi'
    t = re.sub(r"\s+", " ", t)
    if not t.strip():
        return ""
    for pat, name in _b.NAME_KEYWORDS:
        if re.search(pat, t):
            return name
    return ""


def deterministic_checks(bkey: str, sender_iban: str, receiver_iban: str,
                         receiver_bank_text: str, all_ibans: list | None = None) -> list[dict]:
    """IBAN geçerliliği + ihracçı-taraf + alıcı-bankası tutarlılığı. Bulgu listesi döndürür.
    `all_ibans`: belgedeki TÜM IBAN'lar (OCR-temelli). Vision/OCR taraf-eşlemesi yanlış olsa bile
    ihraççı kontrolü belgedeki gerçek IBAN'lara göre yapılır → yanlış-pozitif önlenir."""
    import banks as _b
    out = []
    s_iban = _b.normalize_iban(sender_iban)
    r_iban = _b.normalize_iban(receiver_iban)
    _doc_codes = {_b.iban_bank_code(_b.normalize_iban(ib)) for ib in (all_ibans or []) if ib}
    _doc_codes.discard("")

    # (1) IBAN mod-97 geçerliliği: geçersiz IBAN = tahrifat/uydurma (dijital dekontta yazım hatası olmaz)
    for label, ib in (("gönderen", s_iban), ("alıcı", r_iban)):
        v = _b.iban_valid(ib)
        if v is False:
            out.append({
                "code": "IBAN_INVALID", "severity": "high", "weight": 32,
                "tr": f"GEÇERSİZ IBAN ({label}): ‘{ib}’ IBAN kontrol basamağı (mod-97) tutmuyor. "
                      f"Bankaların ürettiği dekontlarda IBAN her zaman geçerlidir; geçersiz IBAN, "
                      f"numaranın elle değiştirildiğini/uydurulduğunu gösterir.",
                "en": f"INVALID IBAN ({label}): '{ib}' fails the IBAN check digit (mod-97). Genuine bank "
                      f"receipts always carry valid IBANs; an invalid one indicates it was altered/fabricated.",
                "detail": f"{label}_iban={ib}",
            })

    # (A) İhracçı-taraf tutarlılığı: yalnızca HER İKİ IBAN da varken çalışır (hesap sahibi tarafı
    #     eksikse yanlış-pozitif olmasın). İhracçının kodu iki taraftan hiçbirinde yoksa -> tahrifat.
    codes = _ISSUER_IBAN_CODES.get(bkey)
    # SAVUNMA: iki taraf IBAN'ı BİREBİR AYNI ise gerçekte tek IBAN okunup kopyalanmıştır
    # (fotoğrafta gönderen IBAN'ı yakalanamayınca alıcınınki iki tarafa da yazılabilir).
    # Böyle bir durumda ihraççının müşterisi, OKUNAMAYAN taraf olabilir → uyuşmazlık iddia
    # edilemez, denetim atlanır (yanlış-pozitif önlenir).
    if codes and s_iban and r_iban and s_iban != r_iban:
        sc, rc = _b.iban_bank_code(s_iban), _b.iban_bank_code(r_iban)
        # İhraççının kodu, taraf IBAN'larında YA DA belgedeki herhangi bir IBAN'da varsa sorun yok.
        # (Vision/OCR sender↔receiver'ı yanlış eşlese bile ihraççı müşterisi belgede mevcuttur.)
        _present = (codes & {sc, rc}) or (codes & _doc_codes)
        if sc and rc and not _present:
            exp = "/".join(sorted(codes))
            out.append({
                "code": "ISSUER_IBAN_MISMATCH", "severity": "critical", "weight": 46,
                "tr": f"İHRAÇÇI-TARAF UYUŞMAZLIĞI: Bu bir {bkey.upper()} dekontu ve hesap sahibi bu bankada "
                      f"olmalı (IBAN kodu {exp}); oysa ne gönderen ({sc}) ne alıcı ({rc}) IBAN'ı bu bankaya ait. "
                      f"Gerçek bir dekontta taraflardan biri mutlaka ihraç eden bankanın müşterisidir — bu belge "
                      f"büyük olasılıkla başka bir bankanın şablonundan/dekontundan üretilmiş, SAHTE.",
                "en": f"ISSUER-PARTY MISMATCH: this is a {bkey} receipt so the account holder must bank there "
                      f"(IBAN code {exp}), yet neither sender ({sc}) nor receiver ({rc}) IBAN belongs to it. "
                      f"In a genuine receipt one party is always the issuing bank's customer — likely forged.",
                "detail": f"issuer={bkey} expected={exp} sender={sc} receiver={rc}",
            })

    # (B) Alıcı bankası ↔ alıcı IBAN kodu: dekontta YAZAN alıcı bankası ile IBAN'ın bankası çelişiyorsa.
    #     Yalnızca ikisi de BİLİNEN ve FARKLI bankaya çözülürse tetiklenir (ihtiyatlı).
    if r_iban and receiver_bank_text:
        iban_bank = _b.bank_from_iban(r_iban)               # IBAN kodundan resmi ad
        stated = _canon_bank(receiver_bank_text)
        iban_canon = _canon_bank(iban_bank)
        if iban_canon and stated and iban_canon != stated:
            out.append({
                "code": "RECEIVER_BANK_MISMATCH", "severity": "high", "weight": 36,
                "tr": f"ALICI BANKASI ÇELİŞKİSİ: Dekontta alıcı bankası ‘{receiver_bank_text}’ yazıyor, ancak "
                      f"alıcı IBAN'ının banka kodu {iban_bank}’a ait. İsim ile IBAN farklı bankaları gösteriyor "
                      f"— alıcı adı/bankası ya da IBAN sonradan değiştirilmiş olabilir (olası SAHTE).",
                "en": f"RECEIVER BANK CONTRADICTION: the stated receiver bank is '{receiver_bank_text}', but the "
                      f"receiver IBAN's bank code belongs to {iban_bank}. Name and IBAN point to different banks "
                      f"— the receiver name/bank or IBAN may have been altered (possible forgery).",
                "detail": f"stated={stated} iban_bank={iban_canon}",
            })
    return out


# --- (E) Alan-bazlı font tutarlılığı: TUTAR yabancı bir fontta mı? -----------------------
# Gerçek dekontlarda tutar, belgenin ANA font ailesiyle yazılır (yoğun kullanım). Bir düzenleme
# aracıyla tutar değiştirildiğinde, orijinal gömülü alt-küme font tam mevcut olmadığından
# düzenlenen rakamlar FARKLI/yabancı bir fontta çıkar. Bu yüzden tutarın font AİLESİ belgede
# başka yerde neredeyse hiç kullanılmıyorsa -> yapıştırılmış/oynanmış tutar şüphesi.
def _font_family(fontname: str) -> str:
    n = re.sub(r"^[A-Z]{6}\+", "", fontname or "").lower()      # alt-küme öneki (ABCDEF+) at
    n = re.sub(r"[-,]?(bold|italic|oblique|regular|mt|ps|psmt)\b", "", n)
    return re.sub(r"[^a-z]", "", n)


def check_amount_font(pdf_bytes: bytes, amount_value) -> dict | None:
    if not pdf_bytes or amount_value is None:
        return None
    try:
        import pdfplumber, io as _io
        with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
            chars = pdf.pages[0].chars
    except Exception:
        return None
    if not chars:
        return None
    # belgedeki font AİLESİ kullanım sayıları
    fam_use = {}
    for c in chars:
        fam_use[_font_family(c.get("fontname", ""))] = fam_use.get(_font_family(c.get("fontname", "")), 0) + 1
    intpart = str(int(abs(amount_value)))
    if len(intpart) < 3:                         # 3 haneden kısa tutarlarda çok az rakam -> güvenilmez
        return None
    # sayısal jetonları grupla (rakam + ., ,) ve tutar tamsayısıyla eşleşenleri bul
    occ_fams = []
    cur = []
    for c in chars + [{"text": " ", "fontname": ""}]:
        t = c.get("text", "")
        if t.isdigit() or t in ".,":
            cur.append(c)
        else:
            if cur:
                digits = "".join(x["text"] for x in cur if x["text"].isdigit())
                if digits.startswith(intpart) or intpart in digits:
                    fams = {_font_family(x["fontname"]) for x in cur if x["text"].isdigit()}
                    occ_fams.append(fams)
            cur = []
    if not occ_fams:
        return None
    # TÜM tutar geçişleri yabancı fonttaysa (ana metinde neredeyse hiç kullanılmayan aile) -> şüphe
    MAIN = max(fam_use.values()) if fam_use else 0
    def is_foreign(fam):
        # aile kullanımı hem mutlak düşük (<25) hem de ana fonta göre çok küçük (<%2)
        u = fam_use.get(fam, 0)
        return u < 25 and (MAIN == 0 or u < 0.02 * MAIN)
    all_foreign = all(all(is_foreign(f) for f in fams) for fams in occ_fams if fams)
    if all_foreign and occ_fams:
        foreign = sorted({f for fams in occ_fams for f in fams})
        return {
            "code": "AMOUNT_FONT_ANOMALY", "severity": "high", "weight": 28,
            "tr": f"TUTAR FONT ANOMALİSİ: İşlem tutarı, belgenin ana fontundan farklı ve belgede başka "
                  f"yerde kullanılmayan bir fontla ({', '.join(foreign)}) yazılmış. Gerçek dekontlarda tutar, "
                  f"belgenin ana fontuyla basılır; farklı/yabancı font, tutarın sonradan düzenlenip "
                  f"yapıştırıldığına işaret eder (olası SAHTE).",
            "en": f"AMOUNT FONT ANOMALY: the amount is rendered in a font ({', '.join(foreign)}) that differs "
                  f"from the document's main font and is used nowhere else. Genuine receipts print the amount "
                  f"in the body font; a foreign font suggests the amount was edited/pasted (possible forgery).",
            "detail": f"foreign_fonts={foreign}",
        }
    return None


def check_masked_name(receiver_name: str, receiver_iban: str, text: str) -> dict | None:
    """Alıcı adı banka tarafından yıldızla maskelenmişse (ör. 'BA***** AŞ*****): IBAN geçerli ve
    açık baş harfler mevcutsa bu EKSİK/şüpheli bilgi DEĞİLDİR, bankanın standart gizleme biçimidir.
    Bilgilendirici (nötr) bir not döndürür."""
    import banks as _b
    nm = (receiver_name or "").strip()
    if "*" not in nm:
        return None
    visible = re.sub(r"\*+", "", nm).strip()
    if not visible:
        return None                              # tamamen maskeli, açık harf yok -> not verme
    if _b.iban_valid(receiver_iban) is False:
        return None                              # IBAN geçersizse zaten IBAN_INVALID tetiklenir
    return {
        "code": "MASKED_RECEIVER_NAME", "severity": "info", "weight": 0,
        "tr": f"Alıcı adı banka tarafından gizlilik gereği maskelenmiştir ({nm}). Alıcı IBAN geçerli ve "
              f"adın açık baş harfleri mevcut — bu EKSİK BİLGİ DEĞİLDİR, bankanın standart maskelemesidir.",
        "en": f"The receiver name is masked by the bank for privacy ({nm}). The receiver IBAN is valid and the "
              f"visible initials are present — this is NOT missing data, just the bank's standard masking.",
        "detail": f"visible={visible}",
    }


# ---------------------------------------------------------------------------
#  İŞLEM TÜRÜ (RAIL) ↔ ÜCRET TARİFESİ TUTARLILIĞI
#  Bir dekont bir işlem türünden (ör. HAVALE) üretilip tutarı/işlem türü FAST'e
#  çevrildiğinde ÜCRET çoğu zaman güncellenmez: FAST etiketli ama ücret HAVALE
#  tarifesinde kalır. Ücret, işlem türüne göre bankanın bilinen tarifesiyle
#  çapraz kontrol edilir. Referanslar DOĞRULANMIŞ gerçek dekontlardan seed edilir
#  (zamanla store'daki gerçek dekontlardan öğrenilebilir). Yalnızca ücret AÇIKÇA
#  BAŞKA bir rail'in tarifesine uyduğunda tetiklenir → tarife değişse bile 0 FP.
# ---------------------------------------------------------------------------
_RAIL_FEE_REF = {
    # bank_key: { rail: [gerçek dekontlarda gözlenen ücret+vergi değerleri] }
    "isbank": {"fast": [16.76], "havale": [8.38]},
}
_RAIL_LABEL = {"fast": "FAST", "havale": "HAVALE", "eft": "EFT"}


def _tr_low(s: str) -> str:
    s = (s or "").lower().replace("̇", "")
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"), ("ö", "o"), ("ç", "c")):
        s = s.replace(a, b)
    return s


def detect_transfer_rail(text: str) -> str | None:
    """İşlem kanalını (rail) metinden çıkarır. Senaryo/Dekont Tipi'ne DEĞİL, işlem
    başlığı ve ÜCRET etiketine bakar (İş Bankası FAST'i 'DEKONT/EFT' tipiyle basar)."""
    n = _tr_low(text)
    if "fast ucreti" in n or "giden fast" in n or "gelen fast" in n or "fast islemi" in n:
        return "fast"
    if "havale ucreti" in n or "dekont/hvl" in n or "hesaptan hesaba havale" in n or "havale+vergi" in n:
        return "havale"
    if "eft ucreti" in n or "dekont/eft" in n and "fast" not in n:
        return "eft"
    return None


# ---------------------------------------------------------------------------
#  KANAL (RAIL) SINIFLANDIRMA — EFT / FAST / HAVALE'yi katmanlı kanıtla belirler
#  KULLANICI KURALI (Akbank): Dekont başlığı "EFT BANKALAR ARASI HESABA HAVALE"
#  hem EFT hem FAST için kullanılan GENEL bir şablondur; tek başına EFT≠FAST ayrımı
#  YAPMAZ. Ama ÜCRET KALEMİNDE "GEÇ EFT / GECEFT / EFT KOMİSYON / EFT BSMV" ibaresi
#  geçiyorsa işlem KESİN olarak EFT'dir (FAST DEĞİL). Bu, özgünlükten AYRI bir tespittir.
# ---------------------------------------------------------------------------
# RAIL İŞARETLERİ — ÖNCELİK: bankanın işlemi NASIL FATURALADIĞI (tutar/ücret etiketi) esas alınır.
# 'EFT TUTARI/ÜCRETİ' → EFT faturalanmış; 'FAST TUTARI/Ücreti' → FAST faturalanmış. Çıplak '(FAST)'
# ya da 'GİDEN FAST' başlığı DAHA ZAYIF bir teslim-rayı etiketidir (FAST, anlık EFT altyapısıdır);
# açık bir 'EFT ÜCRETİ/TUTARI' faturalamasını EZMEZ. (Kullanıcı içgörüsü + Enpara/İşbank/Akbank kıyası.)
#
# KESİN EFT (GEÇ EFT / GECEFT) — kesim sonrası EFT; her şeyi yener.
_EFT_DEFINITIVE = ("geceft", "geceeft", "gecefteft")
# EFT FATURALAMA (tutar/ücret bu rayla adlandırılmış → güçlü EFT):
_EFT_BILLING = ("eftucreti", "efttutari", "eftkomisyon", "eftbsmv", "eftmasraf", "eftvergi", "eft+vergi")
# FAST FATURALAMA (tutar/ücret bu rayla adlandırılmış → güçlü FAST):
_FAST_BILLING = ("fastucreti", "fasttutari", "gidenfasttutari", "gelenfasttutari",
                 "fastkomisyon", "fastbsmv", "fast+vergi", "dekont/fast")
# FAST TESLİM-RAYI etiketi (ZAYIF — faturalamayı ezmez): çıplak '(FAST)', 'GİDEN FAST' başlığı vb.
_FAST_TAG = ("gidenfast", "gelenfast", "fastislemi", "fastgonderimi", "fastparatransferi",
             "(fast)", "eft(fast)")
# Geri uyumluluk (başka yerlerde kullanılıyorsa): eski birleşik kümeler
_EFT_FEE_MARKERS = _EFT_DEFINITIVE + _EFT_BILLING + ("dekont/eft",)
_FAST_FEE_MARKERS = _FAST_BILLING + _FAST_TAG


def classify_rail(text: str, sender_iban: str = "", receiver_iban: str = "",
                  bkey: str = "", amount=None, fee=None) -> dict | None:
    """İşlem kanalını (EFT/FAST/HAVALE) KATMANLI kanıtla sınıflandırır ve
    denetlenebilir bir kanıt nesnesi döndürür:
       {rail, confidence(0-100), evidence[], notice_tr, notice_en}
    Karar veremezse (belirsiz) rail='belirsiz' döner. Özgünlükten bağımsızdır —
    yalnızca 'bu işlem hangi kanaldan gitti' sorusunu yanıtlar."""
    if not text:
        return None
    import banks as _b
    n = _tr_low(text)
    ns = n.replace(" ", "")
    ev = []

    # Katman 2 — aynı banka mı? (IBAN banka kodları)
    sc = _b.iban_bank_code(_b.normalize_iban(sender_iban or "")) if sender_iban else ""
    rc = _b.iban_bank_code(_b.normalize_iban(receiver_iban or "")) if receiver_iban else ""
    same_bank = bool(sc and rc and sc == rc)
    interbank = bool(sc and rc and sc != rc)

    # Katman 1 — FATURALAMA etiketi (birincil): işlemin tutar/ücreti HANGİ rayla adlandırılmış?
    eft_definitive = any(m in ns for m in _EFT_DEFINITIVE)         # GEÇ EFT / GECEFT (en kesin)
    eft_billing = any(m in ns for m in _EFT_BILLING)               # 'EFT TUTARI/ÜCRETİ...' → EFT
    fast_billing = any(m in ns for m in _FAST_BILLING)             # 'FAST Ücreti / GİDEN FAST TUTARI' → FAST
    fast_tag = (any(m in ns for m in _FAST_TAG)                    # çıplak '(FAST)' / 'GİDEN FAST' başlığı (ZAYIF)
                or bool(re.search(r"(?<![a-z])fast(?![a-z])", n)))
    # Herhangi bir FAST işareti (faturalama VEYA zayıf etiket) — havale/aynı-banka kontrolü için
    fast_label = fast_billing or fast_tag
    eft_fee = eft_definitive or eft_billing                        # (geri uyum: eski kod bu adı kullanıyordu)
    # "eft" KELİMESİ (alt-dize DEĞİL): 'defter', 'geft' gibi kelimeler yanlış-pozitif üretmesin diye
    # kelime sınırı aranır (ör. yasal dipnottaki "Banka'nın DEFTer kayıtları" → 'eft' İÇERMEZ sayılır).
    _eft_word = bool(re.search(r"(?<![a-zçğıöşü])eft(?![a-zçğıöşü])", n)) or ("eftbankalararasi" in ns)
    interbank_title = ("bankalararasi" in ns and _eft_word)         # Akbank GENEL başlığı

    # Başlıkta açık "EFT" (Akbank 'EFT BANKALAR ARASI HESABA HAVALE') — tek başına EFT≠FAST ayırmaz.
    # ALT-DİZE DEĞİL kelime eşleşmesi ('defter'/'geft' yakalanmaz).
    eft_in_title = _eft_word or ("eftbankalararasi" in ns) or ("bankalararasihesaba" in ns and _eft_word)

    # ===================== QNB'YE ÖZEL EFT/FAST AYRIMI (kullanıcı kuralı) =====================
    # QNB dekontlarında işlem türü başlıkta "GİDEN EFT" ya da "GİDEN FAST EFT" olarak yazılır:
    #   - "GİDEN EFT"       → KESİN EFT (bu banka için net gösterge).
    #   - "GİDEN FAST EFT"  → FAST (buradaki 'EFT' sadece genel şablon; ibare FAST teslimini gösterir).
    # Bu kural SADECE QNB kanalına özeldir (bkey='qnb' ya da gönderici IBAN kodu 00111).
    # Boşluklar atıldığı için: "GİDEN EFT"→'gideneft', "GİDEN FAST EFT"→'gidenfasteft'.
    _is_qnb = (bkey == "qnb") or (sc == "00111")
    _qnb_giden_fast_eft = "gidenfasteft" in ns          # ÖNCE bakılır (içinde 'eft' geçse de FAST'tır)
    _qnb_giden_eft = ("gideneft" in ns) and not _qnb_giden_fast_eft
    qnb_definitive_eft = False                          # QNB'ye özel net-EFT bildirimi için bayrak

    rail, conf = "belirsiz", 0
    title_based_eft = False
    # ===================== BİRİNCİL KAPI: IBAN BANKA KODU =====================
    # Kullanıcı kuralı: HAVALE mi yoksa EFT/FAST mi olduğu ÖNCE gönderici↔alıcı IBAN banka koduna
    # göre belirlenir. Aynı kod → banka-içi HAVALE (EFT/FAST olamaz). Farklı kod → bankalararası;
    # EFT mi FAST mı ayrımını ise FATURA etiketi (EFT/FAST TUTARI/ÜCRETİ) yapar.
    def _interbank_rail():
        """Bankalararası (farklı IBAN kodu) durumda EFT/FAST'ı fatura etiketinden ayırır."""
        # QNB'YE ÖZEL (en yüksek öncelik): 'GİDEN FAST EFT'→FAST, 'GİDEN EFT'→KESİN EFT.
        if _is_qnb and _qnb_giden_fast_eft:
            return "fast", 93, "QNB: başlıkta 'GİDEN FAST EFT' → FAST (bu ibare FAST teslimini gösterir; içindeki 'EFT' genel şablondur)."
        if _is_qnb and _qnb_giden_eft:
            return "eft", 96, "QNB: başlıkta 'GİDEN EFT' → KESİN EFT (QNB kanalına özel net gösterge)."
        if eft_definitive:
            return "eft", 95, "Ücret kaleminde 'GEÇ EFT / GECEFT' → KESİN EFT."
        if fast_billing and not eft_billing:
            return "fast", 92, "Tutar/ücret FAST olarak faturalanmış ('FAST Ücreti/TUTARI') → FAST."
        if eft_billing and not fast_billing:
            return "eft", 90, "Tutar/ücret EFT olarak faturalanmış ('EFT TUTARI/ÜCRETİ') → EFT ('(FAST)' teslim rayıdır)."
        if fast_billing and eft_billing:
            return "belirsiz", 40, "Hem FAST hem EFT faturalama etiketi var — çelişki; banka teyidi gerek."
        if fast_tag:
            return "fast", 85, "Açık FAST ibaresi ('GİDEN FAST' / 'FAST Para Transferi') → FAST."
        if eft_in_title:
            return "eft", 75, "Başlıkta EFT + bankalararası + FAST işareti yok → EFT (başlık temelli)."
        return "belirsiz", 45, "Bankalararası ama EFT/FAST ayrımı için açık fatura etiketi yok."

    if same_bank:
        # IBAN OTORİTESİ: aynı banka kodu → banka-içi HAVALE (kesin). Aynı bankada EFT/FAST olamaz.
        rail, conf = "havale", 92
        ev.append("Gönderici ve alıcı IBAN AYNI bankaya ait (banka kodu eşit) → banka-içi HAVALE. "
                  "Aynı bankadaki iki hesap arasında EFT/FAST YAPILAMAZ.")
        if eft_definitive or fast_billing or fast_tag or eft_billing:
            ev.append("NOT: Belge EFT/FAST ibaresi taşıyor ama IBAN'lar aynı bankada — bu bir ÇELİŞKİDİR "
                      "(ayrıca SAMEBANK_RAIL_CONTRADICTION olarak işaretlenir).")
    elif interbank:
        # IBAN OTORİTESİ: farklı banka kodu → bankalararası (EFT ya da FAST; asla HAVALE değil).
        rail, conf, _msg = _interbank_rail()
        ev.append("Gönderici ve alıcı IBAN FARKLI bankalarda (banka kodu farklı) → bankalararası "
                  "(EFT ya da FAST; HAVALE olamaz).")
        ev.append(_msg)
        # QNB 'GİDEN EFT' → net EFT (başlık-temelli belirsiz EFT DEĞİL): QNB'ye özel bildirim kullanılır.
        qnb_definitive_eft = _is_qnb and _qnb_giden_eft and rail == "eft"
        title_based_eft = (rail == "eft" and not eft_definitive and not eft_billing
                           and not qnb_definitive_eft)
    else:
        # IBAN kodları eksik/okunamadı → yalnız METİN fatura etiketiyle karar (yedek).
        # QNB'YE ÖZEL (en yüksek öncelik): 'GİDEN FAST EFT'→FAST, 'GİDEN EFT'→EFT.
        if _is_qnb and _qnb_giden_fast_eft:
            rail, conf = "fast", 90; ev.append("QNB: 'GİDEN FAST EFT' → FAST (IBAN kodu okunamadı).")
        elif _is_qnb and _qnb_giden_eft:
            rail, conf = "eft", 92; qnb_definitive_eft = True
            ev.append("QNB: 'GİDEN EFT' → KESİN EFT (IBAN kodu okunamadı, QNB kanalına özel).")
        elif eft_definitive:
            rail, conf = "eft", 90; ev.append("'GEÇ EFT/GECEFT' → EFT (IBAN kodu okunamadı).")
        elif fast_billing and not eft_billing:
            rail, conf = "fast", 88; ev.append("FAST faturalama → FAST (IBAN kodu okunamadı).")
        elif eft_billing and not fast_billing:
            rail, conf = "eft", 86; ev.append("EFT faturalama → EFT (IBAN kodu okunamadı).")
        elif (("hesaptanhavale" in ns or "hesaptanhesabahavale" in ns) and not eft_fee
              and not fast_label and not _eft_word and "bankalararasi" not in ns):
            # 'Hesaptan (Hesaba) Havale' başlığı + GERÇEK EFT/FAST işareti YOK + 'bankalararası' YOK →
            # banka-içi HAVALE (ör. Ziraat Mobil). Akbank'ın 'EFT BANKALAR ARASI HESABA HAVALE' genel
            # şablonu HARİÇ tutulur (o başlıkta 'eft' KELİMESİ ve 'bankalararası' vardır).
            rail, conf = "havale", 80
            ev.append("İşlem türü 'Hesaptan Hesaba Havale' (banka-içi), EFT/FAST işareti yok → HAVALE.")
        elif fast_tag:
            rail, conf = "fast", 80; ev.append("Açık FAST ibaresi → FAST (IBAN kodu okunamadı).")
        elif eft_in_title:
            rail, conf = "eft", 70; title_based_eft = True; ev.append("Başlıkta EFT → EFT (IBAN kodu okunamadı).")
        else:
            return None

    _RL = {"eft": "EFT", "fast": "FAST", "havale": "HAVALE", "belirsiz": "BELİRSİZ"}[rail]
    if rail == "eft" and qnb_definitive_eft:
        notice_tr = ("İŞLEM KANALI: Bu işlem bir **EFT** işlemidir — **FAST DEĞİLDİR**. QNB dekontunun "
                     "başlığında 'GİDEN EFT' ibaresi geçiyor; bu, QNB kanalında EFT işleminin NET "
                     "göstergesidir ('GİDEN FAST EFT' yazsaydı FAST olurdu). NOT: Bu, kanal "
                     "sınıflandırmasıdır; dekontun sahte olup olmadığından AYRIDIR.")
        notice_en = ("TRANSFER RAIL: This is an **EFT** transaction — **NOT FAST**. The QNB receipt title "
                     "contains 'GİDEN EFT', a definitive EFT indicator for the QNB channel (had it said "
                     "'GİDEN FAST EFT' it would be FAST). NOTE: rail classification, separate from authenticity.")
    elif rail == "fast" and _is_qnb and _qnb_giden_fast_eft:
        notice_tr = ("İŞLEM KANALI: Bu işlem bir **FAST** işlemidir. QNB dekontunda 'GİDEN FAST EFT' ibaresi "
                     "geçiyor; bu ibare QNB'de FAST teslimini gösterir (içindeki 'EFT' genel şablondur).")
        notice_en = ("TRANSFER RAIL: This is a **FAST** transaction. The QNB receipt says 'GİDEN FAST EFT', "
                     "which denotes FAST delivery on the QNB channel (the embedded 'EFT' is a generic template).")
    elif rail == "eft" and title_based_eft:
        notice_tr = ("İŞLEM KANALI: Bu işlem büyük olasılıkla bir **EFT** işlemidir — **FAST DEĞİLDİR**. "
                     "Gerekçe: dekont başlığında 'EFT BANKALAR ARASI HESABA HAVALE' ibaresi var, işlem "
                     "bankalararası (gönderici ve alıcı farklı bankalarda) ve belgede HİÇBİR FAST işareti "
                     "(FAST ücreti/Sorgu No/başlık) yok. Kesinlik için işlem/sıra numarasıyla bankadan teyit "
                     "alınabilir. NOT: Bu, kanal sınıflandırmasıdır; dekontun sahte olup olmadığından AYRIDIR.")
        notice_en = ("TRANSFER RAIL: This is most likely an **EFT** transaction — **NOT FAST**. Rationale: the "
                     "title says 'EFT BANKALAR ARASI HESABA HAVALE', the transfer is interbank, and there is NO "
                     "FAST marker anywhere. Confirm with the bank via the transaction number for certainty. "
                     "NOTE: rail classification, separate from authenticity.")
    elif rail == "eft":
        notice_tr = ("İŞLEM KANALI: Bu işlem bir **EFT** işlemidir — **FAST DEĞİLDİR**. Dekontun ücret "
                     "kaleminde 'GEÇ EFT / EFT' ibaresi geçiyor; bu, Akbank'ta EFT kanalının KESİN "
                     "göstergesidir (başlıktaki 'EFT BANKALAR ARASI HESABA HAVALE' ifadesi hem EFT hem "
                     "FAST için kullanılan genel bir şablondur, tek başına ayırt etmez). NOT: Bu tespit, "
                     "dekontun sahte olup olmadığından AYRIDIR; kanal sınıflandırmasıdır.")
        notice_en = ("TRANSFER RAIL: This is an **EFT** transaction — **NOT FAST**. The fee line carries the "
                     "'GEÇ EFT / EFT' marker, a definitive EFT indicator for Akbank (the title 'EFT BANKALAR "
                     "ARASI HESABA HAVALE' is a generic template used for both EFT and FAST). NOTE: this is a "
                     "rail classification, separate from authenticity.")
    elif rail == "fast":
        notice_tr = ("İŞLEM KANALI: Bu işlem bir **FAST** işlemidir (dekontta açık FAST ibaresi var).")
        notice_en = ("TRANSFER RAIL: This is a **FAST** transaction (explicit FAST marker present).")
    elif rail == "havale":
        notice_tr = ("İŞLEM KANALI: Bu işlem banka-içi **HAVALE**'dir (gönderici ve alıcı aynı bankada).")
        notice_en = ("TRANSFER RAIL: Intra-bank **HAVALE** (sender and receiver at the same bank).")
    else:
        notice_tr = ("İŞLEM KANALI BELİRSİZ: Dekontta açık EFT/FAST ücret etiketi bulunamadı; başlık genel "
                     "olduğundan kanal kesinleşmiyor. Kesinlik için işlem/sıra numarası ile banka teyidi gerekir.")
        notice_en = ("RAIL UNDETERMINED: no explicit EFT/FAST fee marker found; the title is generic. Confirm "
                     "the rail via the transaction number with the bank.")

    return {"rail": rail, "confidence": conf, "evidence": ev,
            "notice_tr": notice_tr, "notice_en": notice_en}


def check_amount_currency_consistency(text: str, bkey: str = "") -> dict | None:
    """PARA BİRİMİ SONEKİ TUTARLILIĞI (gerçek-şablon karşılaştırması). Gerçek dekontlarda ana
    tutar ve masraf/ücret AYNI biçimde yazılır (ör. gerçek VakıfBank: hem 'İşlem Tutarı 40.416,00 TL'
    hem 'Masraf Tutarı 16,76 TL'). Ana tutarda 'TL' varken MASRAF/ÜCRET tutarında YOKSA bu biçim
    tutarsızlığıdır — masraf alanı sonradan eklenmiş/değiştirilmiş olabilir (gerçek şablondan sapma)."""
    if not text:
        return None
    m_amt = re.search(r"[İIıi]?[şsSŞ]lem\s*Tutar[ıi]\s*[:：]?\s*([\d.]+,\d{2})\s*(TL|TRY|₺)?", text, re.I)
    # Masraf/Ücret/Komisyon tutarı (etiketli)
    m_fee = re.search(r"(?:Masraf(?:\s*Tutar[ıi])?|[İIıi]?[şs]lem\s*[ÜUu]creti|Komisyon)\s*[:：]?\s*"
                      r"([\d.]+,\d{2})\s*(TL|TRY|₺)?", text, re.I)
    if not m_amt or not m_fee:
        return None
    amt_has_cur = bool(m_amt.group(2))
    fee_has_cur = bool(m_fee.group(2))
    # Yalnız ANA tutar birimli ama MASRAF birimsiz durumunu işaretle (tersinde OCR kırpması olası)
    if amt_has_cur and not fee_has_cur:
        return {
            "code": "AMOUNT_CURRENCY_INCONSISTENT", "severity": "high", "weight": 22,
            "tr": f"BİÇİM TUTARSIZLIĞI (gerçek şablondan sapma): Belgede işlem tutarı para birimiyle "
                  f"('{m_amt.group(1)} {m_amt.group(2)}') yazılmışken MASRAF tutarı ('{m_fee.group(1)}') "
                  f"PARA BİRİMİ (TL) OLMADAN yazılmış. Bu bankanın GERÇEK dekontlarında masraf da her zaman "
                  f"'TL' ile yazılır; sonekin eksikliği masraf alanının sonradan eklendiğine/değiştirildiğine "
                  f"işaret edebilir. Fotoğrafta kesinlik düşüktür; orijinal dijital PDF ile teyit edin.",
            "en": f"FORMAT INCONSISTENCY (deviation from genuine template): the transaction amount carries a "
                  f"currency ('{m_amt.group(1)} {m_amt.group(2)}') but the FEE ('{m_fee.group(1)}') has NO "
                  f"currency suffix. Genuine receipts of this bank always show the fee with 'TL'; the missing "
                  f"suffix may indicate the fee field was added/altered.",
            "detail": f"amount='{m_amt.group(0).strip()}' fee='{m_fee.group(0).strip()}' bank={bkey}"}
    return None


def issuer_iban_codes(bkey: str) -> set:
    """İhraççı bankanın (bkey) IBAN banka kodları kümesi (ör. 'kuveyt'→{'00205'}). Boşsa boş küme."""
    return set(_ISSUER_IBAN_CODES.get(bkey) or set())


def check_samebank_rail_contradiction(text: str, sender_iban: str, receiver_iban: str,
                                      issuer_codes: set | None = None) -> dict | None:
    """AYNI-BANKA ↔ 'BANKALARARASI/EFT/FAST' BAŞLIK ÇELİŞKİSİ (sahtecilik).
    Gönderici ve alıcı AYNI bankadaysa işlem banka-içidir; ama dekont başlığı/türü 'bankalar arası' /
    EFT / FAST diyorsa bu İMKÂNSIZDIR — işlem türü uydurulmuş.
    GÜVENİLİR TARAF BELİRLEME: Göndericinin bankası = belge İHRAÇÇISIDIR (üst başlık/logo). Gönderici
    IBAN'ı çoğu dekontta YAZILI DEĞİLDİR ya da OCR/vision onu yanlış okuyup/atayıp KARŞI-TARAF (alıcı)
    IBAN'ıyla karıştırabilir. Bu yüzden 'aynı banka mı' kararı ÖNCELİKLE İHRAÇÇI (issuer_codes) ile ALICI
    IBAN bankası KODU kıyaslanarak verilir: ihraççı ≠ alıcı bankası ise işlem BANKALARARASIDIR → çelişki
    YOKTUR (ör. Kuveyt Türk'ten Ziraat'a FAST — gönderici IBAN'ı yanlış Ziraat okunsa bile). issuer_codes
    bilinmiyorsa ESKİ davranışa (gönderici IBAN'ı vs alıcı IBAN, ikisi de geçerli+aynı banka+farklı) düşülür."""
    if not text:
        return None
    import banks as _b
    ns = _tr_low(text).replace(" ", "")
    asserts_interbank = ("bankalararasi" in ns or "eftbankalar" in ns or "dekont/eft" in ns
                         or "eftucreti" in ns or "geceft" in ns
                         or bool(re.search(r"(?<![a-z])fast(?![a-z])", _tr_low(text))))
    if not asserts_interbank:
        return None                     # banka-içi başlık (ör. ÖDEME EMİRLERİ) → sorun yok
    r = _b.normalize_iban(receiver_iban or "")
    rc = _b.iban_bank_code(r) if (_b.iban_valid(r) is True) else ""

    # ÖNCELİK: İHRAÇÇI (gönderici bankası) ↔ ALICI IBAN bankası. Gönderici IBAN'ına GÜVENME.
    if issuer_codes:
        if not rc:
            return None                 # alıcı IBAN geçerli okunamadı → iddia etme
        if rc not in issuer_codes:
            return None                 # ihraççı (gönderici) ≠ alıcı bankası → BANKALARARASI, çelişki YOK
        # rc ihraççı kodlarında → alıcı da göndericiyle AYNI bankada; başlık interbank → ÇELİŞKİ
        sc = rc
    else:
        # İhraççı bilinmiyor → eski, IBAN-çifti temelli kesin kontrol (yanlış-pozitife karşı katı)
        s = _b.normalize_iban(sender_iban or "")
        if not s or not r or s == r:
            return None
        if _b.iban_valid(s) is False or _b.iban_valid(r) is False:
            return None
        sc = _b.iban_bank_code(s)
        if not sc or not rc or sc != rc:
            return None
    bank_lbl = _b.bank_label_from_iban(r) or f"kod {sc}"
    return {
        "code": "SAMEBANK_RAIL_CONTRADICTION", "severity": "critical", "weight": 46,
        "tr": f"İŞLEM TÜRÜ ÇELİŞKİSİ (SAHTECİLİK): Gönderici (ihraççı) ve alıcı AYNI bankaya ait "
              f"({bank_lbl}, banka kodu {sc}); yani bu banka-içi bir transferdir. Ancak dekont "
              f"kendini 'BANKALAR ARASI / EFT / FAST' olarak gösteriyor — aynı bankadaki iki hesap "
              f"arasında bankalararası (EFT/FAST) işlem YAPILAMAZ. İşlem türü/başlık uydurulmuş; "
              f"güçlü sahtecilik işareti. (Gerçek banka-içi transfer farklı bir başlık taşır.)",
        "en": f"RAIL CONTRADICTION (FORGERY): sender (issuer) and receiver are at the SAME bank "
              f"({bank_lbl}, code {sc}) — an intra-bank transfer — yet the receipt labels itself "
              f"'INTERBANK / EFT / FAST'. A same-bank transfer cannot be EFT/FAST; the type was "
              f"fabricated. Strong forgery signal.",
        "detail": f"issuer/sender_code={sc} receiver_code={rc}"}


# HAVALE'ye ÖZGÜ (banka-içi) ücret/işlem etiketleri — İ-güvenli, boşluk-duyarsız.
# Bu etiketler işlemin HAVALE olarak ÜCRETLENDİRİLDİĞİNİ gösterir (banka-içi bir kanal).
_HAVALE_FEE_MARKERS = ("havaleucreti", "havale+vergi", "hvlucreti", "hvl+vergi", "dekont/hvl",
                       "havalemasrafi", "havalekomisyonu")


def check_interbank_havale_contradiction(text: str, sender_iban: str, receiver_iban: str) -> dict | None:
    """BANKALARARASI ↔ HAVALE ÇELİŞKİSİ (sahtecilik/tutarsızlık — AYNA denetim).
    HAVALE banka-İÇİ bir kanaldır. Gönderici ve alıcı IBAN FARKLI bankalara aitse işlem
    bankalararasıdır; ama dekont kendini HAVALE olarak (havale ücreti/kalemi) gösteriyorsa bu
    İMKÂNSIZDIR — farklı bankalar arasında HAVALE yapılamaz (EFT/FAST olmalı). İşlem türü/ücret
    uydurulmuş ya da yanlış; kanal gizlenmeye çalışılmış olabilir → puanı düşüren bir bulgu.
    IBAN'a dayalı olduğundan iki IBAN da mod-97 GEÇERLİ ve FARKLI banka olmalı (OCR yanlış-pozitifi yok).
    Akbank'ın 'EFT BANKALAR ARASI HESABA HAVALE' genel şablonu HARİÇTİR (EFT/bankalararası ibaresi taşır)."""
    if not text:
        return None
    import banks as _b
    s = _b.normalize_iban(sender_iban or "")
    r = _b.normalize_iban(receiver_iban or "")
    if not s or not r or s == r:
        return None
    if _b.iban_valid(s) is False or _b.iban_valid(r) is False:
        return None                     # geçersiz IBAN = okuma hatası
    sc, rc = _b.iban_bank_code(s), _b.iban_bank_code(r)
    if not sc or not rc or sc == rc:
        return None                     # aynı banka → burada çelişki yok (gerçek havale olabilir)
    n = _tr_low(text)
    ns = n.replace(" ", "")
    # İşlem HAVALE olarak mı sunuluyor? (a) havale ücret kalemi, (b) 'Hesaptan/Hesaba Havale' TÜRÜ,
    # (c) net 'havale' işlem türü. Böylece yalnız ücret değil, BAŞLIK/TÜR 'HAVALE' diyorsa da yakalanır.
    claims_havale = (any(m in ns for m in _HAVALE_FEE_MARKERS)
                     or "hesaptanhavale" in ns or "hesabahavale" in ns
                     or "islemturuhavale" in ns or "islemtipihavale" in ns or "islemturu:havale" in ns)
    # EFT/FAST/bankalararası ibaresi VARSA çelişki yok (doğru şekilde bankalararası etiketlenmiş);
    # Akbank 'EFT BANKALAR ARASI HESABA HAVALE' şablonu da bu kapıdan elenir.
    asserts_interbank_rail = ("eft" in ns or "bankalararasi" in ns
                              or bool(re.search(r"(?<![a-z])fast(?![a-z])", n)))
    if not claims_havale or asserts_interbank_rail:
        return None
    s_lbl = _b.bank_label_from_iban(s) or f"kod {sc}"
    r_lbl = _b.bank_label_from_iban(r) or f"kod {rc}"
    return {
        "code": "INTERBANK_HAVALE_CONTRADICTION", "severity": "high", "weight": 30,
        "tr": f"İŞLEM TÜRÜ ÇELİŞKİSİ: Dekont işlemi HAVALE olarak gösteriyor, ancak gönderici ({s_lbl}, "
              f"kod {sc}) ve alıcı ({r_lbl}, kod {rc}) FARKLI bankalarda. Farklı bankalar arasında "
              f"HAVALE YAPILAMAZ — bu bir bankalararası işlemdir ve EFT ya da FAST olmalıdır. Belge "
              f"kendini yanlış/uydurma bir kanal (havale) ile sunuyor; kanal gizlenmiş ya da tür tahrif "
              f"edilmiş olabilir. Bu güçlü bir tutarsızlık işaretidir; orijinal dijital dekontla teyit edin.",
        "en": f"RAIL CONTRADICTION: the receipt presents the transaction as HAVALE, but sender ({s_lbl}, "
              f"code {sc}) and receiver ({r_lbl}, code {rc}) are at DIFFERENT banks. HAVALE cannot occur "
              f"between different banks — this is an interbank transfer and must be EFT or FAST. The stated "
              f"rail is wrong/fabricated. Strong inconsistency signal.",
        "detail": f"sender_code={sc} receiver_code={rc} claims_havale=1"}


# Kimlik (TCKN) alan etiketleri — BANKA-GENEL. Gönderen/işlemi-yapan tarafındaki 11 haneli
# kimlik numarasını taşıyan alan adları (bankadan bankaya değişir).
_ID_LABELS = [
    r"islemi\s*yapan\s*tckn", r"islemi\s*yapan\s*(?:tc\s*)?kimlik(?:\s*no)?",
    r"gonderen\s*tckn", r"gonderen\s*(?:tc\s*)?kimlik(?:\s*no)?",
    r"tckn", r"tc\s*kimlik\s*(?:no)?", r"t\.?c\.?\s*kimlik\s*(?:no)?",
    r"kimlik\s*no", r"vkn\s*/?\s*vergi\s*(?:kimlik\s*)?(?:dair[a-z]*|no)?",
    r"vergi\s*kimlik\s*no",
]


def _party_identity_ids(text: str) -> set:
    """Dekonttan GÖNDEREN/İŞLEMİ-YAPAN (aynı kişi) tarafına ait 11 haneli kimlik
    numaralarını toplar. Alıcı bloğundaki kimlikler (farklı kişi olabilir) HARİÇ tutulur;
    'İşlemi Yapan' yalnızca KENDİSİ ise aktörün kimliği de gönderenle aynı sayılır.
    Banka-geneldir: Akbank ('VKN/Vergi' + 'İşlemi Yapan TCKN'), Ziraat/Garanti/İş/YKB vb.
    'TC Kimlik No', 'TCKN', 'Kimlik No' etiketlerini de tanır."""
    low = _tr_low(text)
    # Alıcı bloğunu ayır: alıcı kimlikleri (varsa) farklı kişiye ait olabilir → dışla
    cut = re.search(r"(alici\s*bilgileri|alacakli|alici\b)", low)
    sender_region = low[:cut.start()] if cut else low
    # 'İşlemi Yapan' alanı KENDİSİ mi? (aktör = gönderen)
    actor_self = bool(re.search(r"islemi\s*yapan\s*[:：]?\s*kendis", low))
    ids = set()
    # Gönderen bölgesindeki tüm kimlik-etiketli 11 haneli numaralar
    for lab in _ID_LABELS:
        for m in re.finditer(lab + r"\s*[:：]?\s*([0-9]{10,11})", sender_region):
            v = m.group(1)
            if len(v) == 11:
                ids.add(v)
    # 'İşlemi Yapan TCKN/Kimlik' (belge sonunda) — yalnız aktör KENDİSİ ise gönderenle aynı say
    if actor_self:
        for m in re.finditer(r"islemi\s*yapan\s*(?:tckn|(?:tc\s*)?kimlik[a-z ]*)\s*[:：]?\s*([0-9]{11})", low):
            ids.add(m.group(1))
    return ids


def check_id_field_consistency(text: str, input_kind: str = "pdf",
                               text_source: str = "digital") -> dict | None:
    """KİMLİK ALAN TUTARLILIĞI (sahtecilik) — BANKA-GENEL. Bir dekontta gönderen/işlemi-yapan
    (aynı kişi) için birden çok kimlik alanı varsa hepsi AYNI TCKN'yi taşımalıdır. Farklı
    numaralar görünüyorsa ya da biri kontrol basamağı sağlamasını geçemiyorsa alan uydurulmuş/
    değiştirilmiştir. İki alan metinde aynı yazıldığından fotoğrafta bile OCR ikisini AYNI okur →
    mismatch güvenilir. FP koruması: fotoğraf/OCR'da salt-mismatch YETMEZ, biri sağlamasız olmalı;
    alıcı kimlikleri (farklı kişi) hariç tutulur; aktör KENDİSİ değilse karşılaştırılmaz."""
    if not text:
        return None
    import banks as _b
    ids = _party_identity_ids(text)
    if len(ids) < 2:
        return None                                   # tek (ya da sıfır) kimlik → mismatch yok
    ids = sorted(ids)
    validity = {v: _b.id_valid(v) for v in ids}
    invalid_present = any(x is False for x in validity.values())
    pixel = (input_kind == "image" or text_source in ("ocr", "vision"))
    if pixel and not invalid_present:
        # Fotoğrafta salt-mismatch OCR hatası olabilir; sağlama-desteği yoksa bastır (FP koruması)
        return None
    sev, w = ("critical", 45) if invalid_present else ("high", 30)
    _bad = [v for v, ok in validity.items() if ok is False]
    _badtxt = (" Ayrıca " + ", ".join(f"'{v}'" for v in _bad) +
               " resmî kontrol basamağı sağlamasını GEÇEMİYOR.") if _bad else ""
    return {
        "code": "ID_FIELD_MISMATCH", "severity": sev, "weight": w,
        "tr": f"KİMLİK ALAN ÇELİŞKİSİ (SAHTECİLİK): Aynı kişi (gönderen/işlemi yapan) için dekontta "
              f"FARKLI kimlik numaraları görünüyor: {', '.join(ids)}. Gerçek bir dekontta bu alanların "
              f"hepsi aynı kişinin TCKN'sini birebir taşır.{_badtxt} Alanlardan biri uydurulmuş/"
              f"değiştirilmiş — güçlü sahtecilik işareti.",
        "en": f"IDENTITY FIELD CONFLICT (FORGERY): different national-ID numbers appear for the same "
              f"party (sender/transacting person): {', '.join(ids)}. On a genuine receipt these fields "
              f"carry the same TCKN.{(' One fails the official checksum.') if _bad else ''} A field was "
              f"fabricated/altered.",
        "detail": f"ids={ids} validity={validity}"}


def check_fast_limit(text: str, amount, learned_max=None) -> dict | None:
    """FAST işlem-başına üst limiti. SABİT sayı yerine VERİ-ODAKLI çalışır:
      etkin_limit = max(regülasyon tabanı, elimizdeki GERÇEK FAST dekontlarının en yüksek tutarı)
    Böylece TCMB/bankalar limiti artırdığında ve gerçek büyük-FAST geldikçe tavan KENDİLİĞİNDEN
    yükselir → yanlış-pozitif olmaz. Regülasyon tabanı DEKONT_FAST_LIMIT ile ayarlanır (varsayılan
    100.000 TL). Aşımda bile YUMUŞAK ağırlık verilir (gerçek büyük-FAST'i asla 'kesin sahte' yapmaz;
    kaydedilebilir kalır ki tavanı öğrensin)."""
    if amount is None:
        return None
    try:
        amt = float(amount)
        floor = float(os.environ.get("DEKONT_FAST_LIMIT", "100000"))
    except Exception:
        return None
    if detect_transfer_rail(text) != "fast":
        return None
    try:
        lm = float(learned_max) if learned_max else 0.0
    except Exception:
        lm = 0.0
    limit = max(floor, lm)                       # taban + gerçek dekontlardan öğrenilen tavan
    if amt <= limit:
        return None
    return {
        "code": "FAST_LIMIT_EXCEEDED", "severity": "medium", "weight": 12,
        "tr": f"FAST TUTAR ANOMALİSİ: işlem FAST olarak görünüyor ve tutar ({amt:,.0f} TL) hem regülasyon "
              f"tabanını hem de bugüne dek gördüğümüz gerçek FAST dekontlarının en yükseğini ({limit:,.0f} TL) "
              f"aşıyor. FAST işlem-başına üst limiti vardır; bu tutar için EFT/havale beklenir — tutar ya da "
              f"işlem türü değiştirilmiş OLABİLİR (bilgi/uyarı, tek başına kesin kanıt değil).".replace(",", "."),
        "en": f"FAST AMOUNT ANOMALY: labeled FAST and the amount ({amt:,.0f} TL) exceeds both the regulatory "
              f"floor and the largest genuine FAST we have seen ({limit:,.0f} TL). FAST has a per-transaction "
              f"limit; EFT/havale would be expected — amount or type MAY be altered (advisory).",
        "detail": f"amount={amt} effective_limit={limit} floor={floor} learned_max={lm}",
    }


def check_rail_bank(text: str, sender_iban: str, receiver_iban: str,
                    all_ibans: list | None = None) -> dict | None:
    """İşlem türü (rail) ↔ taraf bankaları tutarlılığı (KULLANICI KURALI):
    FAST ve EFT BANKALARARASI sistemlerdir. Gönderici ve alıcı IBAN AYNI bankaya aitse
    (aynı banka kodu), transfer banka içi HAVALE'dir; FAST/EFT olamaz. Dekont FAST/EFT
    diyorsa işlem türü değiştirilmiş demektir — güçlü, deterministik tahrifat işareti.
    Koruma: belgede ≥2 FARKLI banka IBAN'ı varsa 'aynı banka' sonucu bir çıkarım hatasıdır
    (vision taraf-eşlemesini çökertmiş olabilir) → bastırılır, yanlış-pozitif önlenir."""
    import banks as _b
    rail = detect_transfer_rail(text)
    if rail not in ("fast", "eft"):
        return None
    sc = _b.iban_bank_code(sender_iban or "")
    rc = _b.iban_bank_code(receiver_iban or "")
    if not sc or not rc:
        return None
    # SAVUNMA: gönderen ve alıcı IBAN'ı BİREBİR AYNI string ise bu bir çıkarım/kopyalama
    # hatasıdır (tek IBAN okunup iki tarafa da yazılmış), gerçek 'aynı banka' transferi değil.
    # Gerçek banka-içi havalede iki FARKLI hesap IBAN'ı aynı banka kodunu taşır. → bastır.
    if _b.normalize_iban(sender_iban) == _b.normalize_iban(receiver_iban):
        return None
    # IBAN checksumları geçersizse bunu IBAN_INVALID ele alır; burada karışma
    if _b.iban_valid(sender_iban) is False or _b.iban_valid(receiver_iban) is False:
        return None
    if sc != rc:
        return None
    # Belge birden fazla FARKLI banka IBAN'ı içeriyorsa "aynı banka" yanılgıdır -> bastır
    _doc_codes = {_b.iban_bank_code(_b.normalize_iban(ib)) for ib in (all_ibans or []) if ib
                  and _b.iban_valid(_b.normalize_iban(ib)) is not False}
    _doc_codes.discard("")
    if len(_doc_codes) >= 2:
        return None
    return {
        "code": "RAIL_SAMEBANK_MISMATCH", "severity": "critical", "weight": 42,
        "tr": f"İŞLEM TÜRÜ ÇELİŞKİSİ: dekont {_RAIL_LABEL[rail]} işlemi olarak görünüyor ama gönderici ve "
              f"alıcı IBAN AYNI bankaya ait (banka kodu {sc}). {_RAIL_LABEL[rail]} bankalararası bir sistemdir; "
              f"aynı banka içindeki transfer FAST/EFT ile YAPILAMAZ, HAVALE olur. İşlem türü sonradan "
              f"değiştirilmiş — güçlü sahtecilik işareti.",
        "en": f"RAIL MISMATCH: labeled {_RAIL_LABEL[rail]} but sender and receiver IBANs are at the SAME bank "
              f"(code {sc}). {_RAIL_LABEL[rail]} is an inter-bank rail; a same-bank transfer cannot be FAST/EFT, "
              f"it is a HAVALE. The transaction type was altered — strong forgery signal.",
        "detail": f"rail={rail} sender_code={sc} receiver_code={rc}",
    }


def check_fee_rail(bkey: str, text: str, fee, learned: dict | None = None) -> dict | None:
    """İşlem türü (rail) ile ÜCRET tarifesi uyumsuzsa (ör. FAST etiketli ama ücret
    HAVALE tarifesinde) tahrifat sinyali döndürür. `learned`: store'daki gerçek
    dekontlardan öğrenilen {rail: [fee,...]} — seed referanslarla birleştirilir."""
    if fee is None or not bkey:
        return None
    prof = dict(_RAIL_FEE_REF.get(bkey) or {})
    if learned:
        for rl, vals in learned.items():
            if vals:
                prof[rl] = sorted(set((prof.get(rl) or []) + list(vals)))
    if not prof:
        return None
    rail = detect_transfer_rail(text)
    if not rail or rail not in prof:
        return None
    try:
        fee = float(fee)
    except Exception:
        return None

    def _near(f, refs, tol=0.18):
        return any(abs(f - r) <= max(1.0, r * tol) for r in refs)

    if _near(fee, prof[rail]):
        return None                       # ücret kendi rail'ine uyuyor -> sorun yok
    # DÜŞÜK-ÜCRET AYIRT-EDİLEMEZLİK KAPISI: Türk bankalarında küçük tutarlarda FAST/EFT/HAVALE
    # ücretleri neredeyse AYNIDIR (komisyon+BSMV; ör. YKB FAST 8,37 ↔ HAVALE 8,38 = 1 kuruş fark).
    # FAST ÜCRETİ TUTARA GÖRE KADEMELİDİR; öğrenilmiş tek bir yüksek değer (ör. 16,76) o rail'in
    # TÜM tarifesi değildir. Bu yüzden ücret bu eşiğin ALTINDAYSA rail'ler ücretle ayırt edilemez
    # → yanlış 'ÜCRET–İŞLEM TÜRÜ ÇELİŞKİSİ' üretme (kullanıcı bildirdi: 1 kuruşluk fark).
    _fee_rail_min = 12.0
    try:
        _fee_rail_min = float(os.environ.get("DEKONT_FEE_RAIL_MIN", "12"))
    except Exception:
        _fee_rail_min = 12.0
    if fee < _fee_rail_min:
        return None
    for other, refs in prof.items():
        # eşleşilen KARŞI rail değeri de ayırt-edilebilir seviyede olmalı (düşük değerlerde çakışır)
        if other != rail and _near(fee, refs) and min(refs) >= _fee_rail_min:
            return {
                "code": "FEE_RAIL_MISMATCH", "severity": "critical", "weight": 40,
                "tr": f"ÜCRET–İŞLEM TÜRÜ ÇELİŞKİSİ: işlem {_RAIL_LABEL[rail]} olarak görünüyor ama ücret+vergi "
                      f"({fee:g} TL) bu bankanın {_RAIL_LABEL[other]} tarifesine (~{refs[0]:g} TL) uyuyor; "
                      f"{_RAIL_LABEL[rail]} tarifesine değil. Gerçek bir {_RAIL_LABEL[rail]} işleminde ücret "
                      f"farklıdır. Bu dekont büyük olasılıkla bir {_RAIL_LABEL[other]} dekontundan üretilip "
                      f"tutar/işlem türü değiştirilmiş, ücret güncellenmemiş — güçlü sahtecilik işareti.",
                "en": f"FEE–RAIL MISMATCH: labeled {_RAIL_LABEL[rail]} but the fee ({fee:g} TL) matches this "
                      f"bank's {_RAIL_LABEL[other]} tariff (~{refs[0]:g} TL), not {_RAIL_LABEL[rail]}. Likely "
                      f"built from a {_RAIL_LABEL[other]} receipt with amount/type changed but fee left unchanged.",
                "detail": f"rail={rail} fee={fee} own={prof[rail]} matched_{other}={refs}",
            }
    return None


# ---------------------------------------------------------------------------
#  SIRA NO (İŞLEM ANI) ↔ DÜZENLENME TARİHİ (BELGE OLUŞTURMA) TUTARLILIĞI
#  Garanti BBVA dekontunda SIRA NO gömülü tam zaman damgası taşır:
#     'SIRA NO : 2026-08-18-23.56.48.697190'  -> işlemin yapıldığı AN (YYYY-MM-DD-HH.MM.SS)
#  DÜZENLENME TARİHİ ise belgenin OLUŞTURULDUĞU andır ('19.08.2026 00:15:55').
#  MANTIK: Belge, işlemden ÖNCE oluşturulamaz. Oluşturma < işlem ise (birkaç dk toleransın
#  ötesinde) bu İMKÂNSIZDIR -> geriye tarihleme/tahrifat. Oluşturma >= işlem ise (işlemden
#  hemen sonra dekont alınır) TUTARLIDIR -> olumlu doğrulama. Bu, fotoğrafta da çalışır.
# ---------------------------------------------------------------------------
def check_seq_vs_creation(text: str) -> dict | None:
    if not text:
        return None
    m1 = re.search(r"SIRA\s*NO\s*[:：]?\s*(\d{4})-(\d{2})-(\d{2})-(\d{2})[.:](\d{2})[.:](\d{2})", text, re.I)
    m2 = re.search(r"D[ÜUÜü]ZENLENME\s*TAR[İIıi]H[İIıi]?\s*[:：]?\s*(\d{2})[.\-/](\d{2})[.\-/](\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?",
                   text, re.I)
    if not m1 or not m2:
        return None
    try:
        txn = _dt.datetime(int(m1.group(1)), int(m1.group(2)), int(m1.group(3)),
                           int(m1.group(4)), int(m1.group(5)), int(m1.group(6)))
        cre = _dt.datetime(int(m2.group(3)), int(m2.group(2)), int(m2.group(1)),
                           int(m2.group(4)), int(m2.group(5)), int(m2.group(6) or 0))
    except Exception:
        return None
    gap = (cre - txn).total_seconds()
    # Belge işlemden ÖNCE mi oluşturulmuş? (2 dk saat-kayması toleransı) -> imkânsız
    if gap < -120:
        return {
            "code": "SEQ_CREATION_BACKDATE", "severity": "critical", "weight": 45,
            "tr": f"GERİYE TARİHLEME (İMKÂNSIZ): işlem SIRA NO'ya göre {txn:%d.%m.%Y %H:%M:%S}'de yapılmış, "
                  f"ancak belge DÜZENLENME TARİHİ {cre:%d.%m.%Y %H:%M:%S} — yani dekont işlemden ÖNCE "
                  f"oluşturulmuş görünüyor. Bir dekont, kaydettiği işlemden önce üretilemez; SIRA NO ya da "
                  f"düzenlenme tarihi/saati sonradan değiştirilmiş — güçlü sahtecilik işareti.",
            "en": f"BACKDATING (IMPOSSIBLE): the transaction occurred at {txn:%Y-%m-%d %H:%M:%S} per SIRA NO, "
                  f"but the document creation time is {cre:%Y-%m-%d %H:%M:%S} — the receipt appears created "
                  f"BEFORE its transaction. Impossible; SIRA NO or creation time was altered — strong forgery signal.",
            "detail": f"txn={txn:%Y-%m-%d %H:%M:%S} creation={cre:%Y-%m-%d %H:%M:%S} gap_s={gap:.0f}",
        }
    # Oluşturma, işlemden makul süre sonra (aynı gün/kısa süre) -> TUTARLI (olumlu bilgi)
    return {
        "code": "SEQ_CREATION_CONSISTENT", "severity": "info", "weight": 0,
        "tr": f"SIRA NO işlem anı ({txn:%d.%m.%Y %H:%M:%S}) ile belgenin DÜZENLENME zamanı "
              f"({cre:%d.%m.%Y %H:%M:%S}) tutarlı: belge işlemden sonra üretilmiş (fark "
              f"{gap/60:.0f} dk). Bu, tarih/saat alanlarının oynanmadığını destekleyen olumlu bir işarettir.",
        "en": f"SIRA NO transaction time ({txn:%Y-%m-%d %H:%M:%S}) is consistent with document creation "
              f"({cre:%Y-%m-%d %H:%M:%S}); created after the transaction (+{gap/60:.0f} min). Positive signal.",
        "detail": f"txn={txn:%Y-%m-%d %H:%M:%S} creation={cre:%Y-%m-%d %H:%M:%S} gap_s={gap:.0f}",
    }


# ---------------------------------------------------------------------------
#  TARİH MANTIK ZİNCİRİ (içerik bazlı — fotoğrafta da çalışır)
#  Saldırgan tarih alanlarını değiştirdiğinde mantık kırılır: dekont işlemden önce
#  üretilemez, hiçbir tarih gelecekte olamaz, valör işlemden önce olamaz.
# ---------------------------------------------------------------------------
_DT_RE = re.compile(r"(\d{2})[.\-/](\d{2})[.\-/](\d{4})(?:[ T]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?")


def _parse_dt(s: str):
    if not s:
        return None
    m = _DT_RE.search(s)
    if not m:
        return None
    d, mo, y, h, mi, se = m.groups()
    try:
        return _dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(se or 0))
    except Exception:
        return None


def _label_date(text: str, labels: list[str]):
    n = text or ""
    for lab in labels:
        m = re.search(re.escape(lab) + r"[^\n:]*[:：]?\s*" + _DT_RE.pattern, n)
        if m:
            return _parse_dt(m.group(0))
    return None


def check_date_chain(text: str, txn_date_str: str, value_date_str: str, now=None) -> list[dict]:
    """Tarih alanları arasındaki mantık tutarlılığı. Liste döndürür (0+ bulgu)."""
    out = []
    now = now or _dt.datetime.now()
    txn = _parse_dt(txn_date_str) or _label_date(text, ["İşlem Zam", "İşlem Tarihi", "ISLEM TARIHI", "İŞLEM TARİHİ"])
    dek = _label_date(text, ["Dekont Tarihi", "Düzenlenme Tarihi", "DÜZENLENME"])
    val = _parse_dt(value_date_str) or _label_date(text, ["Valör", "Valor", "VALÖR"])

    # 1) Gelecek tarih: hiçbir işlem/dekont tarihi bugünden ileri olamaz (1 gün tolerans)
    fut = now + _dt.timedelta(days=1)
    for nm, dtv in (("işlem", txn), ("dekont", dek)):
        if dtv and dtv > fut:
            out.append({
                "code": "DATE_IN_FUTURE", "severity": "critical", "weight": 40,
                "tr": f"TARİH ÇELİŞKİSİ: {nm} tarihi ({dtv:%d.%m.%Y %H:%M}) GELECEKTE — bir dekont henüz "
                      f"gerçekleşmemiş bir işlemi gösteremez. Tarih elle değiştirilmiş (ileri tarihleme).",
                "en": f"DATE CONFLICT: the {nm} date ({dtv:%d.%m.%Y %H:%M}) is in the FUTURE — a receipt cannot "
                      f"show a transaction that has not happened yet. The date was altered (forward-dating).",
                "detail": f"{nm}={dtv.isoformat()} now={now.isoformat()}"})
            break

    # 2) Dekont tarihi işlem tarihinden ÖNCE olamaz (dekont işlemle birlikte/sonra üretilir)
    if txn and dek and dek < txn - _dt.timedelta(minutes=1):
        out.append({
            "code": "RECEIPT_BEFORE_TXN", "severity": "critical", "weight": 40,
            "tr": f"TARİH ÇELİŞKİSİ: dekont tarihi ({dek:%d.%m.%Y %H:%M}) işlem tarihinden "
                  f"({txn:%d.%m.%Y %H:%M}) ÖNCE. Dekont, işlem gerçekleşmeden üretilemez — tarih alanlarından "
                  f"biri değiştirilmiş.",
            "en": f"DATE CONFLICT: receipt date ({dek:%d.%m.%Y %H:%M}) precedes the transaction date "
                  f"({txn:%d.%m.%Y %H:%M}). A receipt cannot be issued before the transaction.",
            "detail": f"dekont={dek.isoformat()} islem={txn.isoformat()}"})

    # 3) Valör işlem tarihinden ÖNCE olamaz (aynı gün ya da sonrası; 1 gün tolerans)
    if txn and val and val.date() < (txn - _dt.timedelta(days=1)).date():
        out.append({
            "code": "VALUE_DATE_ANOMALY", "severity": "high", "weight": 18,
            "tr": f"TARİH ANOMALİSİ: valör tarihi ({val:%d.%m.%Y}) işlem tarihinden ({txn:%d.%m.%Y}) önce. "
                  f"Valör normalde işlem günü ya da sonrasıdır.",
            "en": f"DATE ANOMALY: value date ({val:%d.%m.%Y}) is before the transaction date ({txn:%d.%m.%Y}).",
            "detail": f"valor={val.isoformat()} islem={txn.isoformat()}"})
    return out


# ---------------------------------------------------------------------------
#  GÖRÜNTÜ EDİTÖRÜ / İÇERİK-KİMLİK İMZASI (fotoğraf dekontlar için anında bayrak)
#  Bir "dekont fotoğrafı"nın EXIF/yazılım imzası bir masaüstü görüntü editörünü
#  gösteriyorsa (Photoshop, GIMP, Photopea...), belge düzenlenmiş demektir.
# ---------------------------------------------------------------------------
_HEAVY_EDITORS = ("photoshop", "gimp", "photopea", "pixlr", "affinity", "coreldraw",
                  "paint.net", "krita", "inkscape", "illustrator", "lightroom",
                  # TASARIM/MOCKUP araçları: bir banka dekontu fotoğrafı/ekran görüntüsü bu
                  # araçlardan ASLA geçmez — dekont bunlarda "derlenmişse" neredeyse kesin sahtedir.
                  "canva", "figma", "sketch", "pixelmator", "adobe express", "adobe xd",
                  "indesign", "publisher", "microsoft designer", "adobe firefly", "picsart")


def check_image_editor(exif_software: str, edit_hits, c2pa_present: bool) -> dict | None:
    sw = (exif_software or "").lower()
    hits = [h for h in (edit_hits or [])]
    hit_editor = next((e for e in _HEAVY_EDITORS if e in sw), None)
    if hit_editor:
        return {
            "code": "IMAGE_EDITOR_SOFTWARE", "severity": "critical", "weight": 38,
            "tr": f"GÖRÜNTÜ EDİTÖRÜ İMZASI: dosyanın meta verisi bir masaüstü görüntü düzenleyicisiyle "
                  f"({hit_editor}) işlendiğini gösteriyor. Gerçek bir banka dekontu (ekran görüntüsü/fotoğraf) "
                  f"böyle bir editörden geçmez — belge düzenlenmiş, güçlü tahrifat işareti.",
            "en": f"IMAGE EDITOR SIGNATURE: metadata shows the file was processed by a desktop image editor "
                  f"({hit_editor}). A genuine bank receipt would not pass through such software — strong tampering signal.",
            "detail": f"software={exif_software}"}
    if hits:
        return {
            "code": "IMAGE_EDIT_SIGNATURE", "severity": "high", "weight": 20,
            "tr": f"DÜZENLEME İMZASI: dosyada görüntü düzenleme aracı izleri bulundu ({', '.join(hits)}). "
                  f"Dekont üzerinde oynama yapılmış olabilir.",
            "en": f"EDIT SIGNATURE: image-editing tool traces found ({', '.join(hits)}).",
            "detail": f"hits={hits}"}
    return None


def check_identity(id_str: str, label: str = "kimlik") -> dict | None:
    """TCKN/VKN sağlaması: maskeli değilse ve kontrol basamağı tutmuyorsa tahrifat sinyali.
    TC Kimlik/Vergi No matematiksel bir sağlama taşır; uydurulan/değiştirilen numara tutmaz."""
    import banks as _b
    v = _b.id_valid(id_str)
    if v is False:
        d = re.sub(r"\D", "", id_str or "")
        kind = "TC Kimlik No" if len(d) == 11 else "Vergi Kimlik No"
        return {
            "code": "ID_CHECKSUM_INVALID", "severity": "high", "weight": 30,
            "tr": f"GEÇERSİZ {kind} ({label}): ‘{id_str}’ resmi kontrol basamağı sağlamasını GEÇEMİYOR. "
                  f"Gerçek bir {kind} matematiksel sağlama taşır; geçersiz numara, alanın uydurulduğunu ya da "
                  f"elle değiştirildiğini gösterir.",
            "en": f"INVALID {('National ID' if len(d)==11 else 'Tax ID')} ({label}): '{id_str}' fails the official "
                  f"checksum. A genuine ID carries a valid check digit; an invalid one indicates it was fabricated "
                  f"or altered.",
            "detail": f"{label}_id={id_str}"}
    return None


def check_self_transfer(sender_iban: str, receiver_iban: str) -> dict | None:
    """Gönderici IBAN = Alıcı IBAN ise: aynı hesaba transfer anlamsızdır. (Yalnızca güvenilir
    çıkarımda — dijital PDF — çağrılmalı; fotoğrafta OCR aynı IBAN'ı iki alana yazabilir.)"""
    import banks as _b
    s = _b.normalize_iban(sender_iban)
    r = _b.normalize_iban(receiver_iban)
    if s and r and s == r and _b.iban_valid(s) is not False:
        return {
            "code": "SELF_TRANSFER", "severity": "critical", "weight": 40,
            "tr": f"ANLAMSIZ İŞLEM: gönderici ve alıcı IBAN AYNI ({s}). Bir hesaptan yine kendisine transfer "
                  f"yapılamaz; taraf alanlarından biri (IBAN/isim) sonradan değiştirilmiş — güçlü tahrifat işareti.",
            "en": f"NONSENSICAL TRANSFER: sender and receiver IBANs are identical ({s}). One cannot transfer to the "
                  f"same account; a party field (IBAN/name) was altered — strong tampering signal.",
            "detail": f"iban={s}"}
    return None

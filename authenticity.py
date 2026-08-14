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

import re
import io
import datetime as _dt


# --- Banka görünen adı -> iç anahtar ---
_BANK_KEY = {
    "enpara.com (qnb)": "enpara",
    "t.c. ziraat bankası": "ziraat",
    "yapı ve kredi bankası": "yapikredi",
    "türkiye iş bankası": "isbank",
    "vakıfbank": "vakif",
    "garanti bbva": "garanti",
    "akbank t.a.ş.": "akbank",
    "ing bank a.ş.": "ing",
}


def bank_key(bank_display: str) -> str:
    return _BANK_KEY.get((bank_display or "").strip().lower(), "")


# --- (2) Bankanın GERÇEK dekont üretim imzası (producer alt-dizeleri, küçük harf) ---
# Not: Bu liste doğrulanmış örneklere dayanır; bir banka birden çok geçerli kütüphane
# kullanabilir. Eşleşme YOKSA belge bankanın sisteminden çıkmamış demektir.
EXPECTED_PRODUCERS = {
    "enpara":    ["itext", "1t3xt"],          # Enpara -> iText (6 doğrulanmış örnek)
    "ziraat":    ["skia", "chromium"],        # Ziraat mobil -> tarayıcı motoru (Skia)
    "yapikredi": ["aspose"],                  # Yapı Kredi -> Aspose.Words
    "akbank":    ["openpdf", "itext"],        # Akbank -> OpenPDF (iText türevi)
    "ing":       ["evopdf", "html to pdf"],   # ING -> EvoPdf HTML-to-PDF
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
    "ziraat":    {"style": "seq"},                              # Skia (sıralı önek NORMAL)
    "yapikredi": {"style": "random"},                           # Aspose: Calibri + CourierNew
    "akbank":    {"style": "random"},                           # OpenPDF: ArialNarrow
    "ing":       {"style": "none"},                             # gömülü font yok
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
    # Tarih taşıyan aday numaralar (banka kuralı varsa onu, yoksa genel)
    srcs = RECEIPT_DATE_SOURCE.get(bkey)
    field_vals = {"document_no": document_no, "ref_no": ref_no, "seq_number": seq_number}
    if srcs:
        cands = [(s, field_vals.get(s, "")) for s in srcs]
    else:
        cands = [("document_no", document_no), ("seq_number", seq_number)]
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

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

# Belgeyi yeniden basmak/düzenlemek için kullanılan tipik tarayıcı/editör üreticileri.
# Bankanın imzasıyla eşleşmeyen üretici BUNLARDAN biriyse sinyal daha güçlüdür.
_RERENDER_PRODUCERS = ["pdfium", "skia", "chromium", "quartz", "cairo", "microsoft: print",
                       "acrobat", "ghostscript", "wkhtmltopdf", "print to pdf"]


def check_producer(bkey: str, producer: str) -> dict | None:
    """Belgenin üreticisini bankanın gerçek imzasıyla karşılaştırır.
    Eşleşmezse bulgu döndürür (bankanın sisteminden çıkmamış -> olası sahte)."""
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
    tail = (" Belge bir tarayıcı/editör (yeniden basım) aracıyla üretilmiş görünüyor."
            if rerender else "")
    return {
        "code": "PRODUCER_MISMATCH", "severity": "high", "weight": 30, "rerender": rerender,
        "tr": f"ÜRETİCİ UYUŞMAZLIĞI: Bu bankanın (‘{bkey}’) gerçek dekontları ‘{exp_txt}’ ile üretilir; "
              f"ancak bu belgenin üreticisi ‘{producer}’. Belge bankanın sisteminden çıkmamış; "
              f"büyük olasılıkla düzenlenip yeniden dışa aktarılmış (olası SAHTE)." + tail,
        "en": f"PRODUCER MISMATCH: genuine receipts of this bank ('{bkey}') are produced with '{exp_txt}', "
              f"but this file's producer is '{producer}'. The document did not come from the bank's system; "
              f"likely edited and re-exported (possible forgery)." + tail,
        "detail": f"producer={producer} expected={exp}",
    }


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

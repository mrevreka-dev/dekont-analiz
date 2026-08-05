"""
Hesap hareketi (hesap özeti) analizi / Bank account statement analysis.

Dekonttan FARKLI bir belge türüdür: tek bir işlem değil, bir DÖNEME ait çok sayıda
işlem satırı ve yürüyen BAKİYE içerir. Bu modül:

  1) Belgenin bir hesap hareketi olup olmadığını tespit eder (dekont değil).
  2) Hesap sahibi, IBAN, hesap tipi, dönem, seri/sıra no gibi alanları çıkarır.
  3) EN ÖNEMLİ ADLİ KONTROL — BAKİYE ZİNCİRİ SÜREKLİLİĞİ:
     Her satırda  bakiye(i) = bakiye(i-1) ± işlem tutarı  olmalıdır. Bir işlem tutarı
     ya da bakiye elle değiştirilirse (veya satır eklenip çıkarılırsa) zincir KIRILIR.
     Bu, hesap hareketlerinde içerik oynamasının kesin (matematiksel) kanıtıdır.

Not: Yapısal PDF adli analizleri (revizyon, zaman, üstveri, editör tespiti) her PDF için
zaten çalışır; bu modül hesap-hareketine ÖZGÜ katmanı ekler.
"""
from __future__ import annotations

import re

_AMT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
_DATE = r"\d{2}[./]\d{2}[./]\d{4}"

_KEYWORDS = [
    "hesap hareket", "hesap ozeti", "hesap özeti", "baslangic tarihi", "başlangıç tarihi",
    "bitis tarihi", "bitiş tarihi", "hareket tipi", "bakiye", "islem tutari", "işlem tutarı",
    "gelen transfer", "giden transfer", "acilis bakiye", "kapanis bakiye", "devir",
]


def _num(s: str) -> float:
    return float(s.replace(".", "").replace(",", "."))


def statement_score(text: str) -> float:
    """0-1: metnin bir hesap hareketi/özeti olma olasılığı."""
    if not text:
        return 0.0
    low = text.lower().replace("İ", "i").replace("ı", "i")
    kw = sum(1 for k in _KEYWORDS if k in low)
    score = min(kw, 6) / 6 * 0.6
    # yürüyen bakiye sütunu göstergesi: aynı satırda iki tutar (tutar + bakiye) + tarih
    tx = parse_transactions(text)
    if len(tx) >= 3:
        score += 0.4
    elif len(tx) >= 1:
        score += 0.2
    return round(min(score, 1.0), 2)


def parse_transactions(text: str) -> list[dict]:
    """Hareket satırlarını (tarih, işlem tutarı, yürüyen bakiye) olarak ayrıştırır.
    Bir satır tarih VE en az iki tutar içeriyorsa; son iki tutar (işlem tutarı, bakiye)."""
    rows = []
    for line in (text or "").splitlines():
        dm = re.search(_DATE, line)
        if not dm:
            continue
        amts = re.findall(_AMT, line)
        if len(amts) < 2:
            continue
        try:
            tutar = _num(amts[-2]); bakiye = _num(amts[-1])
        except Exception:
            continue
        rows.append({"tarih": dm.group(0), "tutar": tutar, "bakiye": bakiye,
                     "satir": re.sub(r"\s{2,}", " ", line.strip())[:180]})
    return rows


def check_balance_continuity(text: str) -> dict:
    """Yürüyen bakiye sürekliliğini doğrular (belge sırası: en yeni en üstte).
    İlişki:  bakiye(i) - tutar(i) == bakiye(i+1)  (bir sonraki/eski satırın bakiyesi).
    Kırılma varsa -> içerik oynaması (matematiksel kanıt)."""
    rows = parse_transactions(text)
    out = {"checked": len(rows), "consistent": None, "breaks": [],
           "opening": None, "closing": None, "net": None}
    if len(rows) < 2:
        return out
    out["closing"] = rows[0]["bakiye"]            # en üstteki = en yeni bakiye
    out["opening"] = rows[-1]["bakiye"] - rows[-1]["tutar"]  # en eski işlemden önceki bakiye
    breaks = []
    for i in range(len(rows) - 1):
        beklenen = round(rows[i]["bakiye"] - rows[i]["tutar"], 2)
        gercek = round(rows[i + 1]["bakiye"], 2)
        if abs(beklenen - gercek) > 0.01:
            breaks.append({
                "satir_no": i + 1,
                "tarih": rows[i]["tarih"],
                "tutar": rows[i]["tutar"],
                "bakiye": rows[i]["bakiye"],
                "beklenen_onceki_bakiye": beklenen,
                "gercek_onceki_bakiye": gercek,
                "fark": round(beklenen - gercek, 2),
                "satir": rows[i]["satir"],
            })
    out["breaks"] = breaks
    out["consistent"] = (len(breaks) == 0)
    # net değişim (kapanış - açılış), tutarların toplamıyla uyuşmalı
    try:
        out["net"] = round(out["closing"] - out["opening"], 2)
    except Exception:
        out["net"] = None
    return out


_FIELD_PATTERNS = {
    "ad_soyad": r"[Aa]d\s*soyad\s*[:：]\s*([^\n]+?)(?:\s{2,}|Başlangıç|Baslangic|$)",
    "hesap_tipi": r"[Hh]esap\s*tipi\s*[:：]\s*([^\n]+?)(?:\s{2,}|İşlem|Islem|$)",
    "donem_baslangic": r"[Bb]aşlangıç\s*tarihi\s*[:：]\s*(\d{4}\d{2}\d{2}|\d{2}[./]\d{2}[./]\d{4})",
    "donem_bitis": r"[Bb]itiş\s*tarihi\s*[:：]\s*(\d{4}\d{2}\d{2}|\d{2}[./]\d{2}[./]\d{4})",
    "seri_sira_no": r"[Ss]eri\s*/?\s*[Ss]ıra\s*[Nn]o\s*[:：]\s*([^\n]+?)(?:\s{2,}|$)",
}
_IBAN_RE = re.compile(r"TR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}", re.I)


def _fmt_period(s: str) -> str:
    s = (s or "").strip()
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    return s


def extract_statement_fields(text: str) -> dict:
    f = {"ad_soyad": "", "iban": "", "hesap_tipi": "", "donem_baslangic": "",
         "donem_bitis": "", "seri_sira_no": ""}
    for key, pat in _FIELD_PATTERNS.items():
        m = re.search(pat, text or "")
        if m:
            val = m.group(1).strip()
            if key in ("donem_baslangic", "donem_bitis"):
                val = _fmt_period(val)
            f[key] = val
    im = _IBAN_RE.search(text or "")
    if im:
        f["iban"] = re.sub(r"\s+", "", im.group(0)).upper()
    return f


def analyze(text: str) -> dict:
    """Hesap hareketi analizini tek çatı altında döndürür."""
    score = statement_score(text)
    fields = extract_statement_fields(text)
    balance = check_balance_continuity(text)
    tx = parse_transactions(text)
    return {
        "is_statement": score >= 0.5,
        "score": score,
        "fields": fields,
        "islem_sayisi": len(tx),
        "balance": balance,
    }

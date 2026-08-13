"""
Tarih/saat derin analizi / Deep timestamp analysis.

Bir banka dekontu, işlemin gerçekleştiği anda üretilir. Dolayısıyla:
  - PDF oluşturma zamanı ≈ dekont üzerindeki işlem zamanı olmalıdır,
  - PDF, işlemden ÖNCE üretilmiş olamaz (bu imkânsızdır — geriye tarihleme),
  - Oluşturmadan sonra değiştirilme (ModDate ileri) sonradan oynama işaretidir,
  - İşlemden çok sonra üretilen dosya (ör. saatler/günler sonra) yeniden
    oluşturma / fotoğraflama / tahrifat riskidir.

İçerik tarihi Türkiye yerel saatidir (UTC+3); PDF metadata tarihleri UTC olarak
saklanır. Karşılaştırma UTC'de yapılır.
"""
from __future__ import annotations

import re
import datetime as _dt

TR_OFFSET = _dt.timedelta(hours=3)   # Türkiye UTC+3 (yıl boyu sabit)


def parse_content_datetime(s: str):
    """'25.07.2026 04:21:24' / '21/07/2026' -> (datetime, has_time)."""
    if not s:
        return None, False
    s = s.strip()
    m = re.search(r"(\d{2})[./](\d{2})[./](\d{4})(?:[ T\-]+(\d{2}):(\d{2})(?::(\d{2}))?)?", s)
    if not m:
        return None, False
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4)) if m.group(4) else 0
    mi = int(m.group(5)) if m.group(5) else 0
    ss = int(m.group(6)) if m.group(6) else 0
    has_time = m.group(4) is not None
    try:
        return _dt.datetime(y, mo, d, hh, mi, ss), has_time
    except ValueError:
        return None, False


def _fmt(dt, tz_add=None):
    if dt is None:
        return ""
    if tz_add:
        dt = dt + tz_add
    return dt.strftime("%d.%m.%Y %H:%M:%S")


def _human_delta(sec: float) -> str:
    sec = abs(int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d} gün")
    if h: parts.append(f"{h} saat")
    if m: parts.append(f"{m} dk")
    if s and not d and not h: parts.append(f"{s} sn")
    return " ".join(parts) or "0 sn"


def analyze_timing(creation_dt, mod_dt, transaction_date_str, is_aem: bool,
                   suppress_late_generation: bool = False) -> dict:
    """
    Zaman analizi. Döndürür:
      timeline: [{label, value}], gaps, findings:[{code,severity,weight,tr,en,detail}]
    Metadata (creation_dt/mod_dt) UTC; işlem tarihi yerel (UTC+3) kabul edilir.
    """
    out = {"timeline": [], "gaps": {}, "findings": [],
           "transaction_local": "", "creation_local": "", "mod_local": ""}
    txn, has_time = parse_content_datetime(transaction_date_str)

    # Yerel gösterim (metadata UTC -> +3 yerel)
    out["transaction_local"] = _fmt(txn) if txn else ""
    out["creation_local"] = _fmt(creation_dt, TR_OFFSET) if creation_dt else ""
    out["mod_local"] = _fmt(mod_dt, TR_OFFSET) if mod_dt else ""

    out["timeline"] = [
        {"label": "İşlem zamanı (dekont içeriği)", "value": out["transaction_local"] or "—"},
        {"label": "PDF oluşturma", "value": out["creation_local"] or "—"},
        {"label": "PDF değiştirme", "value": out["mod_local"] or "—"},
    ]

    # "Üretim zamanı": AEM'de oluşturma tarihi şablon tarihidir -> değiştirmeyi kullan
    gen = None
    if creation_dt and creation_dt.year >= 2020 and not is_aem:
        gen = creation_dt
    elif mod_dt:
        gen = mod_dt
    elif creation_dt:
        gen = creation_dt

    # İşlemi UTC'ye çevir (yerel - 3s)
    txn_utc = (txn - TR_OFFSET) if txn else None

    F = out["findings"]

    # 1) İşlem ↔ üretim zamanı
    if gen and txn_utc:
        delta = (gen - txn_utc).total_seconds()   # + : üretim işlemden sonra
        out["gaps"]["txn_to_generation_sec"] = int(delta)
        if delta < -300:
            F.append({"code": "TIME_FILE_BEFORE_TXN", "severity": "critical", "weight": 40,
                      "tr": f"PDF, dekonttaki işlem zamanından ÖNCE üretilmiş görünüyor "
                            f"(dosya ~{_human_delta(delta)} önce). Bir dekont, işlem gerçekleşmeden üretilemez — "
                            f"bu, tarih/saatle oynandığına (geriye tarihleme) güçlü işarettir.",
                      "en": f"The PDF appears to be generated BEFORE the receipt's transaction time "
                            f"(~{_human_delta(delta)} earlier) — impossible; strong backdating signal.",
                      "detail": f"işlem(UTC)={txn_utc} üretim(UTC)={gen}"})
        elif delta <= 900:
            F.append({"code": "TIME_CONSISTENT", "severity": "info", "weight": -6,
                      "tr": f"PDF üretim zamanı, işlem zamanıyla tutarlı (fark ~{_human_delta(delta)}). "
                            f"Dekont işlem anında üretilmiş görünüyor (doğrulayıcı).",
                      "en": f"PDF generation time matches the transaction time (~{_human_delta(delta)}) — corroborating.",
                      "detail": ""})
        elif delta > 6 * 3600 and not suppress_late_generation:
            # Hesap özetinde dönemden SONRA üretim normaldir; bu bulgu yalnızca dekontlarda anlamlı.
            sev = "high" if delta > 24 * 3600 else "medium"
            F.append({"code": "TIME_LATE_GENERATION", "severity": sev, "weight": 18 if sev == "high" else 10,
                      "tr": f"PDF, işlem zamanından çok sonra üretilmiş (~{_human_delta(delta)} sonra). "
                            f"Anlık üretilen bir dekontta bu beklenmez; belge sonradan yeniden oluşturulmuş, "
                            f"fotoğraflanmış veya üzerinde işlem yapılmış olabilir.",
                      "en": f"PDF was generated long after the transaction (~{_human_delta(delta)} later) — "
                            f"re-generation/photograph/tamper risk.",
                      "detail": f"işlem(UTC)={txn_utc} üretim(UTC)={gen}"})

    # 2) Oluşturma ↔ değiştirme
    if creation_dt and mod_dt and creation_dt.year >= 2020:
        dmod = (mod_dt - creation_dt).total_seconds()
        out["gaps"]["creation_to_mod_sec"] = int(dmod)
        if dmod > 300:
            sev = "high" if dmod > 3600 else "medium"
            F.append({"code": "TIME_MODIFIED_AFTER_CREATE", "severity": sev, "weight": 16 if sev == "high" else 9,
                      "tr": f"PDF, oluşturulduktan ~{_human_delta(dmod)} SONRA değiştirilmiş. "
                            f"Anlık üretilen dekontlarda oluşturma ve değiştirme zamanı aynıdır; bu fark, belgenin "
                            f"üretimden sonra açılıp yeniden kaydedildiğini (olası oynama) gösterir.",
                      "en": f"PDF was modified ~{_human_delta(dmod)} AFTER creation — opened and re-saved after generation.",
                      "detail": f"oluşturma={creation_dt} değiştirme={mod_dt}"})
        elif dmod < -60:
            F.append({"code": "TIME_MOD_BEFORE_CREATE", "severity": "high", "weight": 20,
                      "tr": "Değiştirilme tarihi oluşturulma tarihinden ÖNCE — metadata ile oynanmış olabilir.",
                      "en": "Modification date is before creation date — metadata may be tampered.",
                      "detail": f"oluşturma={creation_dt} değiştirme={mod_dt}"})

    return out

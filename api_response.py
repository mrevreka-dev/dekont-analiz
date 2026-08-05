"""
Web servisi cevabı / API response builder.

İç analiz raporunu (analyze_document çıktısı), TÜKETİCİNİN dekont durumunu kolayca
değerlendirebileceği TEMİZ ve amaç odaklı bir JSON yapısına dönüştürür.

Zorunlu alanlar: dekont mu, değerleme skoru + açıklaması, evrakta oynama olup olmadığı,
işlem/dekont zamanı tutarlılığı, gönderici/alıcı ad-IBAN, tutar, işlem tarihi/saati,
işlem numarası vb. Ayrıntılı iç rapor `detay` altında korunur (isteğe bağlı kullanım).
"""
from __future__ import annotations

import re

_TR_TUR = {"pdf": "pdf", "image": "gorsel"}


def _split_date_time(s: str) -> tuple[str, str]:
    """'24.07.2026 02:21:10' -> ('24.07.2026','02:21:10'). Saat yoksa ('tarih','')."""
    if not s:
        return "", ""
    dm = re.search(r"\d{2}[./]\d{2}[./]\d{4}", s)
    tm = re.search(r"\d{2}:\d{2}(?::\d{2})?", s)
    return (dm.group(0) if dm else s.strip()), (tm.group(0) if tm else "")


def _oynama(state: str) -> str:
    # içerik bütünlüğü verdict'i -> evrakta oynama cevabı
    return {"true": "yok", "false": "var", "neutral": "belirsiz"}.get(state, "belirsiz")


def build_summary(report: dict) -> dict:
    ex = report.get("extracted", {})
    s = ex.get("sender", {})
    r = ex.get("receiver", {})
    amt = ex.get("amount", {})
    tx = ex.get("transaction", {})
    cls = report.get("classification", {})
    score = report.get("score", {})
    vd = report.get("verdicts", {}) or {}
    checks = {c["key"]: c for c in vd.get("checks", [])}
    overall = vd.get("overall", {})

    islem_tarihi, islem_saati = _split_date_time(tx.get("date", ""))

    # Bir işlem/dekont içeriği tespit edildi mi (tutar veya taraf bilgisi var mı)
    islem_tespit = bool(amt.get("value") is not None or s.get("name") or r.get("name")
                        or r.get("iban") or tx.get("sequence_number"))

    def vstate(key):
        c = checks.get(key)
        return c["state"] if c else "neutral"

    summary = {
        "basarili": True,
        "motor_surumu": report.get("engine_version", ""),
        "analiz_zamani": report.get("analyzed_at", ""),

        "dosya": {
            "ad": report.get("file", {}).get("name", ""),
            "boyut_bytes": report.get("file", {}).get("size_bytes", 0),
            "sha256": report.get("file", {}).get("sha256", ""),
            "tur": _TR_TUR.get(cls.get("input_kind", ""), cls.get("input_kind", "")),
        },

        # --- Dekont mu? ---
        "dekont_mu": bool(cls.get("is_receipt", False)),

        # --- Doğrulama modu (belge tipine göre neyin kesin doğrulanabildiği) ---
        "dogrulama_modu": vd.get("mode", ""),
        "dogrulama_modu_aciklama": vd.get("mode_label_tr", ""),

        # --- Değerleme ---
        "degerlendirme": {
            "skor": score.get("authenticity_score"),
            "azami_skor": score.get("max_possible"),
            "risk_seviyesi": score.get("risk_level", ""),
            "guvenilir_mi": overall.get("state", "neutral"),   # true | false | neutral
            "aciklama": score.get("verdict_tr", ""),
            "genel_karar_aciklama": overall.get("label_tr", ""),
        },

        # --- Kesin cevaplar (true / false / neutral) ---
        "kesin_cevaplar": {
            "gecerli_dekont": vstate("valid_receipt"),
            "evrakta_oynama": _oynama(vstate("content_integrity")),   # yok | var | belirsiz
            "zaman_tutarli": vstate("time_consistency"),
            "veri_tutarli": vstate("data_consistency"),
            "numara_celiskisi_yok": vstate("cross_reference"),
        },

        # --- Evrakta işlem tespit edildi mi ---
        "islem_tespit_edildi": islem_tespit,

        # --- Çıkarılan bilgiler ---
        "bilgiler": {
            "banka": ex.get("bank", ""),
            "gonderici_ad_soyad": s.get("name", ""),
            "gonderici_iban": s.get("iban", ""),
            "gonderici_banka": s.get("bank", ""),
            "alici_ad_soyad": r.get("name", ""),
            "alici_iban": r.get("iban", ""),
            "alici_banka": r.get("bank", ""),
            "tutar": amt.get("value"),
            "para_birimi": amt.get("currency", ""),
            "masraf": amt.get("fee"),
            "toplam": amt.get("total"),
            "islem_tarihi": islem_tarihi,
            "islem_saati": islem_saati,
            "islem_numarasi": tx.get("sequence_number", "") or tx.get("document_no", ""),
            "referans_no": tx.get("ref_no", ""),
            "islem_turu": tx.get("type", ""),
            "islem_kanali": tx.get("channel", ""),
            "aciklama": tx.get("description", ""),
        },

        # --- Zaman detayları (işlem ↔ dekont/PDF üretim zamanı) ---
        "zaman": {
            "islem_zamani": report.get("timing", {}).get("transaction_local", ""),
            "dekont_olusturma": report.get("timing", {}).get("creation_local", ""),
            "dekont_degistirme": report.get("timing", {}).get("mod_local", ""),
        },

        # --- Bulgular (özet) ---
        "bulgular": [
            {"kod": f.get("code"), "onem": f.get("severity"), "aciklama": f.get("message")}
            for f in report.get("findings_tr", []) if f.get("weight", 0) > 0
        ],

        # --- Tam ayrıntılı iç rapor (isteğe bağlı) ---
        "detay": report,
    }
    return summary

"""
Kesin doğrulama kararları / Definitive verdicts (true / false / neutral).

Analiz sonuçlarını, belgenin DOĞRULANABİLİRLİĞİNE göre net cevaplara çevirir. Amaç,
"içerikte oynama var mı?", "zaman tutarlı mı?", "dekont sahte mi?" gibi sorulara
KESİN (evet/hayır) ya da dürüstçe NÖTR (analiz edilemedi) cevap vermektir.

Üç mod:
  - "digital"    : Gerçek dijital PDF/XML. İçerik ve yapı GERÇEKTEN doğrulanabilir.
                   İçerik oynaması ve zaman tutarlılığı için KESİN true/false verilir.
  - "pdf_photo"  : PDF ama içeriği bir fotoğraf/tarama. PDF yapısı/üstverisi (revizyon,
                   zaman) doğrulanabilir; ancak fotoğrafın PİKSEL içeriğindeki oynama
                   doğrulanamaz -> o soru NÖTR kalır (revizyon değişikliği varsa FALSE).
  - "photo"      : Doğrudan fotoğraf (JPG/PNG). Yapısal doğrulama yoktur; içerik ve zaman
                   soruları NÖTR kalır. (Gerçek dekontun aksine piksel oynaması kanıtlanamaz.)

Her karar: state ∈ {"true","false","neutral"}
  true    = olumlu/temiz (soru olumlu yanıtlanıyor)
  false   = olumsuz (tahrifat/uyumsuzluk KESİN tespit edildi)
  neutral = analiz edilemedi (belge tipi bu doğrulamaya elvermiyor)
"""
from __future__ import annotations

TRUE, FALSE, NEUTRAL = "true", "false", "neutral"

# İçerik oynamasını KESİN kanıtlayan bulgular (revizyonlar arası fark)
_CONTENT_TAMPER = {"REV_CONTENT_CHANGED", "REV_AMOUNT_CHANGED", "REV_FIELD_CHANGED"}
# Zaman tutarsızlığını gösteren bulgular
_TIME_BAD = {"TIME_FILE_BEFORE_TXN", "TIME_LATE_GENERATION", "TIME_MODIFIED_AFTER_CREATE"}

_MODE_TR = {
    "digital": "Gerçek dijital PDF (içerik ve yapı doğrulanabilir)",
    "pdf_photo": "PDF içinde fotoğraf/tarama (piksel içeriği doğrulanamaz)",
    "photo": "Doğrudan fotoğraf (yapısal doğrulama yapılamaz)",
}
_MODE_EN = {
    "digital": "Genuine digital PDF (content and structure verifiable)",
    "pdf_photo": "Photo/scan inside a PDF (pixel content not verifiable)",
    "photo": "Direct photo (no structural verification possible)",
}

_NEUTRAL_TR = "Belge tipi bu doğrulamaya elvermiyor (yalnızca gerçek dijital PDF'te kesin analiz edilebilir); bu yüzden nötr bırakıldı."
_NEUTRAL_EN = "The document type does not allow this verification (only a genuine digital PDF can be checked definitively); left neutral."


def _mode(doc_type: str, input_kind: str) -> str:
    if input_kind == "image":
        return "photo"
    if doc_type in ("image_only", "scanned"):
        return "pdf_photo"
    return "digital"


def compute_verdicts(*, doc_type: str, input_kind: str, codes: set, cons: dict,
                     has_pdf_dates: bool, txn_date: str, seq: str,
                     db_checked: bool, db_count: int, is_receipt: bool) -> dict:
    mode = _mode(doc_type, input_kind)
    checks = []

    def add(key, q_tr, q_en, state, r_tr, r_en):
        checks.append({"key": key, "question_tr": q_tr, "question_en": q_en,
                       "state": state, "reason_tr": r_tr, "reason_en": r_en})

    # 1) Geçerli bir banka dekontu mu? (her zaman kesin)
    if is_receipt:
        add("valid_receipt", "Geçerli bir banka dekontu mu?", "Is it a valid bank receipt?",
            TRUE, "Dekont içeriği (banka, IBAN, tutar, taraf bilgileri) tespit edildi.",
            "Receipt content (bank, IBAN, amount, party info) was detected.")
    else:
        add("valid_receipt", "Geçerli bir banka dekontu mu?", "Is it a valid bank receipt?",
            FALSE, "Görselde/dosyada banka dekontu içeriği tespit EDİLEMEDİ.",
            "No bank-receipt content could be detected in the file/image.")

    # 2) İçerik değiştirilmemiş mi? (oynama yok mu)
    if codes & _CONTENT_TAMPER:
        add("content_integrity", "İçerik değiştirilmemiş mi (oynama yok mu)?",
            "Is the content unaltered (no tampering)?", FALSE,
            "PDF revizyonları arasında içerik/tutar DEĞİŞTİRİLMİŞ — kesin oynama tespiti.",
            "Content/amount was CHANGED between PDF revisions — definitive tampering.")
    elif mode == "digital":
        add("content_integrity", "İçerik değiştirilmemiş mi (oynama yok mu)?",
            "Is the content unaltered (no tampering)?", TRUE,
            "Gerçek dijital PDF; revizyon/yapı incelemesinde içerik oynaması bulunmadı.",
            "Genuine digital PDF; no content tampering found in revision/structure checks.")
    else:
        add("content_integrity", "İçerik değiştirilmemiş mi (oynama yok mu)?",
            "Is the content unaltered (no tampering)?", NEUTRAL,
            "Fotoğraf/tarama içeriğinde piksel düzeyinde oynama doğrulanamaz (gerçek PDF gibi "
            "revizyon geçmişi yok). " + _NEUTRAL_TR,
            "Pixel-level edits in a photo/scan cannot be verified (no revision history like a real PDF). "
            + _NEUTRAL_EN)

    # 3) İşlem saati/tarih tutarlı mı?
    if mode == "photo":
        add("time_consistency", "İşlem saati/tarih tutarlı mı?", "Is the transaction time consistent?",
            NEUTRAL, "Doğrudan fotoğrafta güvenilir üretim/oluşturma zamanı yoktur; zaman "
            "kıyaslaması yapılamaz. " + _NEUTRAL_TR,
            "A direct photo has no reliable generation timestamp; time cannot be compared. " + _NEUTRAL_EN)
    elif codes & _TIME_BAD:
        add("time_consistency", "İşlem saati/tarih tutarlı mı?", "Is the transaction time consistent?",
            FALSE, "PDF üretim/değiştirme zamanı ile işlem zamanı UYUMSUZ (geriye tarihleme veya "
            "geç üretim) — zaman tutarsızlığı.",
            "PDF generation/modification time conflicts with the transaction time (backdating or late "
            "generation) — timing inconsistency.")
    elif has_pdf_dates and txn_date:
        add("time_consistency", "İşlem saati/tarih tutarlı mı?", "Is the transaction time consistent?",
            TRUE, "PDF üretim zamanı işlem zamanıyla tutarlı; geriye tarihleme/geç üretim yok.",
            "PDF generation time is consistent with the transaction time; no backdating/late generation.")
    else:
        add("time_consistency", "İşlem saati/tarih tutarlı mı?", "Is the transaction time consistent?",
            NEUTRAL, "Karşılaştırma için güvenilir tarih bilgisi bulunamadı. " + _NEUTRAL_TR,
            "No reliable date information available for comparison. " + _NEUTRAL_EN)

    # 4) Tutar/veri hesapları tutarlı mı? (kaynak bağımsız; alanlar varsa)
    _checks = cons.get("checks", []) if isinstance(cons, dict) else []
    if not _checks:
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            NEUTRAL, "Karşılaştırılacak tutar bilgisi (toplam/masraf) okunamadı.",
            "No amount figures (total/fee) were available to reconcile.")
    elif cons.get("fail_count", 0) > 0:
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            FALSE, "Tutar tutarlılığı SAĞLANMIYOR (Toplam ≠ Tutar + Masraf vb.) — alanlardan biri "
            "elle değiştirilmiş olabilir.",
            "Amounts DO NOT reconcile (Total ≠ Amount + Fee, etc.) — a field may have been altered.")
    else:
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            TRUE, "Tutar hesapları tutarlı (Toplam = Tutar + Masraf).",
            "Amounts reconcile correctly (Total = Amount + Fee).")

    # 5) Numara geçmişiyle çelişki yok mu? (kalıcı veritabanı)
    if "SEQ_DB_DUPLICATE" in codes:
        add("cross_reference", "Numara geçmişiyle çelişki yok mu?", "No conflict with the number history?",
            FALSE, "Bu işlem/sıra numarası daha önce FARKLI bir dekontta da kayıtlı — kopyalanmış/"
            "uydurulmuş numara.",
            "This transaction/sequence number is already recorded on a DIFFERENT receipt — copied/fabricated.")
    elif db_checked and seq and db_count > 0:
        add("cross_reference", "Numara geçmişiyle çelişki yok mu?", "No conflict with the number history?",
            TRUE, "İşlem/sıra numarası geçmiş kayıtlarla karşılaştırıldı; çakışma bulunmadı.",
            "The transaction/sequence number was compared with history; no conflict found.")
    else:
        add("cross_reference", "Numara geçmişiyle çelişki yok mu?", "No conflict with the number history?",
            NEUTRAL, "Karşılaştırılacak numara yok ya da veritabanında henüz geçmiş kayıt yok.",
            "No number to compare, or no prior records in the database yet.")

    # 6) GENEL: Dekont güvenilir mi (sahte değil)?
    states = {c["key"]: c["state"] for c in checks}
    any_false = any(c["state"] == FALSE for c in checks)
    if any_false:
        overall = FALSE
        o_tr = ("Dekont GÜVENİLİR DEĞİL: en az bir doğrulama kesin olarak başarısız oldu "
                "(tahrifat/uyumsuzluk tespit edildi).")
        o_en = ("The receipt is NOT trustworthy: at least one verification definitively failed "
                "(tampering/inconsistency detected).")
    elif states.get("content_integrity") == TRUE:
        overall = TRUE
        o_tr = ("Dekont GÜVENİLİR: gerçek dijital PDF üzerinde yapılan kesin doğrulamalarda "
                "tahrifat/uyumsuzluk bulunmadı.")
        o_en = ("The receipt is TRUSTWORTHY: definitive checks on the genuine digital PDF found "
                "no tampering/inconsistency.")
    else:
        overall = NEUTRAL
        o_tr = ("KESİN KARAR VERİLEMİYOR (nötr): belge bir fotoğraf/tarama olduğundan içerik "
                "oynaması kesin olarak doğrulanamaz. Görünür alanlar okundu ancak orijinallik "
                "yapısal olarak kanıtlanamaz — mümkünse orijinal dijital PDF isteyin.")
        o_en = ("NO DEFINITIVE VERDICT (neutral): the file is a photo/scan, so content tampering "
                "cannot be verified with certainty. Visible fields were read but authenticity cannot "
                "be structurally proven — request the original digital PDF if possible.")

    return {
        "mode": mode,
        "mode_label_tr": _MODE_TR[mode],
        "mode_label_en": _MODE_EN[mode],
        "checks": checks,
        "overall": {"state": overall, "label_tr": o_tr, "label_en": o_en},
    }

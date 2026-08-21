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

# İçerik oynamasını KESİN kanıtlayan bulgular (revizyon farkı, bakiye kırılması, satır silme)
_CONTENT_TAMPER = {"REV_CONTENT_CHANGED", "REV_AMOUNT_CHANGED", "REV_FIELD_CHANGED",
                   "STATEMENT_BALANCE_BREAK", "STATEMENT_ROW_COUNT_MISMATCH", "AMOUNT_MISMATCH",
                   "RECEIPT_NO_DATE_MISMATCH", "PRODUCER_MISMATCH", "BROWSER_RERENDER",
                   "FONT_BROWSER_RERENDER", "FONT_SET_MISMATCH", "INTERNAL_DATE_MISMATCH", "PDFIUM_PRODUCED",
                   "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH", "SENDER_BANK_MISMATCH",
                   "NUMBER_REUSE", "AMOUNT_FONT_ANOMALY", "AI_VISUAL_TAMPER", "FEE_RAIL_MISMATCH", "RAIL_SAMEBANK_MISMATCH",
                   "SAMEBANK_RAIL_CONTRADICTION", "INTERBANK_HAVALE_CONTRADICTION",
                   "DATE_IN_FUTURE", "RECEIPT_BEFORE_TXN", "IMAGE_EDITOR_SOFTWARE",
                   "ID_CHECKSUM_INVALID", "SELF_TRANSFER"}
# Zaman tutarsızlığını gösteren bulgular
_TIME_BAD = {"TIME_FILE_BEFORE_TXN", "TIME_LATE_GENERATION", "TIME_MODIFIED_AFTER_CREATE",
             "TIME_MOD_BEFORE_CREATE", "RECEIPT_NO_DATE_MISMATCH"}

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


def _time_reason(codes: set, timing: dict, is_statement: bool) -> tuple[str, str]:
    """Zaman tutarsızlığı için SOMUT (tarihli) açıklama üretir."""
    t = timing or {}
    created = t.get("creation_local") or "—"
    modified = t.get("mod_local") or "—"
    txn = t.get("transaction_local") or "—"
    islem = "dönem/işlem" if is_statement else "işlem"
    if "RECEIPT_NO_DATE_MISMATCH" in codes:
        tr = ("Fiş/belge numarasının kodladığı (bankaca verilen) tarih ile belge üzerindeki işlem "
              "tarihi UYUŞMUYOR. Fiş numarasındaki tarih değiştirilemez; belge üzerindeki tarih "
              "sonradan değiştirilmiş görünüyor (ileri/geri tarihleme) — güçlü SAHTECİLİK işareti.")
        en = ("The bank-assigned date encoded in the receipt number does NOT match the transaction "
              "date shown on the document — the visible date was altered (forgery signal).")
        return tr, en
    if "TIME_FILE_BEFORE_TXN" in codes:
        tr = (f"PDF {created} tarihinde ÜRETİLMİŞ görünüyor, ancak belge {txn} tarihli {islem} "
              f"içeriyor. Bir dosya, içerdiği işlemlerden ÖNCE oluşturulamaz — bu İMKÂNSIZDIR. "
              f"Tarih/metadata ile oynandığına (geriye tarihleme) güçlü işarettir.")
        en = (f"The PDF appears CREATED on {created}, yet it contains a {txn} transaction. A file "
              f"cannot be created before the transactions it contains — this is IMPOSSIBLE. Strong "
              f"backdating/metadata-tampering signal.")
        return tr, en
    if "TIME_MOD_BEFORE_CREATE" in codes:
        tr = (f"PDF değiştirilme tarihi ({modified}), oluşturulma tarihinden ({created}) ÖNCE — "
              f"metadata ile oynanmış olabilir.")
        en = (f"PDF modification date ({modified}) precedes creation date ({created}) — metadata may be tampered.")
        return tr, en
    if "TIME_MODIFIED_AFTER_CREATE" in codes:
        tr = (f"PDF, oluşturulduktan ({created}) SONRA değiştirilmiş ({modified}). Anlık üretilen "
              f"belgelerde bu iki zaman aynıdır; fark, belgenin üretimden sonra açılıp yeniden "
              f"kaydedildiğini (olası oynama) gösterir.")
        en = (f"PDF was modified ({modified}) after creation ({created}); indicates it was reopened "
              f"and re-saved after generation (possible tampering).")
        return tr, en
    if "TIME_LATE_GENERATION" in codes:
        tr = (f"PDF, {islem} zamanından ({txn}) çok sonra üretilmiş ({created}). Anlık üretilen bir "
              f"belgede beklenmez; sonradan yeniden oluşturma/oynama riski.")
        en = (f"PDF generated ({created}) long after the {txn} transaction — regeneration/tamper risk.")
        return tr, en
    return ("PDF üretim/değiştirme zamanı ile işlem zamanı UYUMSUZ — zaman tutarsızlığı.",
            "PDF generation/modification time conflicts with the transaction time.")


def compute_verdicts(*, doc_type: str, input_kind: str, codes: set, cons: dict,
                     has_pdf_dates: bool, txn_date: str, seq: str,
                     db_checked: bool, db_count: int, is_receipt: bool,
                     doc_kind: str = "dekont", balance_state: str = "neutral",
                     timing: dict = None) -> dict:
    mode = _mode(doc_type, input_kind)
    is_statement = (doc_kind == "hesap_hareketi")
    belge = "hesap hareketi" if is_statement else "dekont"
    checks = []

    def add(key, q_tr, q_en, state, r_tr, r_en):
        checks.append({"key": key, "question_tr": q_tr, "question_en": q_en,
                       "state": state, "reason_tr": r_tr, "reason_en": r_en})

    # 1) Geçerli bir belge mi? (dekont / hesap hareketi)
    if is_statement:
        add("valid_receipt", "Geçerli bir hesap hareketi belgesi mi?",
            "Is it a valid account statement?", TRUE,
            "Hesap hareketi içeriği (hesap sahibi, IBAN, dönem, işlem/bakiye tablosu) tespit edildi.",
            "Account-statement content (holder, IBAN, period, transaction/balance table) was detected.")
    elif is_receipt:
        add("valid_receipt", "Geçerli bir banka dekontu mu?", "Is it a valid bank receipt?",
            TRUE, "Dekont içeriği (banka, IBAN, tutar, taraf bilgileri) tespit edildi.",
            "Receipt content (bank, IBAN, amount, party info) was detected.")
    else:
        add("valid_receipt", "Geçerli bir banka dekontu mu?", "Is it a valid bank receipt?",
            FALSE, "Görselde/dosyada banka dekontu içeriği tespit EDİLEMEDİ.",
            "No bank-receipt content could be detected in the file/image.")

    # 1.5) HESAP HAREKETİ: yürüyen bakiye zinciri tutarlı mı? (matematiksel içerik kontrolü)
    if is_statement:
        if balance_state == FALSE:
            add("balance_chain", "Yürüyen bakiye zinciri tutarlı mı?",
                "Is the running-balance chain consistent?", FALSE,
                "Bakiye zinciri KIRILMIŞ: en az bir satırda bakiye, işlem tutarıyla uyuşmuyor — bir "
                "tutar/bakiye elle değiştirilmiş ya da satır eklenip çıkarılmış olabilir (matematiksel kanıt).",
                "Balance chain BROKEN: on at least one row the balance does not match the amount — an "
                "amount/balance was altered or a row inserted/removed (mathematical proof).")
        elif balance_state == TRUE:
            add("balance_chain", "Yürüyen bakiye zinciri tutarlı mı?",
                "Is the running-balance chain consistent?", TRUE,
                "Her satırda bakiye = önceki bakiye ± işlem tutarı doğrulandı; içerik oynaması yönünde işaret yok.",
                "Verified balance = previous balance ± amount on every row; no sign of tampering.")
        else:
            add("balance_chain", "Yürüyen bakiye zinciri tutarlı mı?",
                "Is the running-balance chain consistent?", NEUTRAL,
                "Yürüyen bakiye sütunu okunamadı ya da yeterli işlem satırı yok.",
                "The running-balance column could not be read or there are too few transaction rows.")

    # 2) İçerik değiştirilmemiş mi? (oynama yok mu)
    if codes & _CONTENT_TAMPER:
        if "RECEIPT_NO_DATE_MISMATCH" in codes:
            _ci_tr = ("Fiş/belge numarasındaki bankaca verilmiş tarih ile belge üzerindeki işlem "
                      "tarihi UYUŞMUYOR — belgenin tarihi sonradan değiştirilmiş (olası SAHTE).")
            _ci_en = ("The bank-assigned date embedded in the receipt number does NOT match the "
                      "transaction date shown — the date was altered (possible forgery).")
        elif codes & {"BROWSER_RERENDER", "FONT_BROWSER_RERENDER"}:
            _ci_tr = ("Belge, bankanın sunucu kütüphanesiyle değil bir TARAYICI ile üretilmiş (font/üretici "
                      "imzası tarayıcı yeniden-basımına işaret ediyor) — bankanın sisteminden çıkmamış, "
                      "olası SAHTE.")
            _ci_en = ("The document was produced by a browser, not the bank's server library (font/producer "
                      "signature indicates a browser re-render) — did not come from the bank; possible forgery.")
        elif "FONT_SET_MISMATCH" in codes:
            _ci_tr = ("Belgedeki font kümesi bankanın orijinal dekont şablonuyla uyuşmuyor — belge orijinal "
                      "olmayabilir (olası SAHTE).")
            _ci_en = ("The font set does not match the bank's original receipt template — possible forgery.")
        elif "INTERNAL_DATE_MISMATCH" in codes:
            _ci_tr = ("Belge içindeki/metadata'daki tarihler birbiriyle çelişiyor — belgenin tarihiyle "
                      "oynanmış olabilir (olası SAHTE).")
            _ci_en = ("Dates inside the document/metadata conflict with each other — possible date tampering.")
        elif "PRODUCER_MISMATCH" in codes:
            _ci_tr = ("Belge, bankanın gerçek dekont üretim kütüphanesiyle üretilmemiş — düzenlenip "
                      "yeniden dışa aktarılmış olabilir (olası SAHTE).")
            _ci_en = ("The document was not produced by the bank's genuine receipt-generation library — "
                      "it may have been edited and re-exported (possible forgery).")
        elif "AMOUNT_MISMATCH" in codes:
            _ci_tr = "İşlem tutarı belgede farklı yerlerde FARKLI yazılmış — tutar oynaması."
            _ci_en = "The transaction amount is written inconsistently across the document — amount tampering."
        else:
            _ci_tr = "PDF revizyonları arasında içerik/tutar DEĞİŞTİRİLMİŞ — kesin oynama tespiti."
            _ci_en = "Content/amount was CHANGED between PDF revisions — definitive tampering."
        add("content_integrity", "İçerik değiştirilmemiş mi (oynama yok mu)?",
            "Is the content unaltered (no tampering)?", FALSE, _ci_tr, _ci_en)
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
        _r_tr, _r_en = _time_reason(codes, timing, is_statement)
        add("time_consistency", "İşlem saati/tarih tutarlı mı?", "Is the transaction time consistent?",
            FALSE, _r_tr, _r_en)
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
    elif cons.get("fail_count", 0) > 0 and mode == "digital":
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            FALSE, "Tutar tutarlılığı SAĞLANMIYOR (Toplam ≠ Tutar + Masraf vb.) — alanlardan biri "
            "elle değiştirilmiş olabilir.",
            "Amounts DO NOT reconcile (Total ≠ Amount + Fee, etc.) — a field may have been altered.")
    elif cons.get("fail_count", 0) > 0:
        # FOTOĞRAF/TARAMA: tutarlar OCR/vision ile pikselden okunur; tek bir rakamın yanlış
        # okunması (ör. 1.234,56 → 1.284,56) 'Toplam ≠ Tutar + Masraf' verir. Bu bir OKUMA
        # HATASI da olabilir, tahrifat da — fotoğrafta kesin AYIRT EDİLEMEZ. Bu yüzden KESİN
        # 'GÜVENİLİR DEĞİL' kararı VERMEZ (nötr kalır, puanı 40'a çakmaz); yalnızca uyarı verilir.
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            NEUTRAL, "Tutar hesapları okunan alanlarla tam uyuşmadı (Toplam ≠ Tutar + Masraf). Ancak "
            "tutarlar fotoğraftan/taramadan OCR ile okunduğundan bu bir OKUMA HATASI da olabilir; "
            "tek başına tahrifat kanıtı sayılmaz. Kesinlik için orijinal dijital PDF isteyin.",
            "Amount figures did not fully reconcile (Total ≠ Amount + Fee), but they were OCR-read from a "
            "photo/scan so this may be a misread, not tampering — not conclusive. Request the original digital PDF.")
    else:
        add("data_consistency", "Tutar/veri hesapları tutarlı mı?", "Do the amount/data figures reconcile?",
            TRUE, "Tutar hesapları tutarlı (Toplam = Tutar + Masraf).",
            "Amounts reconcile correctly (Total = Amount + Fee).")

    # 5) Numara geçmişiyle çelişki yok mu? (kalıcı veritabanı)
    if codes & {"SEQ_DB_DUPLICATE", "NUMBER_REUSE"}:
        add("cross_reference", "Numara geçmişiyle çelişki yok mu?", "No conflict with the number history?",
            FALSE, "Bu işlem/sıra/referans numarası (banka bazında) daha önce FARKLI bir dekontta da "
            "görülmüş — kopyalanmış/uydurulmuş numara.",
            "This transaction/sequence/reference number was already seen (bank-scoped) on a DIFFERENT "
            "receipt — copied/fabricated.")
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
    _B = "Hesap hareketi" if is_statement else "Dekont"
    _Ben = "account statement" if is_statement else "receipt"
    if any_false:
        overall = FALSE
        o_tr = (f"{_B} GÜVENİLİR DEĞİL: en az bir doğrulama kesin olarak başarısız oldu "
                "(tahrifat/uyumsuzluk tespit edildi).")
        o_en = (f"The {_Ben} is NOT trustworthy: at least one verification definitively failed "
                "(tampering/inconsistency detected).")
    elif states.get("content_integrity") == TRUE:
        overall = TRUE
        o_tr = (f"{_B} GÜVENİLİR: gerçek dijital PDF üzerinde yapılan kesin doğrulamalarda "
                "tahrifat/uyumsuzluk bulunmadı.")
        o_en = (f"The {_Ben} is TRUSTWORTHY: definitive checks on the genuine digital PDF found "
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
        "belge_turu": doc_kind,
        "checks": checks,
        "overall": {"state": overall, "label_tr": o_tr, "label_en": o_en},
    }

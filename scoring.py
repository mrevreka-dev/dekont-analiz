"""
Doğruluk puanlama motoru / Authenticity scoring engine.

Yapısal bulguları (forensics) ve görsel adli analizi birleştirip:
  - authenticity_score (0-100)  : yüksek = güvenilir
  - risk_level                  : authentic | low | medium | high | critical
  - ai_trace                    : {likelihood 0-100, verdict}
  - verdict metni (TR/EN)
üretir.

Şeffaflık ilkesi: her ceza puanı bir bulgudan gelir; skor kırılımı raporda
gösterilir. Skorlar olasılıksaldır, kesin hukuki hüküm değildir.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from forensics import Finding

# Kategori bazında maksimum ceza (tek bir kategori skoru domine etmesin)
CATEGORY_CAP = {
    "metadata": 45,
    "structure": 40,
    "fonts": 20,
    "content": 20,
    "image": 45,
    "ai": 60,
}
# Belge tipine göre yapabileceğimiz en yüksek güven (yapısal doğrulama sınırı)
DOCTYPE_MAX_SCORE = {
    "digital_native": 100,
    "hybrid": 92,
    "scanned": 78,      # sadece görsel; yapısal doğrulama yok
    "image_only": 72,
}


@dataclass
class ScoreResult:
    authenticity_score: int = 100
    risk_level: str = "authentic"
    ai_likelihood: int = 0
    ai_verdict: str = ""
    verdict_tr: str = ""
    verdict_en: str = ""
    breakdown: list = field(default_factory=list)   # {category, penalty}
    penalties_total: int = 0
    bonuses_total: int = 0
    max_possible: int = 100
    not_a_receipt: bool = False


def _risk_level(score: int) -> str:
    if score >= 85:
        return "authentic"
    if score >= 70:
        return "low"
    if score >= 50:
        return "medium"
    if score >= 30:
        return "high"
    return "critical"


RISK_TR = {
    "authentic": "Güvenilir",
    "low": "Düşük Risk",
    "medium": "Orta Risk / Şüpheli",
    "high": "Yüksek Risk",
    "critical": "Kritik — Yüksek Olasılıkla Sahte/Oynanmış",
}
RISK_EN = {
    "authentic": "Trustworthy",
    "low": "Low Risk",
    "medium": "Medium Risk / Suspicious",
    "high": "High Risk",
    "critical": "Critical — Likely Forged/Tampered",
}


def compute_score(findings: list[Finding], doc_type: str,
                  img_manipulation: float = 0.0, img_ai: float = 0.0,
                  verdict_untrusted: bool = False) -> ScoreResult:
    res = ScoreResult()

    # --- Kategori bazında ceza topla (cap uygulayarak) ---
    cat_pen: dict[str, float] = {}
    bonus = 0.0
    for f in findings:
        if f.weight < 0:
            bonus += -f.weight
            continue
        cat_pen[f.category] = cat_pen.get(f.category, 0) + f.weight

    # Görsel adli analizi kategori cezalarına kat
    if img_manipulation:
        cat_pen["image"] = cat_pen.get("image", 0) + 0.40 * img_manipulation
    if img_ai:
        cat_pen["ai"] = cat_pen.get("ai", 0) + 0.60 * img_ai

    total_pen = 0.0
    for cat, pen in cat_pen.items():
        capped = min(pen, CATEGORY_CAP.get(cat, 40))
        total_pen += capped
        res.breakdown.append({"category": cat, "penalty": round(capped, 1)})

    res.breakdown.sort(key=lambda x: x["penalty"], reverse=True)

    score = 100 - total_pen + min(bonus, 10)
    # Belge tipi tavanı
    score = min(score, DOCTYPE_MAX_SCORE.get(doc_type, 100))
    score = int(max(0, min(100, round(score))))

    # SERT GEÇERSİZLEŞTİRME: revizyonlar arasında tutar/kritik alan değişmişse bu,
    # kanıtlanmış tahrifattır; kategori tavanlarından bağımsız olarak skoru kritiğe çeker.
    codes = {f.code for f in findings}
    if "NOT_A_RECEIPT" in codes:      # görselde dekont içeriği yok
        score = min(score, 5)
        res.not_a_receipt = True
    if "REV_AMOUNT_CHANGED" in codes:
        score = min(score, 8)
    elif "REV_CONTENT_CHANGED" in codes:
        score = min(score, 15)
    if "AMOUNT_MISMATCH" in codes:           # tutar belgede farklı yerlerde farklı yazılmış
        score = min(score, 8)
    if "FEE_RAIL_MISMATCH" in codes:         # ücret, işlem türünün (FAST/HAVALE) tarifesine uymuyor
        score = min(score, 8)
    if "RAIL_SAMEBANK_MISMATCH" in codes:    # FAST/EFT ama gönderici=alıcı banka (aynı banka = havale)
        score = min(score, 6)
    if "SAMEBANK_RAIL_CONTRADICTION" in codes:  # aynı banka ama başlık 'bankalararası/EFT/FAST'
        score = min(score, 6)
    if "INTERBANK_HAVALE_CONTRADICTION" in codes:  # farklı bankalar ama işlem HAVALE olarak sunuluyor
        score = min(score, 35)
    if "ID_FIELD_MISMATCH" in codes:         # VKN alanı ≠ İşlemi Yapan TCKN (kimlik uydurma)
        score = min(score, 10)
    if "AMOUNT_CURRENCY_INCONSISTENT" in codes:  # masraf gerçek şablondaki 'TL' sonekini taşımıyor
        score = min(score, 55)
    if "REF_FEE_CURRENCY_MISSING" in codes:   # referans: masraf bu bankada hep TL'li olur; burada yok
        score = min(score, 55)
    if "REF_ID_LENGTH_MISMATCH" in codes:     # referans: numara hane deseni gerçek şablondan farklı
        score = min(score, 62)
    if "DATE_IN_FUTURE" in codes:            # işlem/dekont tarihi gelecekte
        score = min(score, 8)
    if "RECEIPT_BEFORE_TXN" in codes:        # dekont, işlemden önce üretilmiş (imkânsız)
        score = min(score, 8)
    if "IMAGE_EDITOR_SOFTWARE" in codes:     # dekont fotoğrafı bir görüntü editöründen geçmiş
        score = min(score, 10)
    if "SELF_TRANSFER" in codes:             # gönderici IBAN = alıcı IBAN (anlamsız)
        score = min(score, 8)
    if "ID_CHECKSUM_INVALID" in codes:       # TCKN/VKN kontrol basamağı tutmuyor
        score = min(score, 20)
    if "RECEIPT_NO_DATE_MISMATCH" in codes:  # fiş numarasındaki tarih ≠ işlem tarihi (tarihleme)
        score = min(score, 8)
    if "PRODUCER_MISMATCH" in codes:         # bankanın gerçek kütüphanesiyle üretilmemiş
        score = min(score, 30)
    if "PDFIUM_PRODUCED" in codes:            # PDFium = yeniden basım (global sahte kuralı)
        score = min(score, 5)
    # NOT: KNOWN_FAKE (kara-liste eşleşmesi) ARTIK skoru düşürmez. Bir belgenin yalnızca
    # daha önce sahte damgalanmış olması tek başına hüküm değildir (eski yanlış-pozitifler
    # kalıcı ceza yaratmasın). Belge kendi güncel bulgularıyla değerlendirilir; kara-liste
    # eşleşmesi yalnızca BİLGİ notu olarak gösterilir (bkz. store.check_blocklist).
    if "ISSUER_IBAN_MISMATCH" in codes:       # ihracçının kodu taraf IBAN'larında yok (kesin)
        score = min(score, 8)
    if "IBAN_INVALID" in codes:               # IBAN mod-97 tutmuyor
        score = min(score, 20)
    if "RECEIVER_BANK_MISMATCH" in codes:     # yazan alıcı bankası ≠ IBAN bankası
        score = min(score, 12)
    if "SENDER_BANK_MISMATCH" in codes:       # yazan gönderici bankası ≠ IBAN bankası
        score = min(score, 12)
    if "AMOUNT_FONT_ANOMALY" in codes:        # tutar yabancı fontta (yapıştırılmış)
        score = min(score, 15)
    if codes & {"BROWSER_RERENDER", "FONT_BROWSER_RERENDER"}:  # tarayıcıyla yeniden basım
        score = min(score, 8)
    if "FONT_SET_MISMATCH" in codes:         # font kümesi bankanın şablonuyla uyuşmuyor
        score = min(score, 20)
    if "INTERNAL_DATE_MISMATCH" in codes:    # belge içi tarihler çelişiyor
        score = min(score, 12)
    if "TIME_FILE_BEFORE_TXN" in codes:      # geriye tarihleme — imkânsız
        score = min(score, 10)
    if "SINGLE_PHOTO_PDF" in codes:          # PDF içinde tek fotoğraf
        score = min(score, 20)
    if "QR_MISMATCH" in codes:
        score = min(score, 30)
    if "SEQ_DB_DUPLICATE" in codes:          # numara başka dekontta da var
        score = min(score, 12)
    if "NUMBER_REUSE" in codes:              # banka-bazlı: işlem/sıra/ref numarası başka dekontta da var
        score = min(score, 12)
    if "AI_VISUAL_TAMPER" in codes:          # YZ: yazı tipi/yapıştırma uyuşmazlığı (görsel tahrifat)
        score = min(score, 20)
    if "STATEMENT_BALANCE_BREAK" in codes:   # hesap hareketinde bakiye zinciri kırık
        score = min(score, 10)
    if "STATEMENT_ROW_COUNT_MISMATCH" in codes:   # beyan≠gerçek: satır silinmiş
        score = min(score, 12)

    # TUTARLILIK: Kesin karar "GÜVENİLİR DEĞİL" ise puan "güvenilir/düşük risk" olamaz.
    # Kritik geçersizleştirmeler zaten daha düşük çekmişse dokunma; aksi halde en fazla 40
    # (yüksek risk) yaparak puanı kesin kararla çelişmez hale getir.
    if verdict_untrusted:
        score = min(score, 40)

    res.authenticity_score = score
    res.penalties_total = int(round(total_pen))
    res.bonuses_total = int(round(min(bonus, 10)))
    res.max_possible = DOCTYPE_MAX_SCORE.get(doc_type, 100)
    res.risk_level = _risk_level(score)

    # --- AI olasılığı ---
    ai_like = img_ai
    # PDF tarafı AI/sentetik sinyalleri
    for f in findings:
        if f.category == "ai":
            ai_like = max(ai_like, f.weight)
    res.ai_likelihood = int(max(0, min(100, round(ai_like))))
    if res.ai_likelihood >= 60:
        res.ai_verdict = "AI üretimi güçlü olasılık / Strong AI-generation likelihood"
    elif res.ai_likelihood >= 30:
        res.ai_verdict = "AI/düzenleme izi olabilir / Possible AI or editing trace"
    else:
        res.ai_verdict = "Belirgin AI izi yok / No clear AI trace"

    # --- Sözel hüküm ---
    res.verdict_tr = _verdict_text(res, findings, doc_type, "tr")
    res.verdict_en = _verdict_text(res, findings, doc_type, "en")
    return res


def _verdict_text(res: ScoreResult, findings: list[Finding], doc_type: str, lang: str) -> str:
    crit = [f for f in findings if f.severity in ("critical", "high") and f.weight > 0]
    lvl_tr = RISK_TR[res.risk_level]
    lvl_en = RISK_EN[res.risk_level]
    if res.not_a_receipt:
        if lang == "tr":
            return ("BU DOSYA BİR BANKA DEKONTU DEĞİLDİR. Yüklenen görselde dekont içeriği "
                    "(banka adı, IBAN, tutar, gönderen/alıcı, işlem/referans numarası) tespit edilemedi. "
                    "Lütfen gerçek bir banka dekontu (tercihen orijinal dijital PDF) yükleyin.")
        return ("THIS FILE IS NOT A BANK RECEIPT. No receipt content (bank name, IBAN, amount, "
                "sender/receiver, transaction/reference number) was detected in the uploaded image. "
                "Please upload a genuine bank receipt (ideally the original digital PDF).")
    if lang == "tr":
        base = f"Doğruluk puanı {res.authenticity_score}/100 — {lvl_tr}. "
        if res.risk_level in ("authentic", "low"):
            base += "Belgede belirgin bir tahrifat/oynama bulgusu tespit edilmedi. "
        else:
            base += f"{len(crit)} adet önemli tahrifat/oynama sinyali tespit edildi. "
        if doc_type in ("image_only", "scanned"):
            base += "Belge yalnızca görsel içerdiğinden yapısal doğrulama yapılamadı; değerlendirme görsel analizle sınırlıdır."
        if res.ai_likelihood >= 30:
            base += f" Yapay zeka/görsel düzenleme izi olasılığı: %{res.ai_likelihood}."
        return base
    else:
        base = f"Authenticity score {res.authenticity_score}/100 — {lvl_en}. "
        if res.risk_level in ("authentic", "low"):
            base += "No significant tampering signals were detected. "
        else:
            base += f"{len(crit)} significant tamper signal(s) detected. "
        if doc_type in ("image_only", "scanned"):
            base += "The document contains only an image, so structural verification was not possible; assessment is limited to image analysis."
        if res.ai_likelihood >= 30:
            base += f" AI/editing trace likelihood: {res.ai_likelihood}%."
        return base

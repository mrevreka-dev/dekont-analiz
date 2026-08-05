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
                  img_manipulation: float = 0.0, img_ai: float = 0.0) -> ScoreResult:
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
    if "TIME_FILE_BEFORE_TXN" in codes:      # geriye tarihleme — imkânsız
        score = min(score, 10)
    if "SINGLE_PHOTO_PDF" in codes:          # PDF içinde tek fotoğraf
        score = min(score, 20)
    if "QR_MISMATCH" in codes:
        score = min(score, 30)
    if "SEQ_DB_DUPLICATE" in codes:          # numara başka dekontta da var
        score = min(score, 12)

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

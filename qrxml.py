"""
QR kod ve gömülü XML tespiti / QR code & embedded XML detection.

- QR: sayfa görüntüye çevrilir, OpenCV ile QR kod(lar) tespit/çözümlenir. Çözülen
  içerikte IBAN/tutar varsa, dekonttan çıkarılan alanlarla karşılaştırılır
  (uyuşmazlık = tahrifat işareti). QR yoksa tek başına risk değildir.
- Gömülü XML: bankaların e-dekont/GİB XML'i PDF'e gömülü olabilir. Ham baytlarda ve
  gömülü dosyalarda XML dekont imzası aranır; varsa alanlarla tutarlılık için kullanılır.
"""
from __future__ import annotations

import io
import re

_IBAN_RE = re.compile(r"TR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}", re.I)
_AMT_RE = re.compile(r"\d{1,3}(?:\.\d{3})*,\d{2}")


def detect_qr(pdf_bytes: bytes) -> dict:
    """Sayfadaki QR kodlarını tespit/çözümle. {found, count, payloads[]}."""
    out = {"found": False, "count": 0, "payloads": []}
    try:
        import numpy as np
        import cv2
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(pdf_bytes)
        try:
            page = doc[0]
            # yüksek çözünürlük — küçük QR'lar için
            img = page.render(scale=3.0).to_pil().convert("RGB")
        finally:
            doc.close()
        arr = np.asarray(img)[:, :, ::-1].copy()  # RGB->BGR
        det = cv2.QRCodeDetector()
        payloads = []
        try:
            ok, decoded, pts, _ = det.detectAndDecodeMulti(arr)
            if ok and decoded:
                payloads = [d for d in decoded if d]
        except Exception:
            pass
        if not payloads:
            try:
                s, pts, _ = det.detectAndDecode(arr)
                if s:
                    payloads = [s]
            except Exception:
                pass
        out["payloads"] = payloads
        out["count"] = len(payloads)
        out["found"] = len(payloads) > 0
    except Exception:
        pass
    return out


def cross_check_qr(payloads: list[str], sender_iban: str, receiver_iban: str,
                   amount: float | None) -> dict:
    """QR içeriğini görünen alanlarla karşılaştırır."""
    res = {"has_iban": False, "has_amount": False, "iban_match": None, "amount_match": None, "qr_ibans": []}
    joined = " ".join(payloads or [])
    if not joined:
        return res
    qr_ibans = [re.sub(r"\s+", "", m.group(0)).upper() for m in _IBAN_RE.finditer(joined)]
    res["qr_ibans"] = qr_ibans
    if qr_ibans:
        res["has_iban"] = True
        known = {i for i in (sender_iban, receiver_iban) if i}
        # QR'daki IBAN'lardan biri belgedeki IBAN'larla eşleşiyor mu?
        res["iban_match"] = any(qi in known for qi in qr_ibans) if known else None
    amts = [a.group(0) for a in _AMT_RE.finditer(joined)]
    if amts and amount is not None:
        res["has_amount"] = True
        target = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        res["amount_match"] = any(a == target for a in amts)
    return res


# e-dekont / GİB / UBL XML imzaları
_XML_MARKERS = [
    b"<?xml", b"<Dekont", b"<Invoice", b"<eArsiv", b"<earsiv", b"urn:oasis:names:specification:ubl",
    b"e-Dekont", b"<cbc:", b"<cac:", b"<Signature", b"efatura", b"gib.gov.tr",
]


def detect_embedded_xml(pdf_bytes: bytes) -> dict:
    """PDF'te gömülü XML/e-dekont verisi olup olmadığını arar."""
    out = {"present": False, "looks_like_dekont": False, "has_embedded_files": False, "snippet": ""}
    data = pdf_bytes
    # 1) ham bayt taraması
    idx = data.find(b"<?xml")
    if idx == -1:
        # UBL/GİB imzaları da dene
        for mk in _XML_MARKERS[1:]:
            j = data.find(mk)
            if j != -1:
                idx = j
                break
    if idx != -1:
        out["present"] = True
        snip = data[idx:idx + 400]
        try:
            out["snippet"] = snip.decode("utf-8", "ignore")
        except Exception:
            out["snippet"] = ""
        low = data.lower()
        out["looks_like_dekont"] = any(m.lower() in low for m in
                                       [b"dekont", b"invoice", b"ubl", b"efatura", b"earsiv", b"gib.gov.tr", b"<cbc:"])
    # 2) gömülü dosyalar (/EmbeddedFiles)
    try:
        import pikepdf
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
        try:
            names = pdf.Root.get("/Names", {})
            ef = names.get("/EmbeddedFiles") if names else None
            if ef is not None:
                out["has_embedded_files"] = True
        except Exception:
            pass
        finally:
            pdf.close()
    except Exception:
        pass
    return out

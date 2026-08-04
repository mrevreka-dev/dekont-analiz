"""
OCR yardımcıları / OCR helpers.

PDF sayfasını pypdfium2 ile görüntüye çevirir, pytesseract (Türkçe+İngilizce)
ile metne döker. Türkçe dil paketi yoksa İngilizce'ye düşer ve Türkçe karakter
düzeltmesi uygular.
"""
from __future__ import annotations

import io
import re
import shutil
import subprocess

import pypdfium2 as pdfium
from PIL import Image

_HAS_TESS = shutil.which("tesseract") is not None


def _available_langs() -> set[str]:
    if not _HAS_TESS:
        return set()
    try:
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=15)
        return {l.strip() for l in out.stdout.splitlines()[1:] if l.strip()}
    except Exception:
        return set()


_LANGS = _available_langs()


def best_lang() -> str:
    if "tur" in _LANGS and "eng" in _LANGS:
        return "tur+eng"
    if "tur" in _LANGS:
        return "tur"
    return "eng"


# İngilizce OCR ile karışan Türkçe karakter düzeltmeleri (kaba)
_TR_FIX = [
    (r"\bTUTARl?\b", "TUTAR"),
]


def render_page_to_image(pdf_bytes: bytes, page_index: int = 0, scale: float = 3.0) -> Image.Image:
    """Belirtilen sayfayı yüksek çözünürlüklü PIL Image olarak döndürür."""
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        page = doc[page_index]
        bitmap = page.render(scale=scale)
        return bitmap.to_pil().convert("RGB")
    finally:
        doc.close()


def extract_embedded_images(pdf_bytes: bytes):
    """PDF'teki gömülü görselleri PIL Image listesi olarak döndürür (en büyükler önce)."""
    import pikepdf
    out = []
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except Exception:
        return out
    try:
        seen = set()
        for pg in pdf.pages:
            res = pg.get("/Resources", {})
            xobjs = res.get("/XObject", {}) if res else {}
            for _, xobj in dict(xobjs).items():
                try:
                    if xobj.get("/Subtype") != "/Image":
                        continue
                    oid = (int(xobj.objgen[0]), int(xobj.objgen[1]))
                    if oid in seen:
                        continue
                    seen.add(oid)
                    pim = pikepdf.PdfImage(xobj)
                    img = pim.as_pil_image().convert("RGB")
                    out.append(img)
                except Exception:
                    continue
    finally:
        pdf.close()
    out.sort(key=lambda im: im.size[0] * im.size[1], reverse=True)
    return out


def ocr_image(img: Image.Image, lang: str | None = None) -> str:
    if not _HAS_TESS:
        return ""
    import pytesseract
    lang = lang or best_lang()
    # Ölçekle ve gri tonlama ile OCR doğruluğunu artır
    w, h = img.size
    if max(w, h) < 1200:
        f = 1200 / max(w, h)
        img = img.resize((int(w * f), int(h * f)))
    try:
        txt = pytesseract.image_to_string(img, lang=lang)
    except Exception:
        try:
            txt = pytesseract.image_to_string(img, lang="eng")
        except Exception:
            return ""
    for pat, rep in _TR_FIX:
        txt = re.sub(pat, rep, txt)
    return txt


def ocr_pdf(pdf_bytes: bytes, page_index: int = 0) -> str:
    """Sayfayı render edip OCR uygular; ayrıca gömülü görselleri de dener."""
    texts = []
    try:
        img = render_page_to_image(pdf_bytes, page_index, scale=3.0)
        texts.append(ocr_image(img))
    except Exception:
        pass
    # Büyük gömülü görselleri de OCR'la (bazı PDF'lerde sayfa render'ı zayıf olabilir)
    try:
        for im in extract_embedded_images(pdf_bytes)[:2]:
            if im.size[0] * im.size[1] > 200 * 200:
                texts.append(ocr_image(im))
    except Exception:
        pass
    # En uzun (en bilgi dolu) çıktıyı döndür
    texts = [t for t in texts if t and t.strip()]
    if not texts:
        return ""
    return max(texts, key=len)


def ocr_available() -> bool:
    return _HAS_TESS

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


# ------------------- Görüntü ön-işleme (GENEL, her foto için) -------------------
def _cv_gray(img: Image.Image):
    import numpy as np, cv2
    arr = np.asarray(img.convert("RGB"))[:, :, ::-1].copy()   # RGB -> BGR
    return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)


def _detect_document_region(gray):
    """Fotoğrafta parlak (beyaz) belge kartını bulur; yoksa None."""
    import numpy as np, cv2
    H, W = gray.shape
    _, th = cv2.threshold(gray, 140, 255, cv2.THRESH_BINARY)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 0.12 * H * W:
        return None
    x, y, w, h = cv2.boundingRect(c)
    pad = int(0.01 * max(W, H))
    x = max(0, x - pad); y = max(0, y - pad)
    w = min(W - x, w + 2 * pad); h = min(H - y, h + 2 * pad)
    if w * h > 0.97 * H * W:     # neredeyse tüm görsel => kırpma
        return None
    return (x, y, w, h)


def _trim_to_ink(gray):
    """Beyaz kenar boşluklarını atıp koyu metin (mürekkep) bölgesine kırpar."""
    import cv2, numpy as np
    _, dark = cv2.threshold(gray, 120, 255, cv2.THRESH_BINARY_INV)
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((9, 35), np.uint8))
    ys, xs = np.where(dark > 0)
    if len(xs) < 60:
        return gray
    x0, x1 = max(0, xs.min() - 12), min(gray.shape[1], xs.max() + 12)
    y0, y1 = max(0, ys.min() - 12), min(gray.shape[0], ys.max() + 12)
    if (x1 - x0) < 40 or (y1 - y0) < 40:
        return gray
    return gray[y0:y1, x0:x1]


def _variants(gray):
    """OCR için ön-işlenmiş varyant(lar) üretir. HIZ için sadeleştirildi: en yavaş adım olan
    fastNlMeansDenoising kaldırıldı; 2 varyant (keskin + Otsu) yeterli, bulanık fotoğrafların
    zaten Vision'a yükseldiği için Tesseract'ın hızlı olması önceliklidir."""
    import cv2, numpy as np
    h, w = gray.shape
    scale = max(1.0, 1600.0 / max(h, w))       # 2000 yerine 1600 (daha hızlı, yeterli çözünürlük)
    if scale > 1.01:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    out = []
    # A: bilateral + CLAHE(3.0) + GÜÇLÜ unsharp — bulanık telefon fotoları için
    g = cv2.bilateralFilter(gray, 9, 40, 40)
    g = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(g)
    blur = cv2.GaussianBlur(g, (0, 0), 3)
    sharp = cv2.addWeighted(g, 2.2, blur, -1.2, 0)
    out.append(sharp)
    # B: Otsu ikili (keskin varyanttan) — net belgeler için
    _, otsu = cv2.threshold(sharp, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out.append(otsu)
    return out


def ocr_image_candidates(img: Image.Image, lang: str | None = None) -> list[str]:
    """Bir görselden birden çok ön-işleme varyantı ile OCR metinleri (aday liste)."""
    if not _HAS_TESS:
        return []
    import pytesseract
    lang = lang or best_lang()
    try:
        gray = _cv_gray(img)
        region = _detect_document_region(gray)
        if region:
            x, y, w, h = region
            gray = gray[y:y+h, x:x+w]
        gray = _trim_to_ink(gray)      # beyaz kenarları at, metne odakla
        variants = _variants(gray)
    except Exception:
        # CV başarısızsa basit yol
        w, h = img.size
        if max(w, h) < 1400:
            f = 1400 / max(w, h); img = img.resize((int(w*f), int(h*f)))
        import numpy as np
        variants = [np.asarray(img.convert("L"))]
    texts = []
    for i, v in enumerate(variants):
        psms = (6,)                            # HIZ: tek PSM (blok metin); ekstra PSM 4 geçişi kaldırıldı
        for psm in psms:
            try:
                t = pytesseract.image_to_string(v, lang=lang, config=f"--oem 1 --psm {psm}")
            except Exception:
                t = ""
            if t and t.strip():
                for pat, rep in _TR_FIX:
                    t = re.sub(pat, rep, t)
                texts.append(t)
    return texts


def ocr_image(img: Image.Image, lang: str | None = None) -> str:
    cands = ocr_image_candidates(img, lang)
    return max(cands, key=len) if cands else ""


def ocr_pdf_candidates(pdf_bytes: bytes, page_index: int = 0) -> list[str]:
    """Sayfayı ve gömülü görselleri OCR'layıp tüm aday metinleri döndürür."""
    cands = []
    try:
        img = render_page_to_image(pdf_bytes, page_index, scale=2.0)   # HIZ: 3.0 yerine 2.0
        cands += ocr_image_candidates(img)
    except Exception:
        pass
    # HIZ: gömülü görsel OCR'ı yalnızca sayfa render'ı hiç metin vermediyse dene (nadir durum);
    # aksi halde ek Tesseract geçişi maliyeti gereksizdir.
    if not any(c and c.strip() for c in cands):
        try:
            for im in extract_embedded_images(pdf_bytes)[:1]:
                if im.size[0] * im.size[1] > 200 * 200:
                    cands += ocr_image_candidates(im)
        except Exception:
            pass
    return [c for c in cands if c and c.strip()]


def ocr_pdf(pdf_bytes: bytes, page_index: int = 0) -> str:
    cands = ocr_pdf_candidates(pdf_bytes, page_index)
    return max(cands, key=len) if cands else ""


def ocr_available() -> bool:
    return _HAS_TESS

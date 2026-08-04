"""
Görsel adli analiz + AI-izi tespiti / Image forensics + AI-trace detection.

Gömülü/oluşturulmuş görselleri inceler:
  - Metadata / yazılım imzaları (Photoshop, AI araçları, C2PA içerik kimlik bilgisi)
  - ELA (Error Level Analysis) — yeniden kaydetme farkıyla düzenlenmiş bölge tespiti
  - JPEG çift-sıkıştırma / kalite tutarsızlığı
  - Gürültü (noise) tutarsızlığı — birleştirme (splicing) işareti
  - AI üretimi heuristik sinyalleri

Not: Görsel adli analiz olasılıksaldır; bu modül kanıt üretir, kesin hüküm vermez.
Skorlama scoring.py'de birleştirilir.
"""
from __future__ import annotations

import io
import re
import base64
from dataclasses import dataclass, field

import numpy as np
from PIL import Image, ImageChops, ExifTags

# AI görsel üretim / düzenleme araç imzaları
AI_SIGNATURES = [
    "stable diffusion", "stablediffusion", "dall-e", "dalle", "midjourney",
    "firefly", "adobe firefly", "generative", "gan", "diffusion",
    "openai", "leonardo.ai", "playground", "flux", "imagen", "ideogram",
    "gpt-4o", "gemini", "nightcafe", "artbreeder", "runway",
]
EDIT_SIGNATURES = [
    "photoshop", "gimp", "lightroom", "affinity", "pixelmator",
    "paint.net", "canva", "figma", "illustrator", "snapseed", "picsart",
]
C2PA_MARKERS = ["c2pa", "contentcredentials", "content credentials", "jumbf", "cai\x00"]


@dataclass
class ImageForensics:
    analyzed: bool = False
    width: int = 0
    height: int = 0
    format: str = ""
    mode: str = ""

    # Metadata
    has_exif: bool = False
    exif_software: str = ""
    exif_make: str = ""
    exif_model: str = ""
    exif_datetime: str = ""
    ai_signature_hits: list = field(default_factory=list)
    edit_signature_hits: list = field(default_factory=list)
    c2pa_present: bool = False

    # ELA
    ela_mean: float = 0.0
    ela_max: float = 0.0
    ela_p99: float = 0.0
    ela_hotspot_ratio: float = 0.0     # yüksek-hata piksel oranı
    ela_preview_b64: str = ""

    # Diğer
    jpeg_quality_est: int = 0
    double_compression_suspected: bool = False
    noise_inconsistency: float = 0.0

    # Türetilmiş skorlar (0-100)
    manipulation_score: float = 0.0    # yüksek = oynama şüphesi yüksek
    ai_score: float = 0.0              # yüksek = yapay üretim şüphesi yüksek

    signals: list = field(default_factory=list)   # açıklamalı bulgular


def _to_bytes(img: Image.Image, fmt="JPEG", quality=90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, fmt, quality=quality)
    return buf.getvalue()


def error_level_analysis(img: Image.Image, quality: int = 90):
    """ELA: görseli yeniden JPEG kaydedip farkı ölçer. Düzenlenmiş bölgeler
    farklı hata seviyesi gösterir."""
    rgb = img.convert("RGB")
    resaved = Image.open(io.BytesIO(_to_bytes(rgb, "JPEG", quality))).convert("RGB")
    ela = ImageChops.difference(rgb, resaved)
    arr = np.asarray(ela).astype(np.float32)
    lum = arr.max(axis=2)  # kanal başına maksimum fark
    mean = float(lum.mean())
    mx = float(lum.max())
    p99 = float(np.percentile(lum, 99))
    hot = float((lum > max(30.0, p99 * 0.8)).mean())
    # Görselleştirme için normalize edilmiş önizleme
    scale = 255.0 / (mx if mx > 1 else 1)
    vis = np.clip(lum * scale, 0, 255).astype(np.uint8)
    vis_img = Image.fromarray(vis).convert("L")
    # küçült
    vis_img.thumbnail((520, 520))
    b = io.BytesIO(); vis_img.save(b, "PNG")
    b64 = base64.b64encode(b.getvalue()).decode()
    return mean, mx, p99, hot, b64


def estimate_jpeg_quality(raw: bytes) -> int:
    """Kaba JPEG kalite tahmini (nicemleme tablosu enerjisinden)."""
    try:
        im = Image.open(io.BytesIO(raw))
        qt = getattr(im, "quantization", None)
        if not qt:
            return 0
        # ilk tablonun ortalaması -> kaliteye kabaca çevir
        vals = list(qt.values())[0]
        avg = sum(vals) / len(vals)
        # yaklaşık ters ilişki
        q = max(1, min(100, int(100 - avg * 0.8)))
        return q
    except Exception:
        return 0


def analyze_image(img: Image.Image, raw: bytes | None = None) -> ImageForensics:
    r = ImageForensics()
    r.analyzed = True
    r.width, r.height = img.size
    r.format = (img.format or "").upper()
    r.mode = img.mode

    # --- Metadata / EXIF ---
    try:
        exif = img.getexif()
        if exif and len(exif) > 0:
            r.has_exif = True
            tagmap = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}
            r.exif_software = str(tagmap.get("Software", "") or "")
            r.exif_make = str(tagmap.get("Make", "") or "")
            r.exif_model = str(tagmap.get("Model", "") or "")
            r.exif_datetime = str(tagmap.get("DateTime", "") or "")
    except Exception:
        pass

    # Ham baytlarda imza taraması (EXIF/XMP/JUMBF/C2PA)
    blob = (raw or b"")
    text_blob = ""
    try:
        text_blob = blob.decode("latin-1", "ignore").lower()
    except Exception:
        text_blob = ""
    meta_all = (r.exif_software + " " + text_blob).lower()
    for sig in AI_SIGNATURES:
        if sig in meta_all:
            r.ai_signature_hits.append(sig)
    for sig in EDIT_SIGNATURES:
        if sig in meta_all:
            r.edit_signature_hits.append(sig)
    for m in C2PA_MARKERS:
        if m in text_blob:
            r.c2pa_present = True
            break

    # --- ELA ---
    try:
        r.ela_mean, r.ela_max, r.ela_p99, r.ela_hotspot_ratio, r.ela_preview_b64 = \
            error_level_analysis(img)
    except Exception:
        pass

    # --- JPEG kalite / çift sıkıştırma ---
    if raw:
        r.jpeg_quality_est = estimate_jpeg_quality(raw)

    # --- Gürültü tutarsızlığı (bloklar arası std sapması) ---
    try:
        r.noise_inconsistency = _noise_inconsistency(img)
    except Exception:
        pass

    _score_image(r)
    return r


def _noise_inconsistency(img: Image.Image) -> float:
    """Görseli bloklara böler, yüksek-frekans gürültü std'sinin blok bazında
    değişkenliğini ölçer. Birleştirilen (spliced) bölgeler farklı gürültü taşır."""
    g = np.asarray(img.convert("L")).astype(np.float32)
    if g.size == 0:
        return 0.0
    # basit yüksek geçiren: komşu fark
    hp = g - np.roll(g, 1, axis=0)
    h, w = hp.shape
    bs = 32
    stds = []
    for y in range(0, h - bs, bs):
        for x in range(0, w - bs, bs):
            block = hp[y:y+bs, x:x+bs]
            stds.append(block.std())
    if len(stds) < 4:
        return 0.0
    stds = np.array(stds)
    m = stds.mean()
    if m < 1e-6:
        return 0.0
    # varyasyon katsayısı
    return float(stds.std() / m)


def _score_image(r: ImageForensics) -> None:
    manip = 0.0
    ai = 0.0
    sig = r.signals

    if r.ai_signature_hits:
        ai += 70
        sig.append({"severity": "critical", "category": "ai",
                    "tr": f"Görsel metadata'sında yapay zeka üretim imzası bulundu: {', '.join(set(r.ai_signature_hits))}.",
                    "en": f"AI generation signature found in image metadata: {', '.join(set(r.ai_signature_hits))}."})
    if r.c2pa_present:
        ai += 40
        sig.append({"severity": "high", "category": "ai",
                    "tr": "Görselde C2PA/İçerik Kimlik Bilgisi (Content Credentials) meta verisi var — "
                          "genellikle yapay zeka ile üretilmiş/düzenlenmiş içerikte bulunur.",
                    "en": "C2PA / Content Credentials metadata present — often indicates AI-generated/edited content."})
    if r.edit_signature_hits:
        manip += 35
        sig.append({"severity": "high", "category": "image",
                    "tr": f"Görsel bir düzenleme yazılımından geçmiş: {', '.join(set(r.edit_signature_hits))}.",
                    "en": f"Image processed by editing software: {', '.join(set(r.edit_signature_hits))}."})

    # ELA hotspot oranı — lokalize düzenleme işareti
    if r.ela_hotspot_ratio > 0.020:
        manip += 30
        sig.append({"severity": "high", "category": "image",
                    "tr": f"ELA analizi görselin %{r.ela_hotspot_ratio*100:.1f}'inde yoğun hata seviyesi farkı gösteriyor — "
                          f"belirli bölgelerin sonradan düzenlenmiş/eklenmiş olabileceğine işaret.",
                    "en": f"ELA shows elevated error level over {r.ela_hotspot_ratio*100:.1f}% of the image — possible localized edits."})
    elif r.ela_hotspot_ratio > 0.008:
        manip += 15
        sig.append({"severity": "medium", "category": "image",
                    "tr": f"ELA analizinde orta düzeyde hata farkı bölgeleri var (%{r.ela_hotspot_ratio*100:.1f}). İncelenmeli.",
                    "en": f"ELA shows moderate error-level regions ({r.ela_hotspot_ratio*100:.1f}%). Worth review."})

    # Gürültü tutarsızlığı
    if r.noise_inconsistency > 1.4:
        manip += 18
        sig.append({"severity": "medium", "category": "image",
                    "tr": f"Görselde gürültü dağılımı tutarsız (katsayı {r.noise_inconsistency:.2f}) — farklı kaynaklardan "
                          f"birleştirme (splicing) olasılığı.",
                    "en": f"Inconsistent noise distribution (coef {r.noise_inconsistency:.2f}) — possible splicing."})

    # Kamera/tarayıcı metadata'sı yok + fotoğrafik boyut -> screenshot/synthetic
    if not r.has_exif and not r.exif_make and (r.width * r.height) > 500 * 500:
        ai += 8
        manip += 5
        sig.append({"severity": "low", "category": "image",
                    "tr": "Görselde kamera/tarayıcı EXIF bilgisi yok. Ekran görüntüsü ya da yeniden üretilmiş olabilir "
                          "(tek başına kanıt değildir).",
                    "en": "No camera/scanner EXIF present — may be a screenshot or re-generated image (not conclusive alone)."})

    r.manipulation_score = float(min(100, manip))
    r.ai_score = float(min(100, ai))

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

    # Recapture (ekranın yeniden çekimi) / moiré — BİLGİ amaçlı (ceza değil):
    # ekran-görüntüsü (screenshot, kolay düzenlenir) ile ikinci-cihaz çekimi (recapture,
    # moiré içerir, düzenlemesi zor) ayrımını yapmaya yarar.
    moire_score: float = 0.0
    recapture_suspected: bool = False
    ela_hotspot_concentrated: bool = False   # ELA sıcak noktaları küçük bir bölgede yoğunlaşmış (yapıştırma)

    # Zemin rengi / renk tonu analizi
    bg_color: list = field(default_factory=list)     # baskın zemin rengi (RGB)
    bg_dev_max: float = 0.0                            # en yüksek bölgesel zemin sapması
    bg_dev_hotspot_ratio: float = 0.0                 # yüksek sapmalı blok oranı
    bg_patch_count: int = 0                            # ayrık şüpheli zemin bölgesi sayısı
    bg_patch_max: int = 0                              # en büyük bitişik şüpheli bölge (blok)
    tone_chroma_var: float = 0.0                       # zemin renk tonu (chroma) değişkenliği
    tone_cast: float = 0.0                             # genel renk tonu sapması (nötr'den)
    bg_heatmap_b64: str = ""                           # zemin sapma ısı haritası

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


def _moire_recapture_score(img: Image.Image) -> float:
    """Ekranın yeniden çekiminde (recapture) oluşan periyodik piksel-ızgara deseni (moiré),
    frekans uzayında (FFT) orta-yüksek bantta belirgin izole TEPELER üretir. Skor = bu bandın
    99.9 persentili / medyanı. Yüksek skor = güçlü periyodik desen = ekran-çekimi olasılığı.
    NOT: Bu bir SAHTECİLİK sinyali DEĞİLDİR; yalnızca screenshot↔recapture ayrımı için bilgidir."""
    import numpy as np
    try:
        g = np.asarray(img.convert("L"), dtype=np.float32)
        h, w = g.shape
        m = max(h, w)
        if m > 1000:
            import cv2
            s = 1000.0 / m
            g = cv2.resize(g, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        g = g - float(g.mean())
        # Hann penceresi (kenar sızıntısını azalt)
        H, W = g.shape
        wy = np.hanning(H)[:, None]; wx = np.hanning(W)[None, :]
        g = g * wy * wx
        mag = np.abs(np.fft.fftshift(np.fft.fft2(g)))
        cy, cx = H // 2, W // 2
        yy, xx = np.ogrid[:H, :W]
        rad = np.sqrt((yy - cy) ** 2 + (xx - cx) ** 2)
        rmin = min(H, W) * 0.12
        rmax = min(H, W) * 0.48
        band = mag[(rad > rmin) & (rad < rmax)]
        if band.size < 50:
            return 0.0
        med = float(np.median(band)) + 1e-6
        peak = float(np.percentile(band, 99.9))
        return round(peak / med, 1)
    except Exception:
        return 0.0


def _ela_concentration(img: Image.Image) -> bool:
    """ELA sıcak-noktaları görselin KÜÇÜK ve BİTİŞİK bir bölgesinde toplanmışsa (tüm belgeye
    yayılmak yerine), bu lokalize bir düzenlemeye (tek bir alanın yapıştırılması) işaret eder."""
    import numpy as np
    try:
        rgb = img.convert("RGB")
        resaved = Image.open(io.BytesIO(_to_bytes(rgb, "JPEG", 90))).convert("RGB")
        lum = np.asarray(ImageChops.difference(rgb, resaved)).astype(np.float32).max(axis=2)
        thr = max(30.0, float(np.percentile(lum, 99)) * 0.8)
        hot = lum > thr
        frac = float(hot.mean())
        if frac < 0.001 or frac > 0.08:
            return False       # ya çok az ya da yaygın -> lokalize paste değil
        # sıcak piksellerin uzaysal yayılımı: dar bir kutuya sığıyorsa yoğunlaşmış demektir
        ys, xs = np.where(hot)
        if ys.size < 30:
            return False
        bh = (ys.max() - ys.min() + 1) / lum.shape[0]
        bw = (xs.max() - xs.min() + 1) / lum.shape[1]
        return (bh * bw) < 0.12    # sıcak alan, kadrajın <%12'lik bir kutusuna sığıyor
    except Exception:
        return False


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
        # Çift sıkıştırma sezgisi (muhafazakâr): görsel kendi tahmini kalitesinde yeniden
        # kaydedildiğinde tek-sıkıştırılmış bir görselde fark ~0 olmalıdır; belirgin bir artık
        # fark, görselin DAHA ÖNCE de sıkıştırıldığına (açılıp yeniden dışa aktarıldığına) işarettir.
        try:
            if (r.format or "").upper() in ("JPEG", "JPG") and r.jpeg_quality_est:
                q = max(60, min(95, r.jpeg_quality_est))
                r2 = Image.open(io.BytesIO(_to_bytes(img, "JPEG", q))).convert("RGB")
                d = np.asarray(ImageChops.difference(img.convert("RGB"), r2)).astype(np.float32).max(axis=2)
                r.double_compression_suspected = float(d.mean()) > 4.0
        except Exception:
            pass

    # --- Gürültü tutarsızlığı (bloklar arası std sapması) ---
    try:
        r.noise_inconsistency = _noise_inconsistency(img)
    except Exception:
        pass

    # --- Moiré / recapture (ekranın yeniden çekimi) — BİLGİ amaçlı ---
    try:
        r.moire_score = _moire_recapture_score(img)
        r.recapture_suspected = r.moire_score >= 12.0
    except Exception:
        pass

    # --- ELA sıcak-noktası yoğunlaşması (lokalize yapıştırma) ---
    try:
        r.ela_hotspot_concentrated = _ela_concentration(img)
    except Exception:
        pass

    # --- Zemin rengi / renk tonu analizi ---
    try:
        background_tone_analysis(r, img)
    except Exception:
        pass

    _score_image(r)
    return r


def background_tone_analysis(r: "ImageForensics", img: Image.Image) -> None:
    """
    Zemin (arka plan) rengi ve renk tonu analizi.

    Dekontlar tek düze açık bir zemine sahiptir. Bir bölgenin üzerine ekleme /
    rötuş yapıldığında (ör. tutar/isim silinip yeniden yazıldığında) o bölgedeki
    zemin beyazı, sıkıştırma veya kaynak farkı yüzünden hafifçe farklı bir tona
    kayar. Bu fonksiyon sayfayı ızgaraya böler, her bloğun zemin rengini küresel
    zemin rengiyle karşılaştırır ve sapan bölgeleri (olası oynama) işaretler.
    Ayrıca zeminin renk tonu (chroma) tutarlılığını ve genel renk kaymasını ölçer.
    """
    rgb = np.asarray(img.convert("RGB")).astype(np.float32)
    H, W, _ = rgb.shape
    if H < 40 or W < 40:
        return
    mx = rgb.max(2); mn = rgb.min(2)
    sat = mx - mn
    # zemin (near-white) maskesi
    bg_mask = (mn > 170) & (sat < 40)
    if bg_mask.sum() < 0.02 * H * W:
        bg_mask = mn > 150
    if bg_mask.sum() < 100:
        return
    global_bg = rgb[bg_mask].mean(0)
    r.bg_color = [int(round(c)) for c in global_bg]

    gy, gx = 26, 18
    grid = np.full((gy, gx), np.nan)
    chroma_vals = []
    for i in range(gy):
        for j in range(gx):
            y0 = i * H // gy; y1 = (i + 1) * H // gy
            x0 = j * W // gx; x1 = (j + 1) * W // gx
            m = bg_mask[y0:y1, x0:x1]
            if m.sum() < 25:
                continue
            block = rgb[y0:y1, x0:x1]
            bgc = block[m].mean(0)
            grid[i, j] = float(np.linalg.norm(bgc - global_bg))
            chroma_vals.append(abs(bgc[0] - bgc[1]) + abs(bgc[1] - bgc[2]) + abs(bgc[0] - bgc[2]))

    valid = grid[~np.isnan(grid)]
    if valid.size == 0:
        return
    r.bg_dev_max = float(valid.max())
    p95 = float(np.percentile(valid, 95))
    # Yüksek eşik: gerçek rötuş, metin-kenarı uç değerlerinin BELİRGİN üstünde olmalı
    high_thr = max(13.0, p95 * 1.4)
    hot = np.nan_to_num(grid, nan=0.0) > high_thr
    r.bg_dev_hotspot_ratio = float(hot.sum() / valid.size)
    # kompakt bölge tespiti: en büyük bitişik yüksek-sapma bloğu (rötuş = kompakt alan)
    cnt, r.bg_patch_max = _count_patches(hot)
    r.bg_patch_count = cnt
    if chroma_vals:
        cv = np.array(chroma_vals)
        r.tone_chroma_var = float(cv.std())
        r.tone_cast = float(cv.mean())
    # ısı haritası görselleştirmesi
    r.bg_heatmap_b64 = _heatmap_png(grid, thr)


def _count_patches(mask: np.ndarray):
    """Bağlı-bileşen sayımı (4-komşuluk). (adet, en_büyük_boyut) döndürür."""
    m = np.asarray(mask).astype(bool)
    seen = np.zeros_like(m, dtype=bool)
    cnt = 0
    biggest = 0
    H, W = m.shape
    for i in range(H):
        for j in range(W):
            if m[i, j] and not seen[i, j]:
                cnt += 1
                size = 0
                stack = [(i, j)]
                seen[i, j] = True
                while stack:
                    y, x = stack.pop()
                    size += 1
                    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < H and 0 <= nx < W and m[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                biggest = max(biggest, size)
    return cnt, biggest


def _heatmap_png(grid: np.ndarray, thr: float) -> str:
    g = np.nan_to_num(grid, nan=0.0)
    mx = max(g.max(), thr * 1.5, 1.0)
    norm = np.clip(g / mx, 0, 1)
    # kırmızı kanalı sapmayla artan basit ısı haritası
    h, w = norm.shape
    rgb = np.zeros((h, w, 3), dtype=np.uint8)
    rgb[..., 0] = (norm * 255).astype(np.uint8)             # kırmızı = sapma
    rgb[..., 2] = ((1 - norm) * 120).astype(np.uint8)       # mavi = düşük
    img = Image.fromarray(rgb).resize((w * 16, h * 16), Image.NEAREST)
    b = io.BytesIO(); img.save(b, "PNG")
    return base64.b64encode(b.getvalue()).decode()


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

    # Zemin rengi oynaması — TANISAL. Tek başına zemin sapması logo/mühür/gölge
    # de olabilir; bu yüzden YÜKSEK ceza yalnızca ELA (hata seviyesi) ile AYNI
    # yönde teyit edilirse verilir. Aksi halde inceleme notu (düşük) bırakılır.
    ela_corroborates = r.ela_hotspot_ratio > 0.008
    if r.bg_patch_max >= 3 and r.bg_dev_max >= 16:
        if ela_corroborates:
            manip += 28
            sig.append({"severity": "high", "category": "image",
                        "tr": f"Zeminde kompakt bir bölgede renk sapması (~{r.bg_patch_max} blok, maks. {r.bg_dev_max:.1f}) "
                              f"ELA hata-seviyesi anomalisiyle ÖRTÜŞÜYOR. İki bağımsız sinyalin aynı bölgeyi işaret etmesi, "
                              f"o alanın silinip üzerine yeniden yazıldığına (rötuş/yama) güçlü işarettir.",
                        "en": f"A compact background deviation (~{r.bg_patch_max} blocks, max {r.bg_dev_max:.1f}) COINCIDES with an "
                              f"ELA anomaly — two independent signals on the same area strongly indicate a retouched/patched region."})
        else:
            manip += 6
            sig.append({"severity": "low", "category": "image",
                        "tr": f"Zeminde bölgesel renk farkı var (~{r.bg_patch_max} blok, maks. {r.bg_dev_max:.1f}). Bu bir logo/mühür/"
                              f"gölge ya da rötuş olabilir; ısı haritasında işaretli bölge(ler)i gözle inceleyin.",
                        "en": f"Regional background-color difference (~{r.bg_patch_max} blocks, max {r.bg_dev_max:.1f}) — could be a logo/"
                              f"stamp/shadow or a retouch; visually inspect the highlighted area(s) in the heatmap."})

    # Renk tonu tutarsızlığı (zemin nötr olmalı; bölgesel renkli ton = şüpheli)
    if r.tone_chroma_var >= 7:
        manip += 15
        sig.append({"severity": "medium", "category": "image",
                    "tr": f"Zeminin renk tonu bölgeden bölgeye tutarsız (chroma değişkenliği {r.tone_chroma_var:.1f}). "
                          f"Farklı kaynaktan yapıştırma veya renk rötuşu olasılığı.",
                    "en": f"Background color tone varies across regions (chroma variance {r.tone_chroma_var:.1f}) — possible paste/retouch."})
    elif r.tone_cast >= 14:
        sig.append({"severity": "low", "category": "image",
                    "tr": f"Genel renk tonu nötr beyazdan sapıyor (renk kayması {r.tone_cast:.1f}). Fotoğraf ışığı veya filtre olabilir.",
                    "en": f"Overall color cast away from neutral white ({r.tone_cast:.1f}) — lighting or a filter."})

    # Gürültü tutarsızlığı (belge taramalarında metin/zemin farkı doğaldır; eşik yüksek)
    if r.noise_inconsistency > 2.4:
        manip += 14
        sig.append({"severity": "medium", "category": "image",
                    "tr": f"Görselde gürültü dağılımı belirgin tutarsız (katsayı {r.noise_inconsistency:.2f}) — farklı kaynaklardan "
                          f"birleştirme (splicing) olasılığı.",
                    "en": f"Strongly inconsistent noise distribution (coef {r.noise_inconsistency:.2f}) — possible splicing."})

    # RECAPTURE (ekranın ikinci cihazla çekimi) — BİLGİ amaçlı, CEZA YOK.
    # Not: recapture, ekran-görüntüsüne (screenshot) göre düzenlemesi ZOR bir üretim biçimidir;
    # bu yüzden şüphe değil, olumlu/nötr bir bağlamdır.
    if r.recapture_suspected:
        sig.append({"severity": "info", "category": "image",
                    "tr": f"Bu görsel, bir ekranın İKİNCİ BİR CİHAZLA yeniden çekimi (recapture) olabilir "
                          f"(moiré/periyodik desen skoru {r.moire_score:.0f}). Recapture, ekran görüntüsüne göre "
                          f"dijital düzenlemesi daha zordur — bu bir SAHTECİLİK işareti değil, bilgilendirici bir bağlamdır.",
                    "en": f"This image may be a re-capture of a screen with a second device (moiré score "
                          f"{r.moire_score:.0f}). Recapture is harder to digitally edit than a screenshot — informational, not a forgery sign."})

    # Kamera/tarayıcı metadata'sı yok + fotoğrafik boyut. Recapture varsa 'ekran görüntüsü' değil
    # ikinci-cihaz çekimi olması muhtemel; recapture yoksa screenshot (kolay düzenlenir) uyarısı verilir.
    if not r.has_exif and not r.exif_make and (r.width * r.height) > 500 * 500:
        if r.recapture_suspected:
            pass  # recapture bağlamı yukarıda bilgi olarak verildi; ek şüphe/ceza yok
        else:
            ai += 8
            manip += 5
            sig.append({"severity": "low", "category": "image",
                        "tr": "Görselde kamera/tarayıcı EXIF bilgisi yok ve ekran-çekimi (recapture) deseni de yok — "
                              "bu bir EKRAN GÖRÜNTÜSÜ (screenshot) ya da yeniden üretilmiş görsel olabilir. Ekran "
                              "görüntüleri dijital olarak KOLAY düzenlenebilir; şüpheli durumda orijinal PDF ya da "
                              "ikinci bir cihazla çekilmiş görüntü/video isteyin (tek başına kanıt değildir).",
                        "en": "No camera EXIF and no screen-recapture pattern — may be a screenshot or re-generated image "
                              "(screenshots are easy to edit; request the original PDF or a second-device capture)."})

    # ELA sıcak-noktası küçük bir bölgede YOĞUNLAŞMIŞSA: lokalize düzenleme (tek alanın yapıştırılması)
    if r.ela_hotspot_concentrated:
        manip += 14
        sig.append({"severity": "high", "category": "image",
                    "tr": "ELA sıcak-noktaları görselin KÜÇÜK ve BİTİŞİK bir bölgesinde yoğunlaşmış — belgenin geri "
                          "kalanından farklı hata seviyesi taşıyan bu alan, tek bir bölgenin (ör. tutar/isim) sonradan "
                          "DÜZENLENDİĞİNE/yapıştırıldığına işaret edebilir. Fotoğrafta kesinlik düşüktür; orijinal PDF isteyin.",
                    "en": "ELA hotspots are concentrated in a small contiguous region — this area carries a different error "
                          "level than the rest, which can indicate a single field (e.g. amount/name) was edited/pasted."})

    # Çift JPEG sıkıştırma: düşük tahmini kalite + belirgin ELA -> yeniden kaydedilmiş/düzenlenmiş
    if r.double_compression_suspected:
        manip += 8
        sig.append({"severity": "medium", "category": "image",
                    "tr": "Görselde çift JPEG sıkıştırma izi var — görsel en az bir kez açılıp yeniden kaydedilmiş; "
                          "düzenleme sonrası yeniden dışa aktarma olasılığı (fotoğrafta tek başına kanıt değildir).",
                    "en": "Double JPEG compression traces — the image was re-saved at least once; possible edit-and-re-export."})

    r.manipulation_score = float(min(100, manip))
    r.ai_score = float(min(100, ai))


# ---------------------------------------------------------------------------
#  ALAN-LOKALİZE PİKSEL FORENSİĞİ (fotoğraf dekontlar için — kalibrasyon aşaması)
#  Tutar / alıcı adı / alıcı IBAN gibi KRİTİK alanların metin kutularını bulur ve
#  mürekkep koyuluğu + keskinlik (kenar enerjisi) açısından belgenin GENEL metniyle
#  karşılaştırır. Yapıştırılmış/düzenlenmiş metin çoğu zaman farklı koyulukta ya da
#  keskinliktedir. Robust-z (medyan + MAD) ile yalnızca GÜÇLÜ aykırılıklar işaretlenir.
#  Şimdilik BİLGİ amaçlı (ağırlık 0) — yanlış-pozitif üretmeden veri toplamak için.
# ---------------------------------------------------------------------------
def _laplacian_var(patch: "np.ndarray") -> float:
    if patch.size < 16:
        return 0.0
    p = patch.astype(np.float32)
    lap = (-4 * p
           + np.roll(p, 1, 0) + np.roll(p, -1, 0)
           + np.roll(p, 1, 1) + np.roll(p, -1, 1))
    lap = lap[1:-1, 1:-1]
    return float(lap.var()) if lap.size else 0.0


def _mad(vals, med):
    return float(np.median([abs(v - med) for v in vals])) if vals else 0.0


def _norm_txt(s: str) -> str:
    return re.sub(r"[^0-9a-zçğıöşü]", "", (s or "").lower())


def text_field_forensics(img: "Image.Image", targets: dict, lang: str = "tur+eng") -> dict:
    """targets: {alan_adı: değer}. Döner: {suspects:[{field,word,z_dark,z_sharp}], checked:int}."""
    out = {"suspects": [], "checked": 0}
    try:
        import pytesseract
        from pytesseract import Output
    except Exception:
        return out
    try:
        gray = img.convert("L")
        arr = np.asarray(gray, dtype=np.float32)
        H, W = arr.shape
        data = pytesseract.image_to_data(gray, lang=lang, output_type=Output.DICT)
    except Exception:
        return out
    boxes = []
    for i in range(len(data.get("text", []))):
        txt = (data["text"][i] or "").strip()
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if not txt or conf < 45:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        if w < 8 or h < 8 or h > H * 0.08:
            continue
        patch = arr[y:y + h, x:x + w]
        if patch.size < 40:
            continue
        lo, hi = float(patch.min()), float(patch.max())
        if hi - lo < 25:                     # kontrast yok (boş/leke) -> atla
            continue
        thr = lo + (hi - lo) * 0.40
        ink = patch[patch < thr]
        dark = float(ink.mean()) if ink.size >= 6 else float(patch.mean())
        boxes.append({"txt": txt, "n": _norm_txt(txt), "h": h,
                      "dark": dark, "sharp": _laplacian_var(patch)})
    out["checked"] = len(boxes)
    if len(boxes) < 10:
        return out
    darks = [b["dark"] for b in boxes]
    sharps = [b["sharp"] for b in boxes]
    dmed = float(np.median(darks)); dmad = _mad(darks, dmed) or 1.0
    smed = float(np.median(sharps)); smad = _mad(sharps, smed) or 1.0
    seen = set()
    for field, val in (targets or {}).items():
        nv = _norm_txt(val)
        if not nv or len(nv) < 3:
            continue
        for b in boxes:
            if len(b["n"]) < 3 or b["n"] not in nv:
                continue
            zd = abs(b["dark"] - dmed) / (1.4826 * dmad)
            zs = abs(b["sharp"] - smed) / (1.4826 * smad)
            if (zd > 3.5 or zs > 4.5) and (field, b["n"]) not in seen:
                seen.add((field, b["n"]))
                out["suspects"].append({"field": field, "word": b["txt"],
                                        "z_dark": round(zd, 1), "z_sharp": round(zs, 1)})
    return out

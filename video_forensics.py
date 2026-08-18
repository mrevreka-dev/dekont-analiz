"""
Video adli analiz / Video forensic analysis.

Sahte / düzenlenmiş / üzerinde-oynanmış / yeniden-kodlanmış / yapay-zeka-üretimi videoları
tespit eder. Dekont doğrulama bağlamında: bir dekonttan şüphelenildiğinde kullanıcıdan İKİNCİ
BİR TELEFONLA çekilmiş video istenir (ekran kaydına göre düzenlemesi çok daha zordur). Bu servis,
gelen videonun HAM bir cihaz çekimi mi yoksa bir araç/düzenleyici/AI ile ÜRETİLMİŞ/YENİDEN
KODLANMIŞ mı olduğunu, konteyner (metadata/encoder) ve kare-düzeyi (ELA/gürültü/moiré) sinyalleriyle
değerlendirir.

BAĞIMSIZLIK: Bu modül mevcut dekont API'sini (uçlar/cevap key'leri) HİÇBİR ŞEKİLDE etkilemez;
yalnızca yeni /api/v1/analyze-video ucundan çağrılır ve KENDİ şemasını döndürür.

Bağımlılık: ffmpeg/ffprobe (sistemde kurulu). Yoksa modül zarifçe kısıtlı çalışır (yalnız konteyner
metadata'sı olmadan boş sonuç döndürür, çökmez).
"""
from __future__ import annotations

import os
import io
import json
import shutil
import tempfile
import subprocess


# =====================================================================
#  Encoder / üretici imzaları (küçük harf alt-dize eşleşmesi)
# =====================================================================
# FFmpeg (libav) ile üretim/yeniden-kodlama: komut satırı aracı; ham cihaz çekimi DEĞİL.
# Birçok düzenleyici ve AI-video aracı çıktısını FFmpeg ile verir -> 'Lavf/Lavc/libx264'.
_FFMPEG_ENC = ["lavf", "lavc", "libx264", "libx265", "libaom", "libvpx",
               "libavformat", "libavcodec", "ffmpeg"]

# Video DÜZENLEYİCİLER (kesme/montaj/altyazı/efekt) — çıktı = işlenmiş video.
_EDITOR_ENC = ["adobe", "premiere", "after effects", "media encoder", "aftereffects",
               "capcut", "kapwing", "canva", "handbrake", "vegas", "shotcut", "kdenlive",
               "davinci", "resolve", "final cut", "finalcut", "imovie", "filmora", "inshot",
               "wondershare", "clipchamp", "veed", "descript", "camtasia", "screenflow",
               "openshot", "lightworks", "movavi", "powerdirector", "videoleap", "splice"]

# YAPAY ZEKA video üreticileri (deepfake / metin-video / görsel-video).
_AI_ENC = ["sora", "runway", "gen-2", "gen-3", "pika", "kling", "luma", "dream machine",
           "veo", "haiper", "genmo", "hailuo", "minimax", "stable video", "svd",
           "animatediff", "modelscope", "zeroscope", "cogvideo", "hunyuan", "wan2",
           "wan 2", "d-id", "heygen", "synthesia", "deepfacelab", "faceswap", "roop",
           "deepfake", "reface", "sadtalker"]

# Ham cihaz çekimine işaret eden üretici/handler ipuçları (olumlu — güven artırıcı).
_DEVICE_HINTS = ["apple", "iphone", "quicktime", "samsung", "xiaomi", "redmi", "huawei",
                 "oppo", "vivo", "realme", "oneplus", "google", "pixel", "motorola",
                 "nokia", "sony", "gopro", "android", "mediarecorder"]


def is_configured() -> bool:
    """ffprobe/ffmpeg kurulu mu."""
    return bool(shutil.which("ffprobe") and shutil.which("ffmpeg"))


def _run(cmd: list, timeout: float = 90.0) -> tuple[int, bytes, bytes]:
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception:
        return 1, b"", b""


def ffprobe_info(path: str) -> dict:
    """Konteyner + akış metadata'sını JSON olarak döndürür (boş dict = başarısız)."""
    if not shutil.which("ffprobe"):
        return {}
    rc, out, _ = _run(["ffprobe", "-v", "quiet", "-print_format", "json",
                       "-show_format", "-show_streams", path], timeout=60)
    if rc != 0 or not out:
        return {}
    try:
        return json.loads(out.decode("utf-8", "replace"))
    except Exception:
        return {}


def _collect_encoder_text(info: dict) -> str:
    """Konteyner + tüm akış tag'lerindeki 'encoder'/handler/vendor metnini birleştirir (küçük harf)."""
    parts = []
    fmt = info.get("format", {}) or {}
    ftags = fmt.get("tags", {}) or {}
    for k in ("encoder", "com.apple.quicktime.software", "handler_name", "major_brand"):
        if ftags.get(k):
            parts.append(str(ftags[k]))
    for s in info.get("streams", []) or []:
        stags = s.get("tags", {}) or {}
        for k in ("encoder", "handler_name", "vendor_id"):
            if stags.get(k):
                parts.append(str(stags[k]))
    return " ".join(parts).lower()


def _hits(text: str, needles: list) -> list:
    return sorted({n for n in needles if n in text})


def _has_creation_time(info: dict) -> bool:
    fmt = info.get("format", {}) or {}
    if (fmt.get("tags", {}) or {}).get("creation_time"):
        return True
    for s in info.get("streams", []) or []:
        if (s.get("tags", {}) or {}).get("creation_time"):
            return True
    return False


def _video_stream(info: dict) -> dict:
    for s in info.get("streams", []) or []:
        if s.get("codec_type") == "video":
            return s
    return {}


def _fps(rate: str) -> float:
    try:
        if "/" in str(rate):
            a, b = str(rate).split("/")
            return float(a) / float(b) if float(b) else 0.0
        return float(rate)
    except Exception:
        return 0.0


def _extract_frames(path: str, n: int = 6) -> list:
    """Videodan ~n adet kareyi eşit aralıklarla çıkarır; PIL Image listesi döndürür."""
    if not shutil.which("ffmpeg"):
        return []
    try:
        from PIL import Image
    except Exception:
        return []
    tmp = tempfile.mkdtemp(prefix="vf_")
    imgs = []
    try:
        # Sahne değişimi + eşit örnekleme yerine basit: her fps*k karede bir (fps filtresi).
        # 'fps=1/K' ile saniyede <1 kare al; toplam ~n kare için K = süre/n.
        info = ffprobe_info(path)
        dur = 0.0
        try:
            dur = float((info.get("format", {}) or {}).get("duration") or 0.0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            step = 2.0
        else:
            step = max(dur / (n + 1), 0.5)
        out_pat = os.path.join(tmp, "f_%03d.jpg")
        rc, _, _ = _run(["ffmpeg", "-v", "quiet", "-i", path, "-vf",
                         f"fps=1/{step:.3f}", "-frames:v", str(n), "-q:v", "3", out_pat],
                        timeout=90)
        for fn in sorted(os.listdir(tmp)):
            if fn.endswith(".jpg"):
                try:
                    with open(os.path.join(tmp, fn), "rb") as fh:
                        raw = fh.read()
                    img = Image.open(io.BytesIO(raw)); img.load()
                    imgs.append((img, raw))
                except Exception:
                    continue
    finally:
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
    return imgs


def analyze_video(path: str, sample_frames: int = 6) -> dict:
    """
    Videoyu adli olarak değerlendirir ve KENDİ şemasında bir sözlük döndürür.
    Anahtarlar (yeni servis — mevcut dekont API'sinden BAĞIMSIZ):
      engine, is_video, duration_sec, container{...}, encoding{...}, frames{...},
      signals[], score(0-100), risk, verdict_tr, verdict_en
    """
    res = {
        "engine": "video-forensics/1.0",
        "is_video": False,
        "ffmpeg_available": is_configured(),
        "duration_sec": None,
        "container": {},
        "encoding": {},
        "frames": {"sampled": 0},
        "signals": [],
        "score": 0,
        "risk": "bilinmiyor",
        "verdict_tr": "",
        "verdict_en": "",
    }
    if not is_configured():
        res["verdict_tr"] = "ffmpeg/ffprobe bulunamadı; video analizi yapılamadı."
        res["verdict_en"] = "ffmpeg/ffprobe not available; video analysis could not run."
        return res

    info = ffprobe_info(path)
    vs = _video_stream(info)
    if not info or not vs:
        res["verdict_tr"] = "Geçerli bir video akışı bulunamadı."
        res["verdict_en"] = "No valid video stream found."
        return res

    res["is_video"] = True
    fmt = info.get("format", {}) or {}
    try:
        res["duration_sec"] = round(float(fmt.get("duration") or 0.0), 2)
    except Exception:
        res["duration_sec"] = None

    enc_text = _collect_encoder_text(info)
    ff_hits = _hits(enc_text, _FFMPEG_ENC)
    ed_hits = _hits(enc_text, _EDITOR_ENC)
    ai_hits = _hits(enc_text, _AI_ENC)
    dev_hits = _hits(enc_text, _DEVICE_HINTS)
    has_ct = _has_creation_time(info)

    r_rate = _fps(vs.get("r_frame_rate", "0"))
    avg_rate = _fps(vs.get("avg_frame_rate", "0"))
    # Değişken kare hızı (VFR): telefon kamerası tipik; sabit tam-sayı fps yeniden-kodlamaya yakın.
    is_vfr = bool(r_rate and avg_rate and abs(r_rate - avg_rate) > 0.6)
    pix = vs.get("pix_fmt", "")

    res["container"] = {
        "format": fmt.get("format_name", ""),
        "major_brand": (fmt.get("tags", {}) or {}).get("major_brand", ""),
        "creation_time_present": has_ct,
        "encoder_text": enc_text[:200],
        "handler": (vs.get("tags", {}) or {}).get("handler_name", ""),
        "vendor_id": (vs.get("tags", {}) or {}).get("vendor_id", ""),
    }
    res["encoding"] = {
        "codec": vs.get("codec_name", ""),
        "width": vs.get("width"), "height": vs.get("height"),
        "pix_fmt": pix,
        "r_frame_rate": vs.get("r_frame_rate", ""),
        "avg_frame_rate": vs.get("avg_frame_rate", ""),
        "variable_frame_rate": is_vfr,
        "nb_frames": vs.get("nb_frames", ""),
        "ffmpeg_encode": bool(ff_hits),
        "editor_hits": ed_hits,
        "ai_hits": ai_hits,
        "device_hints": dev_hits,
    }

    signals = []
    score = 100

    # --- Konteyner/encoder sinyalleri ---
    if ai_hits:
        score -= 60
        signals.append({"code": "VIDEO_AI_GENERATOR", "severity": "critical",
                        "tr": f"Video metadata'sında YAPAY ZEKA video üreticisi imzası: {', '.join(ai_hits)}. "
                              f"Bu video bir yapay-zeka aracıyla ÜRETİLMİŞ olabilir — güvenilmez.",
                        "en": f"AI video-generator signature in metadata: {', '.join(ai_hits)}. "
                              f"The video may be AI-generated — untrusted.",
                        "detail": enc_text[:120]})
    if ed_hits:
        score -= 35
        signals.append({"code": "VIDEO_EDITOR", "severity": "high",
                        "tr": f"Video bir DÜZENLEYİCİ ile işlenmiş görünüyor ({', '.join(ed_hits)}). Ham cihaz "
                              f"çekimi değil; kesilmiş/montajlanmış/eklenmiş olabilir — dikkatle incelenmeli.",
                        "en": f"Video appears processed by an editor ({', '.join(ed_hits)}). Not a raw device "
                              f"capture; may be cut/edited — review carefully.",
                        "detail": enc_text[:120]})
    if ff_hits and not ed_hits and not ai_hits:
        # FFmpeg ile yeniden kodlanmış. creation_time yoksa ham çekim OLMA olasılığı düşer.
        if not has_ct:
            score -= 30
            signals.append({"code": "VIDEO_REENCODED_TOOL", "severity": "high",
                            "tr": "Video bir ARAÇLA (FFmpeg/libav) yeniden kodlanmış ve cihaz 'oluşturma zamanı' "
                                  "(creation_time) YOK. Ham telefon çekimlerinde bu bilgi bulunur; eksikliği + araç "
                                  "encoder'ı, videonun dışa-aktarılmış/yeniden-üretilmiş olduğunu gösterir — güvenilmez.",
                            "en": "Video was re-encoded by a tool (FFmpeg/libav) and has NO device creation_time. "
                                  "Genuine phone captures carry it; its absence + tool encoder indicates a re-exported/"
                                  "regenerated video — untrusted.",
                            "detail": enc_text[:120]})
        else:
            score -= 12
            signals.append({"code": "VIDEO_REENCODED", "severity": "medium",
                            "tr": "Video bir araçla (FFmpeg/libav) yeniden kodlanmış. Meşru bir dönüştürme olabilir "
                                  "ama ham cihaz çekimi değildir; içerik değişmiş olabilir — bilgilendirme.",
                            "en": "Video was re-encoded by a tool (FFmpeg/libav). Could be a legitimate transcode "
                                  "but is not a raw device capture — advisory.",
                            "detail": enc_text[:120]})
    elif not has_ct and not ff_hits and not dev_hits:
        score -= 8
        signals.append({"code": "VIDEO_NO_CREATION_TIME", "severity": "low",
                        "tr": "Videoda cihaz 'oluşturma zamanı' (creation_time) yok. Tek başına kanıt değildir; "
                              "bazı paylaşım kanalları bu bilgiyi siler — bilgilendirme.",
                        "en": "Video has no device creation_time. Not conclusive alone; some sharing channels strip "
                              "it — advisory."})

    # Olumlu (güven artırıcı) bağlam: ham cihaz çekimi işaretleri
    if dev_hits and has_ct and not ed_hits and not ai_hits and not ff_hits:
        signals.append({"code": "VIDEO_DEVICE_CAPTURE", "severity": "info",
                        "tr": f"Ham cihaz çekimi işaretleri mevcut (üretici/handler: {', '.join(dev_hits)}, "
                              f"oluşturma zamanı var{', değişken kare hızı' if is_vfr else ''}). Bu, düzenlenmemiş "
                              f"bir kaydı destekler — olumlu bağlam.",
                        "en": f"Raw device-capture markers present ({', '.join(dev_hits)}; creation_time"
                              f"{', VFR' if is_vfr else ''}). Supports an unedited capture — positive context."})

    # --- Kare-düzeyi adli analiz (mevcut image_forensics yeniden kullanılır) ---
    frames = _extract_frames(path, n=sample_frames)
    res["frames"]["sampled"] = len(frames)
    if frames:
        try:
            import image_forensics as _imf
        except Exception:
            _imf = None
        if _imf is not None:
            moire_max = 0.0
            recapture_any = False
            ela_conc_any = False
            dbl_any = False
            manip_max = 0.0
            ai_frame_max = 0.0
            edit_sig = set()
            ai_sig = set()
            c2pa_any = False
            for img, raw in frames:
                try:
                    r = _imf.analyze_image(img, raw)
                except Exception:
                    continue
                moire_max = max(moire_max, getattr(r, "moire_score", 0.0) or 0.0)
                recapture_any = recapture_any or bool(getattr(r, "recapture_suspected", False))
                ela_conc_any = ela_conc_any or bool(getattr(r, "ela_hotspot_concentrated", False))
                dbl_any = dbl_any or bool(getattr(r, "double_compression_suspected", False))
                manip_max = max(manip_max, getattr(r, "manipulation_score", 0.0) or 0.0)
                ai_frame_max = max(ai_frame_max, getattr(r, "ai_score", 0.0) or 0.0)
                edit_sig.update(getattr(r, "edit_signature_hits", []) or [])
                ai_sig.update(getattr(r, "ai_signature_hits", []) or [])
                c2pa_any = c2pa_any or bool(getattr(r, "c2pa_present", False))
            res["frames"].update({
                "moire_max": round(moire_max, 1),
                "recapture_suspected": recapture_any,
                "ela_hotspot_concentrated": ela_conc_any,
                "double_compression_suspected": dbl_any,
                "manipulation_score_max": round(manip_max, 1),
                "ai_score_max": round(ai_frame_max, 1),
                "edit_signature_hits": sorted(edit_sig),
                "ai_signature_hits": sorted(ai_sig),
                "c2pa_present": c2pa_any,
            })
            # Kare-tabanlı sinyaller
            if ela_conc_any:
                score -= 18
                signals.append({"code": "VIDEO_FRAME_LOCAL_EDIT", "severity": "high",
                                "tr": "Karelerde LOKALİZE düzenleme izi (ELA sıcak noktaları küçük bir bölgede "
                                      "yoğun). Bir alan (ör. tutar/isim) kare üzerinde değiştirilmiş olabilir.",
                                "en": "Localized edit trace in frames (concentrated ELA hotspots). A region "
                                      "(e.g. amount/name) may have been altered on-frame."})
            if manip_max >= 55:
                score -= 12
                signals.append({"code": "VIDEO_FRAME_MANIPULATION", "severity": "medium",
                                "tr": f"Bazı karelerde oynama şüphe skoru yüksek ({manip_max:.0f}/100).",
                                "en": f"Elevated frame manipulation score ({manip_max:.0f}/100)."})
            if recapture_any:
                # Dekont bağlamında İKİNCİ-TELEFON çekimi GÜVEN ARTIRICIDIR (ekran kaydına göre
                # düzenlemesi zor). Bu yüzden ceza YOK — bilgilendirici not.
                signals.append({"code": "VIDEO_RECAPTURE", "severity": "info",
                                "tr": f"Kareler bir ekranın İKİNCİ CİHAZLA yeniden çekimi olabilir (moiré "
                                      f"{moire_max:.0f}). Dekont doğrulamada bu, düzenlemesi zor bir kayıt olduğu "
                                      f"için OLUMLU bağlamdır — sahtecilik işareti değildir.",
                                "en": f"Frames may be a second-device recapture of a screen (moiré {moire_max:.0f}). "
                                      f"In receipt verification this is positive context (hard to edit), not a "
                                      f"forgery signal."})
            if ai_sig:
                score -= 40
                signals.append({"code": "VIDEO_FRAME_AI", "severity": "critical",
                                "tr": f"Karelerde yapay-zeka üretimi imzası: {', '.join(sorted(ai_sig))}.",
                                "en": f"AI-generation signature in frames: {', '.join(sorted(ai_sig))}."})
            if c2pa_any:
                signals.append({"code": "VIDEO_C2PA", "severity": "info",
                                "tr": "Karelerde C2PA/içerik kimlik bilgisi bulundu (köken/üretim iddiası içerir).",
                                "en": "C2PA/content credentials found in frames (provenance claim present)."})

    score = max(0, min(100, score))
    res["score"] = score
    # Risk bandı + karar
    crit = any(s["severity"] == "critical" for s in signals)
    high = any(s["severity"] == "high" for s in signals)
    if crit or score < 35:
        res["risk"] = "kritik"
        res["verdict_tr"] = ("Yüksek olasılıkla ÜRETİLMİŞ/DÜZENLENMİŞ video. Ham cihaz çekimi olarak "
                             "güvenilmez; orijinal kaydı ikinci bir telefonla, kesintisiz ve tek çekimde isteyin.")
        res["verdict_en"] = ("Likely a generated/edited video. Untrusted as a raw capture; request the original "
                             "as a single uninterrupted second-phone recording.")
    elif high or score < 60:
        res["risk"] = "yüksek"
        res["verdict_tr"] = ("Şüpheli: video işlenmiş/yeniden kodlanmış işaretleri taşıyor. Ham cihaz çekimi "
                             "olduğu doğrulanamadı — dikkatle incelenmeli.")
        res["verdict_en"] = ("Suspicious: the video carries processed/re-encoded markers. Could not confirm a raw "
                             "capture — review carefully.")
    elif score < 80:
        res["risk"] = "orta"
        res["verdict_tr"] = ("Belirgin bir üretim/düzenleme işareti bulunmadı ancak bazı belirsizlikler var. "
                             "Mümkünse orijinal, kesintisiz ikinci-telefon çekimiyle teyit edin.")
        res["verdict_en"] = ("No clear generation/editing markers, but some uncertainty remains. Confirm with the "
                             "original uninterrupted second-phone capture if possible.")
    else:
        res["risk"] = "düşük"
        res["verdict_tr"] = ("Ham cihaz çekimiyle tutarlı; belirgin üretim/düzenleme işareti yok. (Video analizi "
                             "olasılıksaldır; tek başına kesin delil değildir.)")
        res["verdict_en"] = ("Consistent with a raw device capture; no clear generation/editing markers. (Video "
                             "analysis is probabilistic; not conclusive alone.)")

    res["signals"] = signals
    return res

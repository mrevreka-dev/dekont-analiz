"""
Orkestratör / Orchestrator.

Bir PDF'i (bytes) alır, tüm analiz hattını çalıştırır ve tek bir yapılandırılmış
rapor (dict) döndürür. Web servisi ve web arayüzü bu raporu kullanır.
"""
from __future__ import annotations

import io
import time
import datetime as _dt
from dataclasses import asdict

import pikepdf

from pdf_structure import analyze_structure_bytes, classify_producer
from forensics import classify_doc_type, detect
from extract import extract_text_digital, extract_fields
import ocr
from image_forensics import analyze_image, ImageForensics
from scoring import compute_score

ENGINE_VERSION = "1.0.0"


def _largest_image_with_raw(pdf_bytes: bytes):
    """En büyük gömülü görseli (PIL, ham_bytes) olarak döndürür."""
    try:
        pdf = pikepdf.open(io.BytesIO(pdf_bytes))
    except Exception:
        return None, None
    best = None
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
                    w = int(xobj.get("/Width", 0)); h = int(xobj.get("/Height", 0))
                    area = w * h
                    pim = pikepdf.PdfImage(xobj)
                    if best is None or area > best[0]:
                        try:
                            raw = pim.obj.read_raw_bytes()
                        except Exception:
                            raw = None
                        best = (area, pim, raw)
                except Exception:
                    continue
    finally:
        pass
    pdf.close()
    if not best:
        return None, None
    try:
        img = best[1].as_pil_image().convert("RGB")
    except Exception:
        img = None
    return img, best[2]


def analyze_document(pdf_bytes: bytes, filename: str = "") -> dict:
    t0 = time.time()

    # 1) Yapı / metadata
    struct = analyze_structure_bytes(pdf_bytes)

    # 2) Metin (dijital), yetersizse OCR
    text_layout = extract_text_digital(pdf_bytes, layout=True)
    text_read = extract_text_digital(pdf_bytes, layout=False)
    digital_text_len = len((text_layout or "").strip())   # SADECE dijital metin
    struct.text_char_count = digital_text_len
    text_source = "digital"
    if digital_text_len < 40:
        ocr_text = ocr.ocr_pdf(pdf_bytes) if ocr.ocr_available() else ""
        if ocr_text and ocr_text.strip():
            text_layout = text_read = ocr_text
            text_source = "ocr"
        else:
            text_source = "none"
    # metin çıkarımı için kullanılacak uzunluk (OCR dahil olabilir)
    extract_text_len = len((text_layout or "").strip())

    # 3) Belge tipi — YALNIZCA dijital (seçilebilir) metne göre; OCR metni sayılmaz
    doc_type = classify_doc_type(struct, digital_text_len)
    struct.doc_type = doc_type

    # 4) Yapısal tahrifat bulguları
    findings = detect(struct, digital_text_len, doc_type)

    # 5) Alan çıkarımı
    extraction = extract_fields(text_layout, text_read)
    extraction.text_source = text_source

    # 6) Görsel adli analiz (görsel içeren belgeler için)
    img_forensics: ImageForensics | None = None
    manip = ai = 0.0
    if struct.image_count > 0:
        pil, raw = _largest_image_with_raw(pdf_bytes)
        if pil is None and doc_type in ("image_only", "scanned"):
            try:
                pil = ocr.render_page_to_image(pdf_bytes, 0, scale=2.0)
            except Exception:
                pil = None
        if pil is not None:
            # imza taraması için tüm PDF baytlarını da ver
            img_forensics = analyze_image(pil, raw if raw else pdf_bytes)
            # image_only/scanned dışında küçük logo görselleri ELA'yı yanıltmasın:
            if doc_type in ("digital_native",):
                # dijital native belgede görsel sadece logo/mühür; manipülasyonu düşür
                manip = 0.0
                ai = img_forensics.ai_score if img_forensics.ai_signature_hits or img_forensics.c2pa_present else 0.0
            else:
                manip = img_forensics.manipulation_score
                ai = img_forensics.ai_score

    # 7) Skorlama
    score = compute_score(findings, doc_type, manip, ai)

    # 8) Rapor derle
    lang_findings = [f.as_dict("tr") for f in findings]
    lang_findings_en = [f.as_dict("en") for f in findings]

    report = {
        "engine_version": ENGINE_VERSION,
        "analyzed_at": _dt.datetime.utcnow().isoformat() + "Z",
        "elapsed_ms": int((time.time() - t0) * 1000),
        "file": {
            "name": filename,
            "size_bytes": struct.file_size,
            "sha256": struct.sha256,
            "md5": struct.md5,
        },
        "classification": {
            "doc_type": doc_type,
            "doc_type_label_tr": _DOCTYPE_TR.get(doc_type, doc_type),
            "doc_type_label_en": _DOCTYPE_EN.get(doc_type, doc_type),
            "text_source": text_source,
            "is_digital_pdf": doc_type in ("digital_native", "hybrid"),
            "is_image_only": doc_type in ("image_only", "scanned"),
        },
        "score": {
            "authenticity_score": score.authenticity_score,
            "max_possible": score.max_possible,
            "risk_level": score.risk_level,
            "penalties_total": score.penalties_total,
            "bonuses_total": score.bonuses_total,
            "breakdown": score.breakdown,
            "verdict_tr": score.verdict_tr,
            "verdict_en": score.verdict_en,
        },
        "ai_trace": {
            "likelihood": score.ai_likelihood,
            "verdict": score.ai_verdict,
            "image_ai_score": round(ai, 1),
            "signatures": (img_forensics.ai_signature_hits if img_forensics else []),
            "c2pa_present": (img_forensics.c2pa_present if img_forensics else False),
        },
        "tamper": {
            "manipulation_score": round(manip, 1),
            "incremental_updates": struct.incremental_updates,
            "resaved_by_editor": bool(classify_producer(struct.producer, struct.creator)["editor_hits"]),
            "append_mode": classify_producer(struct.producer, struct.creator)["append_mode"],
            "findings_count": len([f for f in findings if f.weight > 0]),
            "critical_findings": len([f for f in findings if f.severity in ("critical", "high") and f.weight > 0]),
        },
        "findings_tr": lang_findings,
        "findings_en": lang_findings_en,
        "metadata": {
            "producer": struct.producer,
            "creator": struct.creator,
            "author": struct.author,
            "title": struct.title,
            "pdf_version": struct.pdf_version,
            "page_count": struct.page_count,
            "page_sizes": struct.page_sizes,
            "creation_date": struct.creation_date,
            "mod_date": struct.mod_date,
            "creation_iso": struct.creation_dt.isoformat() if struct.creation_dt else "",
            "mod_iso": struct.mod_dt.isoformat() if struct.mod_dt else "",
            "xmp_present": struct.xmp_present,
            "doc_id_permanent": struct.doc_id_permanent,
            "doc_id_changing": struct.doc_id_changing,
            "fonts": struct.fonts,
            "images": struct.images,
            "eof_count": struct.eof_count,
            "annotation_count": struct.annotation_count,
            "js_present": struct.js_present,
        },
        "extracted": extraction.as_dict(),
        "image_forensics": _img_forensics_dict(img_forensics),
    }
    return report


def _img_forensics_dict(f: ImageForensics | None) -> dict:
    if f is None or not f.analyzed:
        return {"analyzed": False}
    return {
        "analyzed": True,
        "width": f.width, "height": f.height, "format": f.format, "mode": f.mode,
        "has_exif": f.has_exif, "exif_software": f.exif_software,
        "exif_make": f.exif_make, "exif_model": f.exif_model, "exif_datetime": f.exif_datetime,
        "ai_signature_hits": f.ai_signature_hits, "edit_signature_hits": f.edit_signature_hits,
        "c2pa_present": f.c2pa_present,
        "ela_mean": round(f.ela_mean, 2), "ela_hotspot_ratio": round(f.ela_hotspot_ratio, 4),
        "jpeg_quality_est": f.jpeg_quality_est, "noise_inconsistency": round(f.noise_inconsistency, 3),
        "manipulation_score": round(f.manipulation_score, 1), "ai_score": round(f.ai_score, 1),
        "ela_preview_b64": f.ela_preview_b64,
        "signals": f.signals,
    }


_DOCTYPE_TR = {
    "digital_native": "Dijital (gerçek PDF, metin içerikli)",
    "hybrid": "Karma (metin + görsel)",
    "scanned": "Taranmış belge (tek görsel)",
    "image_only": "Yalnızca görsel (fotoğraf/ekran görüntüsü)",
}
_DOCTYPE_EN = {
    "digital_native": "Digital-native PDF (with text)",
    "hybrid": "Hybrid (text + image)",
    "scanned": "Scanned document (single image)",
    "image_only": "Image-only (photo/screenshot)",
}

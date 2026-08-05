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


_IMAGE_MAGIC = {
    b"\xff\xd8\xff": "jpg", b"\x89PNG\r\n\x1a\n": "png", b"GIF8": "gif",
    b"BM": "bmp", b"II*\x00": "tiff", b"MM\x00*": "tiff", b"RIFF": "webp",
}


def _vnum(x):
    """Vision'dan gelen tutarı float'a çevirir (metin/None dahil)."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace(" ", "")
    if not s:
        return None
    # "5.000,00" veya "5000.00" veya "5,000.00" biçimlerini tolere et
    import re as _re
    s = _re.sub(r"[^\d.,-]", "", s)
    if "," in s and "." in s:
        # son ayraç ondalık
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def _apply_vision(ex, v: dict) -> None:
    """Vision modelinin döndürdüğü alanları Extraction'a uygular (görselde vision önceliklidir)."""
    import re as _re
    def _s(key):
        val = v.get(key)
        return str(val).strip() if val not in (None, "") else ""
    def _iban(key):
        raw = _s(key)
        return _re.sub(r"\s+", "", raw).upper() if raw else ""

    if _s("bank"):
        ex.bank = _s("bank")
    # Gönderen
    if _s("sender_name"):
        ex.sender.name = _s("sender_name")
    if _iban("sender_iban"):
        ex.sender.iban = _iban("sender_iban")
    # Alıcı
    if _s("receiver_name"):
        ex.receiver.name = _s("receiver_name")
    if _iban("receiver_iban"):
        ex.receiver.iban = _iban("receiver_iban")
    if _s("receiver_bank"):
        ex.receiver.bank = _s("receiver_bank")
    # Tutar
    av = _vnum(v.get("amount"))
    if av is not None:
        ex.amount.value = av
        if _s("amount_currency"):
            ex.amount.currency = _s("amount_currency").upper()
    fv = _vnum(v.get("fee"))
    if fv is not None:
        ex.amount.fee = fv
    tv = _vnum(v.get("total"))
    if tv is not None:
        ex.amount.total = tv
    # İşlem
    for src, dst in (("date", "date"), ("ref_no", "ref_no"), ("document_no", "document_no"),
                     ("type", "type"), ("channel", "channel"), ("description", "description")):
        if _s(src):
            setattr(ex.transaction, dst, _s(src))
    # IBAN'dan banka tamamla
    if not ex.receiver.bank and ex.receiver.iban:
        try:
            import banks
            ex.receiver.bank = banks.bank_label_from_iban(ex.receiver.iban)
        except Exception:
            pass
    # all_ibans güncelle
    for ib in (ex.sender.iban, ex.receiver.iban):
        if ib and ib not in ex.all_ibans:
            ex.all_ibans.append(ib)


def prepare_input(data: bytes, filename: str = "") -> tuple[bytes, str]:
    """
    Yüklenen veriyi analiz için hazırlar. PDF ise dokunmaz. Görsel (JPG/PNG/...) ise
    KAYIPSIZ olarak tek sayfalık PDF'e sarar (img2pdf) ki ELA/EXIF gibi görsel adli
    analizler orijinal görsel üzerinde çalışsın. Döndürür: (pdf_bytes, input_kind).
    """
    if data[:5] == b"%PDF-" or (filename or "").lower().endswith(".pdf"):
        return data, "pdf"
    ext = (filename or "").lower().rsplit(".", 1)[-1] if "." in (filename or "") else ""
    is_img = ext in ("jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp") or \
             any(data.startswith(sig) for sig in _IMAGE_MAGIC)
    if not is_img:
        raise ValueError("unsupported_type")
    # Kayıpsız sar
    try:
        import img2pdf
        return img2pdf.convert(data), "image"
    except Exception:
        # Yedek: PIL ile normalize edip sar
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        buf = io.BytesIO(); im.save(buf, "PDF", resolution=150.0)
        return buf.getvalue(), "image"


def analyze_document(pdf_bytes: bytes, filename: str = "", input_kind: str = "pdf",
                     use_store: bool = True) -> dict:
    # input_kind: "pdf" (yüklenen PDF) veya "image" (doğrudan yüklenen fotoğraf)
    # use_store: True ise kalıcı numara veritabanı ile karşılaştır ve doğrulanmışsa kaydet
    t0 = time.time()

    # 1) Yapı / metadata
    struct = analyze_structure_bytes(pdf_bytes)

    # 2) Metin (dijital), yetersizse OCR
    text_layout = extract_text_digital(pdf_bytes, layout=True)
    text_read = extract_text_digital(pdf_bytes, layout=False)
    digital_text_len = len((text_layout or "").strip())   # SADECE dijital metin
    struct.text_char_count = digital_text_len
    text_source = "digital"
    ocr_candidates = []
    if digital_text_len < 40:
        ocr_candidates = ocr.ocr_pdf_candidates(pdf_bytes) if ocr.ocr_available() else []
        if ocr_candidates:
            text_layout = text_read = max(ocr_candidates, key=len)
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

    # 5) Alan çıkarımı (geometrik çıkarım için pdf_bytes de verilir)
    extraction = extract_fields(text_layout, text_read, pdf_bytes if text_source == "digital" else None)
    # OCR: birden çok varyanttan alanları birleştir (kötü fotoğraf dayanıklılığı)
    if text_source == "ocr":
        from extract import merge_extractions, ocr_recover
        if len(ocr_candidates) > 1:
            others = [extract_fields(c, c, None) for c in ocr_candidates if c != text_layout]
            extraction = merge_extractions(extraction, others)
        # Bozuk OCR metninden banka kodu / IBAN / rakam dizilerini kurtar
        for c in ocr_candidates:
            ocr_recover(extraction, c)
    extraction.text_source = text_source

    # 5.5) VISION AI: ÖNCE ücretsiz tesseract çalışır; yalnızca ZORUNLU 4 kritik alandan
    # (alıcı adı, alıcı IBAN, tutar, işlem tarihi) EN AZ BİRİ okunamazsa ücretli Vision'a git.
    # Dördü de okunduysa (kaliteli foto) ücretli servise hiç gidilmez → maliyet tasarrufu.
    vision_result = None
    if text_source in ("ocr", "none"):
        import vision_ocr
        _crit_required = (extraction.receiver.name, extraction.receiver.iban,
                          extraction.amount.value, extraction.transaction.date)
        _missing = [1 for v in _crit_required if not v]
        if vision_ocr.is_configured() and len(_missing) >= 1:
            try:
                _pil = ocr.render_page_to_image(pdf_bytes, 0, scale=2.0)
                vision_result = vision_ocr.extract_from_image(_pil)
            except Exception:
                vision_result = None
            if vision_result:
                _apply_vision(extraction, vision_result)
                extraction.text_source = "vision"
                # sıra numarasını vision sonrası tazele
                from extract import derive_sequence_number as _dsn
                extraction.transaction.sequence_number = _dsn(extraction)

    # Sıra/işlem numarası (banka bazlı) — sıra analizi için
    from extract import derive_sequence_number
    extraction.transaction.sequence_number = derive_sequence_number(extraction)

    # --- Bu bir dekont mu? (özellikle görsel/OCR girdilerinde) ---
    from extract import receipt_content_score
    rc_score = receipt_content_score(text_layout, extraction)
    is_receipt = (doc_type in ("digital_native", "hybrid")) or rc_score >= 0.30
    # Vision modeli bir görüşe sahipse onu dikkate al (görselde daha güvenilir)
    if vision_result is not None:
        _vr = vision_result.get("is_receipt")
        _has_fields = bool(extraction.receiver.iban or extraction.sender.name or
                           extraction.receiver.name or extraction.amount.value)
        if _vr is True or _has_fields:
            is_receipt = True
        elif _vr is False and not _has_fields:
            is_receipt = False

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

    # 6.5) İLERİ ANALİZLER: revizyon karşılaştırması, QR, gömülü XML, veri tutarlılığı
    from forensics import Finding
    import revision as _rev, qrxml as _qx, consistency as _cons

    ex = extraction
    # --- Revizyon karşılaştırması (artımlı güncellemeli belgeler) ---
    rev = _rev.compare_revisions(pdf_bytes)
    if rev["has_prior"] and rev["changes"]:
        crit = [c for c in rev["changes"] if c["severity"] == "kritik"]
        amount_changed = any(c["field"] == "amount" for c in crit)
        if amount_changed:
            findings.append(Finding(
                "REV_AMOUNT_CHANGED", "critical", "structure", 45,
                tr="TUTAR, PDF revizyonları arasında DEĞİŞTİRİLMİŞ. Önceki sürümdeki parasal değer "
                   "son sürümde farklı — bu, belge üzerinde doğrudan tutar oynaması demektir.",
                en="The AMOUNT was changed between PDF revisions — direct monetary tampering.",
                detail="; ".join(f"{c['label']}: {c['prev']} -> {c['curr']}" for c in crit if c["field"] == "amount")))
        findings.append(Finding(
            "REV_CONTENT_CHANGED", "critical", "structure", 35,
            tr=f"Eski revizyon ile son görünüm FARKLI: {len(crit)} kritik alan sonradan değiştirilmiş. "
               f"PDF'in önceki sürümündeki metinler değiştirilerek üzerine kaydedilmiş.",
            en=f"Old revision differs from final: {len(crit)} critical field(s) altered after creation.",
            detail="; ".join(f"{c['label']}: {c['prev']} -> {c['curr']}" for c in rev["changes"])))
        for c in crit:
            if c["field"] == "amount":
                continue
            findings.append(Finding(
                "REV_FIELD_CHANGED", "high", "content", 12,
                tr=f"Kritik alan revizyonlar arasında değişmiş — {c['label']}: '{c['prev']}' → '{c['curr']}'.",
                en=f"Critical field changed across revisions — {c['label']}: '{c['prev']}' -> '{c['curr']}'.",
                detail=""))

    # --- QR kod tespiti + karşılaştırma ---
    qr = _qx.detect_qr(pdf_bytes) if doc_type != "none" else {"found": False, "count": 0, "payloads": []}
    qr_check = {}
    if qr["found"]:
        qr_check = _qx.cross_check_qr(qr["payloads"], ex.sender.iban, ex.receiver.iban, ex.amount.value)
        if qr_check.get("iban_match") is False or qr_check.get("amount_match") is False:
            findings.append(Finding(
                "QR_MISMATCH", "high", "content", 30,
                tr="QR koddaki bilgiler, dekont üzerinde görünen alanlarla UYUŞMUYOR (IBAN/tutar farklı). "
                   "Bu, görünen metnin sonradan değiştirildiğine güçlü işaret olabilir.",
                en="QR-code data does not match the visible fields (IBAN/amount) — strong tamper signal.",
                detail=str(qr_check)))
        elif qr_check.get("iban_match") or qr_check.get("amount_match"):
            findings.append(Finding(
                "QR_MATCH", "info", "content", -8,
                tr="QR koddaki bilgiler görünen alanlarla tutarlı (doğrulayıcı).",
                en="QR-code data is consistent with the visible fields (corroborating).", detail=""))

    # --- Gömülü XML (e-dekont/GİB) ---
    xml = _qx.detect_embedded_xml(pdf_bytes)

    # --- Tarih/saat derin analizi ---
    import timing as _tim
    is_aem = any(g in ("adobe experience manager", "adobe livecycle")
                 for g in classify_producer(struct.producer, struct.creator)["generator_hits"])
    # Doğrudan fotoğraf yüklemesinde PDF oluşturma/değiştirme zamanı = yükleme anı (anlamsız);
    # işlem↔üretim kıyaslaması yapılmaz (yanlış "geç üretim" sinyalini önler).
    if input_kind == "image":
        timing = _tim.analyze_timing(None, None, ex.transaction.date, is_aem)
    else:
        timing = _tim.analyze_timing(struct.creation_dt, struct.mod_dt, ex.transaction.date, is_aem)
    for tf in timing["findings"]:
        findings.append(Finding(tf["code"], tf["severity"], "metadata", tf["weight"],
                                tr=tf["tr"], en=tf["en"], detail=tf.get("detail", "")))

    # --- Veri tutarlılığı ---
    cons = _cons.check_consistency(
        ex.amount.value, ex.amount.fee, ex.amount.total,
        _rev._find_bsmv(text_layout or ""), _rev._find_amount_words(text_layout or ""))
    for c in cons["checks"]:
        if not c["ok"]:
            findings.append(Finding(
                "CONSISTENCY_FAIL", "medium", "content", 15,
                tr=f"Veri tutarlılığı hatası — {c['name']}: {c['detail']}. Alanlardan biri elle değiştirilmiş olabilir.",
                en=f"Data consistency failure — {c['name']}: {c['detail']}.", detail=""))

    # --- Bu bir dekont değil ---
    if not is_receipt and extraction.text_source in ("ocr", "none", "vision"):
        findings.append(Finding(
            "NOT_A_RECEIPT", "critical", "content", 0,
            tr="BU DOSYA BİR BANKA DEKONTU DEĞİLDİR. Yüklenen görselde dekont içeriği (banka adı, IBAN, tutar, "
               "gönderen/alıcı, işlem/referans numarası) tespit EDİLEMEDİ. Bu dosya bir dekont değil ya da görüntü "
               "kalitesi okunamayacak kadar düşük olabilir.",
            en="THIS FILE IS NOT A BANK RECEIPT. No receipt content (bank name, IBAN, amount, sender/receiver, "
               "transaction/reference number) was detected in the uploaded image. This file is not a receipt, or the "
               "image quality is too low to read.",
            detail=f"receipt_score={rc_score}"))

    # --- Düşük görüntü kalitesi: dekont ama kritik alanlar okunamadı ---
    if is_receipt and extraction.text_source in ("ocr", "vision"):
        _crit_missing = [nm for nm, val in (
            ("gönderen adı", ex.sender.name), ("alıcı adı", ex.receiver.name),
            ("alıcı IBAN", ex.receiver.iban), ("tutar", ex.amount.value)) if not val]
        _crit_missing_en = [nm for nm, val in (
            ("sender name", ex.sender.name), ("receiver name", ex.receiver.name),
            ("receiver IBAN", ex.receiver.iban), ("amount", ex.amount.value)) if not val]
        if len(_crit_missing) >= 2:
            findings.append(Finding(
                "LOW_IMAGE_QUALITY", "medium", "content", 0,
                tr="Görüntü bir dekont olarak tanındı ancak bulanıklık/düşük çözünürlük nedeniyle bazı kritik "
                   f"alanlar OCR ile güvenilir biçimde okunamadı ({', '.join(_crit_missing)}). Daha net, iyi "
                   "aydınlatılmış ve düz çekilmiş bir fotoğraf ya da mümkünse orijinal dijital PDF yükleyin. "
                   "Yanlış okuma riski taşımamak için okunamayan alanlar boş bırakılmıştır.",
                en="The image was recognized as a receipt, but blur/low resolution prevented reliable OCR of some "
                   f"critical fields ({', '.join(_crit_missing_en)}). Please upload a sharper, well-lit, flat photo, "
                   "or the original digital PDF if available. Unreadable fields are left blank to avoid misreads.",
                detail=f"missing={_crit_missing_en}"))

    # --- Tek fotoğraftan oluşan PDF => KRİTİK (doğrudan foto/ dekont-değil hariç) ---
    if input_kind == "pdf" and is_receipt and doc_type in ("image_only", "scanned"):
        findings.append(Finding(
            "SINGLE_PHOTO_PDF", "critical", "content", 45,
            tr="Yüklenen PDF, seçilebilir hiçbir veri içermeyen TEK BİR FOTOĞRAFTAN/GÖRSELDEN oluşuyor. "
               "Gerçek banka dekontları dijital metin katmanı içerir; tek bir fotoğrafı PDF'e koymak, "
               "orijinal dijital dekont yerine düzenlenebilir bir görsel sunulduğunu gösterir. Yüksek sahtecilik riski.",
            en="The uploaded PDF consists of a SINGLE PHOTO/IMAGE with no selectable data. Genuine bank receipts "
               "contain a digital text layer; wrapping a single photo in a PDF indicates an editable image was "
               "presented instead of the original digital receipt. High forgery risk.",
            detail=f"doc_type={doc_type}"))

    # 6.9) KALICI VERİTABANI KARŞILAŞTIRMASI (banka bazlı numara geçmişi)
    db_findings: list = []
    if use_store and is_receipt:
        try:
            import store as _store  # noqa
            _pre = {"extracted": extraction.as_dict(), "file": {"sha256": struct.sha256},
                    "score": {}, "classification": {}}
            db_findings = _store.check(_pre)
            for df in db_findings:
                _w = 45 if df["code"] == "SEQ_DB_DUPLICATE" else 8
                findings.append(Finding(df["code"], df["severity"], "content", _w,
                                        tr=df["tr"], en=df["en"], detail=df.get("detail", "")))
        except Exception:
            db_findings = []

    # 7) Skorlama
    score = compute_score(findings, doc_type, manip, ai)

    # 7.5) Alt-skorlar (Dekont Guard tarzı)
    subscores = _compute_subscores(struct, rev, cons, xml, qr_check, findings, doc_type)

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
            "text_source": extraction.text_source,
            "vision_used": extraction.text_source == "vision",
            "is_digital_pdf": doc_type in ("digital_native", "hybrid"),
            "is_image_only": doc_type in ("image_only", "scanned"),
            "is_receipt": is_receipt,
            "receipt_score": rc_score,
            "input_kind": input_kind,
        },
        "score": {
            "authenticity_score": score.authenticity_score,
            "max_possible": score.max_possible,
            "risk_level": score.risk_level,
            "not_a_receipt": score.not_a_receipt,
            "penalties_total": score.penalties_total,
            "bonuses_total": score.bonuses_total,
            "breakdown": score.breakdown,
            "verdict_tr": score.verdict_tr,
            "verdict_en": score.verdict_en,
        },
        "subscores": subscores,
        "revision": {
            "revision_count": rev["revision_count"],
            "has_prior": rev["has_prior"],
            "changes": rev["changes"],
            "critical_count": rev["critical_count"],
            "supporting_count": rev["supporting_count"],
        },
        "qr": {
            "found": qr["found"], "count": qr["count"],
            "payloads": [p[:200] for p in qr.get("payloads", [])],
            "check": qr_check,
        },
        "embedded_xml": {
            "present": xml["present"], "looks_like_dekont": xml["looks_like_dekont"],
            "has_embedded_files": xml["has_embedded_files"],
        },
        "consistency": cons,
        "timing": {
            "timeline": timing["timeline"],
            "transaction_local": timing["transaction_local"],
            "creation_local": timing["creation_local"],
            "mod_local": timing["mod_local"],
            "gaps": timing["gaps"],
        },
        "input_kind": input_kind,
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
        "cross_db": {
            "checked": bool(use_store and is_receipt),
            "matches": db_findings,
            "recorded": False,
        },
    }

    # 9) Doğrulanmışsa numaralarını kalıcı veritabanına kaydet (banka bazlı)
    if use_store:
        try:
            import store as _store
            report["cross_db"]["recorded"] = _store.record(report)
        except Exception:
            pass
    return report


def _compute_subscores(struct, rev, cons, xml, qr_check, findings, doc_type) -> dict:
    """Dekont Guard tarzı üç alt-skor: bütünlük, veri tutarlılığı, kaynak doğrulanabilirliği."""
    cp = classify_producer(struct.producer, struct.creator)
    rev_crit = rev.get("critical_count", 0) if rev.get("has_prior") else 0

    # Belge bütünlüğü
    integrity = 100
    if rev_crit > 0:
        integrity -= 85
    elif struct.has_incremental_updates:
        integrity -= 18
    if cp["editor_hits"] and cp["generator_hits"]:
        integrity -= 30
    if cp["append_mode"]:
        integrity -= 15
    integrity = max(0, min(100, integrity))

    # Veri tutarlılığı
    consistency = 100
    consistency -= cons.get("fail_count", 0) * 25
    consistency -= rev.get("critical_count", 0) * 20 + rev.get("supporting_count", 0) * 8
    consistency = max(0, min(100, consistency))

    # Kaynak doğrulanabilirliği
    source = 8
    if struct.has_signature:
        source += 45
    if xml.get("looks_like_dekont"):
        source += 32
    elif xml.get("present"):
        source += 12
    if qr_check.get("iban_match") or qr_check.get("amount_match"):
        source += 22
    source = max(0, min(100, source))

    return {"integrity": int(integrity), "data_consistency": int(consistency),
            "source_verifiability": int(source)}


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
        "bg_color": f.bg_color, "bg_dev_max": round(f.bg_dev_max, 1),
        "bg_dev_hotspot_ratio": round(f.bg_dev_hotspot_ratio, 4),
        "bg_patch_count": f.bg_patch_count, "bg_patch_max": f.bg_patch_max,
        "tone_chroma_var": round(f.tone_chroma_var, 1), "tone_cast": round(f.tone_cast, 1),
        "bg_heatmap_b64": f.bg_heatmap_b64,
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

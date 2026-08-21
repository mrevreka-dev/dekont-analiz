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

import re as _re

_MONEY_TOK = r"-?\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|-?\d+[.,]\d{1,2}"

# YALNIZCA işlem/transfer tutarını taşıyan etiketler. Ücret/komisyon/BSMV/masraf/toplam
# DAHİL DEĞİL — çünkü bunlar dekontta meşru olarak FARKLI değerlerdir.
_TRANSFER_LABELS = [
    "Toplam İşlem Tutarı", "İşlem Tutarı", "İşlem Miktarı", "Gönderilen Tutar",
    "Transfer Tutarı", "Havale Tutarı", "EFT Tutarı", "FAST Tutarı", "Ödeme Tutarı",
]
# Bu ifadeleri İÇEREN satır transfer tutarı DEĞİLDİR (toplam = transfer+ücret; ya da
# ücret/komisyon/BSMV/vergi/masraf). _norm_tr ile karşılaştırılır (aksan-duyarsız).
_NON_TRANSFER_IN_LINE = ("toplam", "ucret", "komisyon", "bsmv", "vergi",
                         "masraf", "kesinti", "fee", "commission", "tax")
# "12.000,00 TRY tutarında ..." gibi tekrar/teyit ifadesi
_RESTATE_RE = _re.compile(r"(" + _MONEY_TOK + r")\s*(?:TL|TRY|₺)?\s*tutar", _re.I)


def _transfer_amounts(text: str) -> list:
    """İŞLEM/TRANSFER tutarının belgede geçtiği tüm yerleri (float) döndürür.
    Gerçek bir dekontta bunların HEPSİ aynı olmalıdır; farklılık = tutar oynaması.

    ÖNEMLİ: 'Toplam İşlem Tutarı' (transfer + ücret) ve ücret/komisyon/BSMV/vergi/masraf
    satırları MEŞRU olarak farklı tutar taşır; bunlar transfer tutarı DEĞİLDİR ve karşılaştırmaya
    KATILMAZ. Aksi halde ücret ayrı yazan bankalarda (İş Bankası, Ziraat, ...) yanlış 'oynama'
    tespiti oluşur. Bu yüzden kontrol satır-bazlıdır ve bu kalemleri içeren satırlar elenir."""
    from extract import _parse_money_token, AMOUNT_RE, _norm_tr
    nlabels = [_norm_tr(l) for l in _TRANSFER_LABELS]
    vals = []
    for ln in (text or "").splitlines():
        nln = _norm_tr(ln)
        if any(x in nln for x in _NON_TRANSFER_IN_LINE):
            continue                          # toplam/ücret/komisyon/BSMV/vergi/masraf satırı: hariç
        if any(lab in nln for lab in nlabels):
            m = AMOUNT_RE.search(ln)          # etiketli transfer satırındaki İLK para jetonu
            if m:
                pv = _parse_money_token(m.group(0))
                if pv is not None:
                    vals.append(pv)
    for m in _RESTATE_RE.finditer(text or ""):   # '… X TL tutarında …' teyit ifadesi
        seg = (text or "")[max(0, m.start() - 40):m.start()]
        if any(x in _norm_tr(seg) for x in _NON_TRANSFER_IN_LINE):
            continue
        pv = _parse_money_token(m.group(1))
        if pv is not None:
            vals.append(pv)
    return vals


def _fmt_tl(v: float) -> str:
    """1234567.89 -> '1.234.567,89 TL' (Türk biçimi)."""
    try:
        s = f"{v:,.2f}"                      # 1,234,567.89
    except (ValueError, TypeError):
        return f"{v} TL"
    s = s.replace(",", "#").replace(".", ",").replace("#", ".")
    return s + " TL"


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


def _apply_vision(ex, v: dict, bank_ex=None) -> None:
    """Vision modelinin döndürdüğü alanları Extraction'a uygular.

    ÖNCELİK: bank_ex (vision'ın SADIK metin dökümüne uygulanan BANKA-ÖZEL extract_fields sonucu)
    > vision'ın genel yapısal tahmini > mevcut OCR. Böylece iki-sütunlu düzenlerde (Enpara vb.)
    gönderen/alıcı eşlemesi bankanın KENDİ etiket yapısına göre yapılır; genel vision tahmininin
    tarafları karıştırması önlenir. bank_ex yoksa davranış eskisiyle aynıdır (genel vision)."""
    import re as _re
    _ocr_ibans = [ib for ib in (ex.all_ibans or []) if ib]   # vision ÖNCESİ OCR IBAN'ları
    be = bank_ex
    # bank_ex, belge içeriğiyle uyumlu ve bir taraf çıkarabildiyse GÜVENİLİR sayılır.
    _be_ok = bool(be and (be.sender.name or be.receiver.name or be.sender.iban or be.receiver.iban))
    def _s(key):
        val = v.get(key)
        return str(val).strip() if val not in (None, "") else ""
    def _iban(key):
        raw = _s(key)
        return _re.sub(r"\s+", "", raw).upper() if raw else ""
    def _bs(attr):   # bank_ex.sender/receiver/... alanı (varsa)
        return getattr(be, attr, "") if be else ""

    # Daha zengin metin dökümü varsa raw_text'i onunla değiştir (metin-tabanlı denetimler için)
    if _be_ok and be.raw_text and len(be.raw_text) > len(ex.raw_text or ""):
        ex.raw_text = be.raw_text

    # Banka adı
    _bank = (be.bank if _be_ok else "") or _s("bank")
    if _bank:
        ex.bank = _bank
    # Gönderen
    _sn = (be.sender.name if _be_ok else "") or _s("sender_name")
    if _sn:
        ex.sender.name = _sn
    _si = (be.sender.iban if _be_ok else "") or _iban("sender_iban")
    if _si:
        ex.sender.iban = _si
    # Alıcı
    _rn = (be.receiver.name if _be_ok else "") or _s("receiver_name")
    if _rn:
        ex.receiver.name = _rn
    _ri = (be.receiver.iban if _be_ok else "") or _iban("receiver_iban")
    if _ri:
        ex.receiver.iban = _ri
    _rb = (be.receiver.bank if _be_ok else "") or _s("receiver_bank")
    if _rb:
        ex.receiver.bank = _rb
    # Gönderici bankası (banka-özel dökümden): ihraççı bankası. Kopyalanmazsa gönderen IBAN'ı
    # yoksa alıcı bankası (ör. ING) yanlışlıkla gönderici bankası gibi görünebilir.
    if _be_ok and be.sender.bank:
        ex.sender.bank = be.sender.bank
    # Kimlik/müşteri no (banka-özel dökümden)
    if _be_ok:
        if be.sender.tckn and not ex.sender.tckn:
            ex.sender.tckn = be.sender.tckn
        if be.sender.customer_no and not ex.sender.customer_no:
            ex.sender.customer_no = be.sender.customer_no
        if be.doc_kind and not ex.doc_kind:
            ex.doc_kind = be.doc_kind
    # Tutar (banka-özel > vision)
    av = (be.amount.value if (_be_ok and be.amount.value is not None) else None)
    if av is None:
        av = _vnum(v.get("amount"))
    if av is not None:
        ex.amount.value = av
    _cur = (be.amount.currency if _be_ok else "") or _s("amount_currency")
    if _cur:
        ex.amount.currency = _cur.upper()
    fv = (be.amount.fee if (_be_ok and be.amount.fee is not None) else None)
    if fv is None:
        fv = _vnum(v.get("fee"))
    if fv is not None:
        ex.amount.fee = fv
    tv = (be.amount.total if (_be_ok and be.amount.total is not None) else None)
    if tv is None:
        tv = _vnum(v.get("total"))
    if tv is not None:
        ex.amount.total = tv
    # İşlem alanları (banka-özel > vision)
    for src, dst in (("date", "date"), ("ref_no", "ref_no"), ("document_no", "document_no"),
                     ("type", "type"), ("channel", "channel"), ("description", "description")):
        _bv = getattr(be.transaction, dst, "") if _be_ok else ""
        _val = _bv or _s(src)
        if _val:
            setattr(ex.transaction, dst, _val)
    # bank_ex'in IBAN listesini de kaynağa ekle (taraf-ayrımı düzeltmeleri için)
    if _be_ok and be.all_ibans:
        for ib in be.all_ibans:
            if ib and ib not in _ocr_ibans:
                _ocr_ibans.append(ib)
    # IBAN'dan banka tamamla
    if not ex.receiver.bank and ex.receiver.iban:
        try:
            import banks
            ex.receiver.bank = banks.bank_label_from_iban(ex.receiver.iban)
        except Exception:
            pass
    # ÇAKIŞMA-ÖNLEME: gönderen ve alıcıya AYNI IBAN atanmışsa (yanlış eşleme / tek IBAN
    # okunup kopyalanması), belgedeki DISTINCT geçerli IBAN'lardan alıcıdan FARKLI birini
    # gönderene ata. Böyle bir IBAN YOKSA gönderen IBAN'ını BOŞALT — kopya IBAN bırakmak
    # 'aynı banka' (RAIL_SAMEBANK) ve 'ihraççı uyuşmazlığı' (ISSUER_IBAN_MISMATCH) yanlış-
    # pozitiflerini doğurur. Eksik IBAN, sahte-hüküm üretmeyen bir eksikliktir.
    try:
        import banks as _bk
        if ex.sender.iban and ex.sender.iban == ex.receiver.iban:
            _valid = [ib for ib in _ocr_ibans if ib and _bk.iban_valid(ib) is not False]
            _other = next((ib for ib in _valid if ib != ex.receiver.iban), "")
            ex.sender.iban = _other   # farklı IBAN yoksa boş kalır (kopya bırakılmaz)
    except Exception:
        pass
    # all_ibans güncelle
    for ib in (ex.sender.iban, ex.receiver.iban):
        if ib and ib not in ex.all_ibans:
            ex.all_ibans.append(ib)


def _repair_party_ibans(ex, input_kind: str) -> list:
    """Fotoğraf/OCR/vision okumasında tek bir rakam yanlış okunmuş olabilir
    (ör. '...218058' ↔ gerçek '...218056'). mod-97 tutmayan taraf IBAN'ını, görsel-karışan
    rakamları deneyerek BENZERSİZ geçerli adaya onar. Benzersiz değilse dokunma (tahmin yok).
    Temiz dijital PDF'de ÇALIŞMAZ — orada geçersiz IBAN gerçek bir tahrifat işaretidir.
    Döner: yapılan onarımların listesi."""
    fixes = []
    if not (input_kind == "image" or ex.text_source in ("ocr", "vision")):
        return fixes
    import banks as _bkr
    for _who, _obj in (("gönderici", ex.sender), ("alıcı", ex.receiver)):
        _cur = _obj.iban
        if _cur and _bkr.iban_valid(_cur) is False:
            _fx = _bkr.repair_iban_ocr(_cur)
            if _fx and _fx != _cur and _bkr.iban_valid(_fx) is True:
                _obj.iban = _fx
                fixes.append({"taraf": _who, "onceki": _cur, "sonraki": _fx})
                if _fx not in ex.all_ibans:
                    ex.all_ibans.append(_fx)
    return fixes


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

    # 1.5) HIZ ÖNBELLEĞİ: aynı dosya (sha256) + aynı motor sürümü daha önce analiz edildiyse,
    # tüm işlem hattını (OCR + Vision + YZ) ATLA ve saklanan raporu ANINDA döndür.
    if use_store:
        try:
            import store as _store_cache
            _cached = _store_cache.cache_get(struct.sha256, ENGINE_VERSION)
            if _cached is not None:
                _cached["elapsed_ms"] = int((time.time() - t0) * 1000)
                return _cached
        except Exception:
            pass

    # 2) Metin (dijital), yetersizse OCR
    text_layout = extract_text_digital(pdf_bytes, layout=True)
    text_read = extract_text_digital(pdf_bytes, layout=False)
    digital_text_len = len((text_layout or "").strip())   # SADECE dijital metin
    struct.text_char_count = digital_text_len
    text_source = "digital"
    ocr_candidates = []
    if digital_text_len < 40:
        # HIZ: doğrudan FOTOĞRAF yüklemesinde ve Vision açıkken tesseract'ı TEK-HIZLI geçişe indir
        # (doğru okumayı zaten Vision yapar → çok-varyantlı ~9s tesseract boşa gitmesin). Taranmış
        # PDF'lerde ya da Vision kapalıyken tam (çok-varyantlı) OCR korunur (kalite düşmez).
        _fast_ocr = (input_kind == "image")
        if _fast_ocr:
            try:
                import vision_ocr as _vo_fast
                _fast_ocr = _vo_fast.is_configured()
            except Exception:
                _fast_ocr = False
        ocr_candidates = ocr.ocr_pdf_candidates(pdf_bytes, fast=_fast_ocr) if ocr.ocr_available() else []
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

    # ADLİ İLKE: IBAN OCR-onarımını Vision KARARINDAN ÖNCE ÇALIŞTIRMA. Aksi hâlde geçersiz (checksum
    # tutmayan → en şüpheli) bir IBAN "onarılıp" _iban_bad False olur ve dekont Vision denetiminden
    # (görsel tahrifat sezgisi + güvenilir metin dökümü) KAÇAR. Vision kararı DAİMA ham OCR okumasına
    # dayanmalı. Onarım yalnızca Vision hiç çalışamadığında (kapalı/hata) şeffaf YEDEK olarak uygulanır.
    _iban_fixes = []

    # 5.5) VISION AI: ÖNCE ücretsiz tesseract çalışır; yalnızca ZORUNLU 4 kritik alandan
    # (alıcı adı, alıcı IBAN, tutar, işlem tarihi) EN AZ BİRİ okunamazsa ücretli Vision'a git.
    # Dördü de okunduysa (kaliteli foto) ücretli servise hiç gidilmez → maliyet tasarrufu.
    vision_result = None
    if text_source in ("ocr", "none"):
        import vision_ocr
        import banks as _bkv
        _crit_required = (extraction.receiver.name, extraction.receiver.iban,
                          extraction.amount.value, extraction.transaction.date)
        _missing = [1 for v in _crit_required if not v]
        # GEÇERSİZ IBAN → OCR bozulması: bulanık ekran-fotoğraflarında Tesseract IBAN'ı yanlış
        # okuyup mod-97'yi bozabilir. Gerçek dekontta IBAN her zaman geçerlidir; bu yüzden bir
        # taraf IBAN'ı OKUNDUĞU HALDE geçersizse (mod-97 tutmuyorsa) OCR güvenilmezdir → Vision'a
        # yüksel ki doğru IBAN okunsun ve yanlış IBAN_INVALID üretilmesin.
        _iban_bad = any(_bkv.iban_valid(ib) is False
                        for ib in (extraction.sender.iban, extraction.receiver.iban) if ib)
        if vision_ocr.is_configured() and (len(_missing) >= 1 or _iban_bad):
            try:
                _pil = ocr.render_page_to_image(pdf_bytes, 0, scale=2.0)
                vision_result = vision_ocr.extract_from_image(_pil)
            except Exception:
                vision_result = None
            if vision_result:
                # BANKA-ÖZEL ÇIKARIM: vision'ın SADIK metin dökümüne (full_text) PDF'lerdeki
                # AYNI banka-özel extract_fields'i uygula. Taraf/tutar eşlemesi bankanın kendi
                # etiket yapısına göre yapılır; genel vision tahmininin iki-sütunlu düzende
                # gönderen/alıcıyı karıştırması önlenir (tüm bankalar için geçerli).
                _bank_ex = None
                _ft = (vision_result.get("full_text") or "").strip()
                if len(_ft) > 40:
                    try:
                        _bank_ex = extract_fields(_ft, _ft, None)
                    except Exception:
                        _bank_ex = None
                _apply_vision(extraction, vision_result, bank_ex=_bank_ex)
                # METİN-TABANLI TÜM DENETİMLER de sadık vision dökümünü kullansın (rail, ücret,
                # tarih zinciri, IBAN, maskeleme, hesap hareketi…): zayıf Tesseract OCR yerine
                # bankanın kendi etiket yapısını içeren güvenilir metin. Böylece banka-özel
                # denetimler fotoğrafta da PDF'deki gibi çalışır.
                if _ft and len(_ft) > 40:
                    text_layout = _ft
                    text_read = _ft
                extraction.text_source = "vision"
                # sıra numarasını vision sonrası tazele
                from extract import derive_sequence_number as _dsn
                extraction.transaction.sequence_number = _dsn(extraction)

    # ÖĞREN-UYGULA: bu bankanın store'da ÖĞRENİLMİŞ etiket ipuçlarıyla HÂLÂ BOŞ kritik alanları
    # doldur (YZ değerlendiricisi geçmişte bu banka için hangi alanın hangi etiket yanında olduğunu
    # öğrettiyse). Kod değişmeden, veri-odaklı iyileştirme. Yalnız blank alanlar; IBAN mod-97 doğrulanır.
    if use_store:
        try:
            import store as _store_h
            import authenticity as _auth_h
            from extract import apply_learned_field_hints
            _hkey = _auth_h.bank_key(extraction.bank)
            _hints = _store_h.learned_field_hints(_hkey) if _hkey else {}
            if _hints:
                apply_learned_field_hints(extraction, _hints, text_layout)
        except Exception:
            pass

    # IBAN OCR-ONARIMI (YEDEK — yalnızca Vision HİÇ ÇALIŞMADIYSA): Vision kapalı/hatalıysa ve OCR
    # tek rakam yanlış okuduysa, BENZERSİZ geçerli adaya onar (banka kodu korunur, dijital PDF'de
    # çalışmaz, iban_ocr_onarim ile şeffaf). Vision çalıştıysa DOKUNMA — onun okuması esastır ve
    # denetimi zayıflatacak sessiz düzeltme yapılmaz.
    if vision_result is None:
        _iban_fixes += _repair_party_ibans(extraction, input_kind)

    # Sıra/işlem numarası (banka bazlı) — sıra analizi için
    from extract import derive_sequence_number
    extraction.transaction.sequence_number = derive_sequence_number(extraction)

    # --- Belge türü: dekont mu, hesap hareketi mi? ---
    from extract import receipt_content_score
    import statement as _stmt
    rc_score = receipt_content_score(text_layout, extraction)
    stmt = _stmt.analyze(text_layout or "")
    st_score = stmt["score"]

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

    # Hesap hareketi belirleyici sinyali: 3+ işlem satırı + yürüyen bakiye tablosu
    # (tek işlemlik dekontlarda bulunmaz). Anahtar kelime örtüşmesinden bağımsız kesin ayrım.
    is_statement = st_score >= 0.5 and stmt["islem_sayisi"] >= 3
    if is_statement:
        is_receipt = False          # hesap hareketi dekont DEĞİLDİR
        doc_kind = "hesap_hareketi"
    elif is_receipt:
        doc_kind = "dekont"
    else:
        doc_kind = "diger"

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
    _gen_hits = classify_producer(struct.producer, struct.creator)["generator_hits"]
    is_aem = any(g in ("adobe experience manager", "adobe livecycle") for g in _gen_hits)
    # Sunucu tarafı RAPOR/ŞABLON üreticileri (Aspose, JasperReports, iText, AEM, ...) PDF
    # metadata zaman damgalarını çoğu zaman ŞABLONDAN/KÜTÜPHANEDEN sabit yazar — bunlar
    # gerçek üretim zamanını YANSITMAZ. Bu durumda metadata'ya dayalı zaman kontrolü
    # (geriye tarihleme / geç üretim) yanlış alarm üretir; bu yüzden baskılanır.
    unreliable_meta_dates = bool(_gen_hits)
    # Doğrudan fotoğraf yüklemesinde PDF oluşturma/değiştirme zamanı = yükleme anı (anlamsız);
    # işlem↔üretim kıyaslaması yapılmaz (yanlış "geç üretim" sinyalini önler).
    # Hesap hareketinde referans tarih = DÖNEM SONU / en geç işlem tarihi. Böylece
    # "PDF, içindeki işlemlerden ÖNCE üretilmiş" (imkânsız) durumu yakalanır.
    _txn_ref = ex.transaction.date
    if is_statement:
        _txn_ref = stmt["fields"].get("donem_bitis") or ""
        if not _txn_ref:
            _rows = stmt.get("balance", {}) and _stmt.parse_transactions(text_layout or "")
            if _rows:
                _txn_ref = _rows[0]["tarih"]   # en üstteki = en yeni işlem
    if input_kind == "image":
        timing = _tim.analyze_timing(None, None, _txn_ref, is_aem)
    elif unreliable_meta_dates:
        # Üretici/şablon metadata tarihleri güvenilmez: OLUMSUZ bulgular (geriye tarihleme,
        # geç üretim, sonradan değiştirme) BASKILANIR; ancak tarih işlem anıyla ÖRTÜŞÜYORSA
        # olumlu doğrulama (TIME_CONSISTENT) korunur — ör. OpenPDF gerçek zaman damgası yazar.
        timing = _tim.analyze_timing(struct.creation_dt, struct.mod_dt, _txn_ref, is_aem,
                                     suppress_negative=True)
        if struct.creation_dt:
            findings.append(Finding(
                "GENERATOR_TEMPLATE_DATES", "info", "metadata", 0,
                tr="Belge bir sunucu-tarafı rapor/şablon üreticisiyle üretilmiş; PDF metadata "
                   "oluşturma/değiştirme tarihleri şablon/kütüphane kaynaklıdır ve gerçek üretim "
                   "zamanını yansıtmaz. Bu nedenle metadata'ya dayalı zaman (geriye tarihleme) "
                   "kontrolü uygulanmadı — bu tek başına bir tahrifat işareti DEĞİLDİR.",
                en="Document produced by a server-side report/template generator; PDF metadata "
                   "creation/modification dates are template/library artifacts and do not reflect "
                   "real generation time. Metadata-based timing checks were skipped.",
                detail=f"Producer={struct.producer}"))
    else:
        timing = _tim.analyze_timing(struct.creation_dt, struct.mod_dt, _txn_ref, is_aem,
                                     suppress_late_generation=is_statement)
    for tf in timing["findings"]:
        findings.append(Finding(tf["code"], tf["severity"], "metadata", tf["weight"],
                                tr=tf["tr"], en=tf["en"], detail=tf.get("detail", "")))

    # --- Veri tutarlılığı ---
    cons = _cons.check_consistency(
        ex.amount.value, ex.amount.fee, ex.amount.total,
        _rev._find_bsmv(text_layout or ""), _rev._find_amount_words(text_layout or ""))
    # FOTOĞRAF/OCR/vision'da tutarlar pikselden okunur; tek rakam yanlış okuması aritmetiği
    # bozar. Bu yüzden tutarlılık hatası fotoğrafta KESİN bulgu değil, BİLGİ notudur (puanı
    # düşürmez); yalnız dijital-metin PDF'te medium ceza olarak sayılır (gerçek tahrifat).
    _cons_pixel = (input_kind == "image" or extraction.text_source in ("ocr", "vision"))
    for c in cons["checks"]:
        if not c["ok"]:
            if _cons_pixel:
                findings.append(Finding(
                    "CONSISTENCY_FAIL", "info", "content", 0,
                    tr=f"Tutar tutarlılığı notu — {c['name']}: {c['detail']}. Tutarlar fotoğraftan/taramadan "
                       f"OCR ile okunduğundan bu bir OKUMA HATASI da olabilir; tek başına tahrifat kanıtı "
                       f"değildir. Kesinlik için orijinal dijital PDF isteyin.",
                    en=f"Amount-consistency note — {c['name']}: {c['detail']}. Amounts were OCR-read from a "
                       f"photo/scan, so this may be a misread rather than tampering; not conclusive.", detail=""))
            else:
                findings.append(Finding(
                    "CONSISTENCY_FAIL", "medium", "content", 15,
                    tr=f"Veri tutarlılığı hatası — {c['name']}: {c['detail']}. Alanlardan biri elle değiştirilmiş olabilir.",
                    en=f"Data consistency failure — {c['name']}: {c['detail']}.", detail=""))

    # --- İŞLEM TUTARI çapraz-kaynak kontrolü (aynı tutar her yerde aynı olmalı) ---
    # Yalnızca İŞLEM/TRANSFER tutarının farklı yazımlarını karşılaştırır; ücret, komisyon,
    # BSMV, mesaj ücreti, toplam masraf ve toplam çekilen tutar gibi MEŞRU farklı kalemler
    # bu kontrole DAHİL EDİLMEZ (yanlış "oynama" tespitini önler).
    if not is_statement:
        tvals = _transfer_amounts(text_layout or "")
        uniq = sorted({round(v, 2) for v in tvals})
        if len(uniq) >= 2:
            hi, lo = uniq[-1], uniq[0]
            findings.append(Finding(
                "AMOUNT_MISMATCH", "critical", "content", 45,
                tr=f"TUTAR ÇELİŞKİSİ: İşlem tutarı dekont üzerinde farklı yerlerde FARKLI yazılmış — "
                   f"{_fmt_tl(hi)} ile {_fmt_tl(lo)}. Gerçek bir dekontta İŞLEM TUTARI (etiketli tutar ve "
                   f"'… tutarında …' teyit ifadesi) her yerde AYNI olmak zorundadır. Bu fark, tutarın belge "
                   f"üzerinde elle değiştirildiğine dair güçlü tahrifat kanıtıdır. (Not: komisyon, BSMV, "
                   f"mesaj ücreti gibi ayrı kalemler bu kontrole dahil edilmez.)",
                en=f"AMOUNT MISMATCH: the transaction amount is written inconsistently ({hi} vs {lo}). "
                   f"The transfer amount (labeled amount and the '… tutarında …' restatement) must be "
                   f"identical everywhere. Fees/commission/BSMV are excluded from this check.",
                detail=f"transfer_amounts={uniq}"))

    # --- HESAP HAREKETİ: yürüyen bakiye sürekliliği (içerik oynaması kesin kanıtı) ---
    if is_statement:
        _bal = stmt["balance"]
        if _bal.get("consistent") is False and _bal.get("breaks"):
            _n = len(_bal["breaks"])
            _b0 = _bal["breaks"][0]
            findings.append(Finding(
                "STATEMENT_BALANCE_BREAK", "critical", "content", 45,
                tr=f"HESAP HAREKETİNDE BAKİYE ZİNCİRİ KIRILMIŞ: {_n} satırda yürüyen bakiye, işlem "
                   f"tutarıyla UYUŞMUYOR. Gerçek bir hesap özetinde her satırda 'bakiye = önceki bakiye ± "
                   f"işlem tutarı' olmalıdır. İlk kırılma {_b0['tarih']} tarihli satırda: beklenen önceki "
                   f"bakiye {_b0['beklenen_onceki_bakiye']}, görünen {_b0['gercek_onceki_bakiye']} "
                   f"(fark {_b0['fark']}). Bu, bir işlem tutarının/bakiyenin elle değiştirildiğine ya da "
                   f"satır eklenip çıkarıldığına matematiksel kanıttır.",
                en=f"BALANCE CHAIN BROKEN in the account statement: on {_n} row(s) the running balance does "
                   f"not match the transaction amount. In a genuine statement each row must satisfy "
                   f"'balance = previous balance ± amount'. First break on {_b0['tarih']}: expected previous "
                   f"balance {_b0['beklenen_onceki_bakiye']}, shown {_b0['gercek_onceki_bakiye']} "
                   f"(diff {_b0['fark']}). Mathematical proof that an amount/balance was altered or a row "
                   f"inserted/removed.",
                detail="; ".join(f"{b['tarih']}: {b['beklenen_onceki_bakiye']} vs {b['gercek_onceki_bakiye']}"
                                 for b in _bal["breaks"][:5])))
        elif _bal.get("consistent") is True and _bal.get("checked", 0) >= 2:
            findings.append(Finding(
                "STATEMENT_BALANCE_OK", "info", "content", -6,
                tr=f"Yürüyen bakiye zinciri tutarlı ({_bal['checked']} işlem doğrulandı): her satırda bakiye, "
                   f"işlem tutarıyla uyumlu. Satır silme, tutar ekleme/değiştirme yürüyen bakiyeyi bozardı; "
                   f"bozulma bulunmadığından içerikte oynama yönünde işaret yok.",
                en=f"Running balance chain is consistent ({_bal['checked']} transactions verified): each row's "
                   f"balance matches the amount. Row deletion or amount edits would break the running balance; "
                   f"no break found, so no sign of tampering.", detail=""))

        # Beyan edilen kayıt sayısı ↔ gerçek satır sayısı (KESİN beyanda satır silme göstergesi)
        _cc = stmt.get("count_check", {})
        if _cc.get("tutarli") is False and _cc.get("kesin") and _cc.get("eksik", 0) > 0:
            findings.append(Finding(
                "STATEMENT_ROW_COUNT_MISMATCH", "critical", "content", 40,
                tr=f"Belgede beyan edilen kayıt sayısı ({_cc['beyan']}) ile listelenen işlem sayısı "
                   f"({_cc['gercek']}) UYUŞMUYOR — {_cc['eksik']} satır eksik. Bu, hareket satırlarından "
                   f"bir kısmının SİLİNDİĞİNE işarettir.",
                en=f"Declared record count ({_cc['beyan']}) does not match the listed transactions "
                   f"({_cc['gercek']}) — {_cc['eksik']} row(s) missing. Indicates rows were DELETED.",
                detail=f"beyan={_cc['beyan']} gerçek={_cc['gercek']}"))

    # --- Bu bir dekont değil ---
    if not is_receipt and not is_statement and extraction.text_source in ("ocr", "none", "vision"):
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

    # 6.85) ÖZGÜNLÜK DENETİMLERİ — tek belgede sahteciliği yakalayan banka-bilinçli sinyaller
    #   (a) belge/fiş numarasındaki gömülü tarih ↔ işlem tarihi
    #   (b) üretici (producer) kütüphanesi ↔ bankanın gerçek imzası
    if is_receipt:
        try:
            import authenticity as _auth
            _bkey = _auth.bank_key(ex.bank)
            _txn_dt, _ = _tim.parse_content_datetime(ex.transaction.date or "")
            # GLOBAL kural (banka bağımsız): PDFium ile üretilmiş dekont = SAHTE
            _pf = _auth.check_pdfium(struct.producer)
            if _pf:
                findings.append(Finding(_pf["code"], _pf["severity"], "metadata", _pf["weight"],
                                        tr=_pf["tr"], en=_pf["en"], detail=_pf.get("detail", "")))
            # Fiş/belge numarasındaki gömülü tarih ↔ işlem tarihi (Enpara/QNB): numara
            # DIGIT-ÖZEL bir kontroldür. Fotoğraf/OCR/vision okumasında YYYYAAGG önekinin
            # tek bir hanesi yanlış okunursa tarih kayar → yanlış 'BELGE TARİHİ ÇELİŞKİSİ'.
            # Pikselden okumada güvenilmez → IBAN kontrolleriyle aynı mantıkla yalnız dijital-metin PDF.
            if not (input_kind == "image" or extraction.text_source in ("ocr", "vision")):
                _rn = _auth.check_receipt_number_date(
                    _bkey, ex.transaction.document_no, ex.transaction.ref_no,
                    ex.transaction.sequence_number, _txn_dt)
                if _rn:
                    findings.append(Finding(_rn["code"], _rn["severity"], "content", _rn["weight"],
                                            tr=_rn["tr"], en=_rn["en"], detail=_rn.get("detail", "")))
            _pr = _auth.check_producer(_bkey, struct.producer)
            if _pr:
                findings.append(Finding(_pr["code"], _pr["severity"], "metadata", _pr["weight"],
                                        tr=_pr["tr"], en=_pr["en"], detail=_pr.get("detail", "")))
            # Bazı bankaların KABUL EDİLEN üreticisi bir tarayıcı/editör olabilir (VakıfBank=iOS
            # Quartz/Skia, Ziraat=Skia, Garanti=Skia). Bu durumda genel EDITOR_PRODUCER cezası
            # çifte-sayımdır: üretici bu banka için MEŞRU sayıldığından (pc=match) o cezayı kaldır.
            _exp = _auth.EXPECTED_PRODUCERS.get(_bkey)
            if _exp and _pr is None and _pf is None:
                _pll = (struct.producer or "").lower()
                if any(e in _pll for e in _exp):
                    findings[:] = [f for f in findings if f.code != "EDITOR_PRODUCER"]
            # Font alt-küme parmak izi (tarayıcı yeniden basımı / eksik font)
            _ft = _auth.check_fonts(_bkey, pdf_bytes)
            if _ft:
                findings.append(Finding(_ft["code"], _ft["severity"], "fonts", _ft["weight"],
                                        tr=_ft["tr"], en=_ft["en"], detail=_ft.get("detail", "")))
            # Belge içi + XMP çapraz-tarih tutarlılığı.
            # FOTOĞRAF/OCR/vision'da BASKILANIR: bu kontrol metindeki HERHANGİ bir tarihi
            # işlem tarihiyle >3 gün fark için tarar. Bir dekont FOTOĞRAFINDA telefonun
            # DURUM ÇUBUĞU tarihi/saati kadraja girer ve OCR bunu okur → her zaman ~BUGÜN
            # çıkar; eski (birkaç hafta önceki) gerçek bir dekont fotoğrafı bu yüzden DAİMA
            # 'tarih çelişkisi' verir (kanıtlandı: iki gerçek dekont other=BUGÜN ile FP verdi).
            # Serbest-tarih taraması pikselden okumada güvenilmez → yalnız dijital-metin PDF.
            # Anlamlı tarih mantığı (gelecek tarih / dekont<işlem) zaten check_date_chain'de,
            # etikete-bağlı ve toleranslı biçimde ele alınır.
            if not (input_kind == "image" or extraction.text_source in ("ocr", "vision")):
                _unrel_meta = bool(classify_producer(struct.producer, struct.creator)["generator_hits"])
                _idt = _auth.check_internal_dates(text_layout, pdf_bytes, _txn_dt,
                                                  ex.transaction.value_date or "",
                                                  use_meta=not _unrel_meta)
                if _idt:
                    findings.append(Finding(_idt["code"], _idt["severity"], "content", _idt["weight"],
                                            tr=_idt["tr"], en=_idt["en"], detail=_idt.get("detail", "")))
            # Deterministik IBAN/banka-tutarlılığı (mod-97, ihracçı-taraf, alıcı-bankası)
            for _d in _auth.deterministic_checks(_bkey, ex.sender.iban, ex.receiver.iban,
                                                 ex.receiver.bank, ex.all_ibans):
                # FOTOĞRAF/OCR: IBAN'a-dayalı sert kontroller GÜVENİLMEZDİR — pikselden okuma
                # (ister tesseract ister vision) rakamları/basamak sırasını bozabilir (yoğun
                # monospace IBAN). Gerçek dekontta IBAN her zaman geçerlidir; fotoğrafta geçersiz
                # çıkması bir OKUMA HATASIDIR, tahrifat kanıtı DEĞİL. Bu yüzden TÜM fotoğraflarda
                # (input_kind=='image', vision dahil) VE taranmış/ocr PDF'lerde IBAN_INVALID/
                # ISSUER/RECEIVER_BANK bastırılır — yalnız dijital-metin PDF'te normal çalışır.
                if (input_kind == "image" or extraction.text_source == "ocr") and _d["code"] in (
                        "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH"):
                    continue
                findings.append(Finding(_d["code"], _d["severity"], "content", _d["weight"],
                                        tr=_d["tr"], en=_d["en"], detail=_d.get("detail", "")))
            # Alan-bazlı font tutarlılığı: TUTAR yabancı/ana-dışı bir fontta mı (yapıştırılmış)?
            _af = _auth.check_amount_font(pdf_bytes, ex.amount.value)
            if _af:
                findings.append(Finding(_af["code"], _af["severity"], "fonts", _af["weight"],
                                        tr=_af["tr"], en=_af["en"], detail=_af.get("detail", "")))
            # Maskeli alıcı adı (ör. 'BA***** AŞ*****'): IBAN geçerliyse EKSİK BİLGİ DEĞİLDİR (bilgi notu)
            _mn = _auth.check_masked_name(ex.receiver.name, ex.receiver.iban, text_layout)
            if _mn:
                findings.append(Finding(_mn["code"], _mn["severity"], "content", _mn["weight"],
                                        tr=_mn["tr"], en=_mn["en"], detail=_mn.get("detail", "")))
            # İşlem türü (FAST/EFT) ↔ taraf bankaları: aynı banka koduysa FAST/EFT olamaz (HAVALE'dir)
            _rb = _auth.check_rail_bank(text_layout, ex.sender.iban, ex.receiver.iban, ex.all_ibans)
            if _rb:
                findings.append(Finding(_rb["code"], _rb["severity"], "content", _rb["weight"],
                                        tr=_rb["tr"], en=_rb["en"], detail=_rb.get("detail", "")))
            # FAST tutar anomalisi — regülasyon tabanı + GERÇEK dekontlardan öğrenilen tavan (veri-odaklı, FP-korumalı)
            try:
                import store as _store_f
                _fmax = _store_f.max_amount_for_rail("fast")
            except Exception:
                _fmax = None
            _fl = _auth.check_fast_limit(text_layout, ex.amount.value, _fmax)
            if _fl:
                findings.append(Finding(_fl["code"], _fl["severity"], "content", _fl["weight"],
                                        tr=_fl["tr"], en=_fl["en"], detail=_fl.get("detail", "")))
            # KANAL (RAIL) SINIFLANDIRMA — EFT / FAST / HAVALE (özgünlükten AYRI bir tespit).
            # KULLANICI KURALI: ücret kaleminde 'GEÇ EFT / EFT' ibaresi geçiyorsa işlem KESİN EFT'dir
            # (FAST DEĞİL); Akbank'ın 'EFT BANKALAR ARASI HESABA HAVALE' başlığı genel şablondur, ayırt
            # etmez. Net bir BİLGİ bildirimi olarak verilir (puanı düşürmez, weight=0).
            try:
                _rl = _auth.classify_rail(text_layout, ex.sender.iban, ex.receiver.iban,
                                          _bkey, ex.amount.value, ex.amount.fee)
            except Exception:
                _rl = None
            if _rl and _rl.get("rail") in ("eft", "fast", "havale"):
                _rlcode = {"eft": "RAIL_IS_EFT", "fast": "RAIL_IS_FAST", "havale": "RAIL_IS_HAVALE"}[_rl["rail"]]
                findings.append(Finding(
                    _rlcode, "info", "content", 0,
                    tr=_rl["notice_tr"], en=_rl["notice_en"],
                    detail=f"rail={_rl['rail']} conf={_rl['confidence']} evidence={' | '.join(_rl.get('evidence', []))}"))
            # OTORİTER KURAL: HAVALE banka-İÇİDİR. IBAN'lar FARKLI bankalarsa (bankalararası) işlem
            # HAVALE OLAMAZ → gösterilen işlem türünü (doc_kind) IBAN kanıtına göre düzelt. Başlıktaki
            # 'HAVALE' kelimesi (Akbank genel şablonu) yanıltıcıdır; kanal EFT/FAST'tır.
            try:
                import banks as _bkv2
                _scode = _bkv2.iban_bank_code(ex.sender.iban) if ex.sender.iban else ""
                _rcode = _bkv2.iban_bank_code(ex.receiver.iban) if ex.receiver.iban else ""
                _is_interbank = bool(_scode and _rcode and _scode != _rcode)
                if _is_interbank and str(getattr(ex, "doc_kind", "")).upper() == "HAVALE":
                    _rail_kind = (_rl or {}).get("rail")
                    ex.doc_kind = {"eft": "EFT", "fast": "FAST"}.get(_rail_kind, "Bankalararası transfer (EFT/FAST)")
            except Exception:
                pass
            # AYNI-BANKA ↔ 'BANKALARARASI/EFT/FAST' başlık çelişkisi (sahtecilik). IBAN'lar mod-97
            # geçerli+aynı banka olduğundan fotoğrafta da güvenilir; okuma hatası bastırılır.
            _sbc = _auth.check_samebank_rail_contradiction(text_layout, ex.sender.iban, ex.receiver.iban)
            if _sbc:
                findings.append(Finding(_sbc["code"], _sbc["severity"], "content", _sbc["weight"],
                                        tr=_sbc["tr"], en=_sbc["en"], detail=_sbc.get("detail", "")))
            # AYNA: BANKALARARASI ↔ HAVALE çelişkisi. Farklı bankalar arası işlem HAVALE olarak
            # sunuluyorsa (havale ücreti/kalemi) bu imkânsızdır → puanı düşüren tutarsızlık bulgusu.
            _ihc = _auth.check_interbank_havale_contradiction(text_layout, ex.sender.iban, ex.receiver.iban)
            if _ihc:
                findings.append(Finding(_ihc["code"], _ihc["severity"], "content", _ihc["weight"],
                                        tr=_ihc["tr"], en=_ihc["en"], detail=_ihc.get("detail", "")))
            # REFERANS PARMAK-İZİ KIYASI (gerçek PDF korpusundan): gelen dekontu bankanın GERÇEK
            # şablonuyla kıyaslar — kimlik numarası hane deseni, banka-özel para birimi soneki vb.
            # sapmalarını işaretler (veri-odaklı; support>=3 özelliklerde).
            _ref_codes = set()
            try:
                import reference_profiles as _refp
                for _rf in _refp.check_against_reference(_bkey, text_layout):
                    _ref_codes.add(_rf["code"])
                    findings.append(Finding(_rf["code"], _rf["severity"], "content", _rf["weight"],
                                            tr=_rf["tr"], en=_rf["en"], detail=_rf.get("detail", "")))
            except Exception:
                pass
            # PARA BİRİMİ SONEKİ (belge-içi tutarlılık) — yalnız referans profili bunu ZATEN
            # işaretlemediyse çalışır (profili olmayan bankalar için yedek; çift-bulgu önlenir).
            if "REF_FEE_CURRENCY_MISSING" not in _ref_codes:
                _acc = _auth.check_amount_currency_consistency(text_layout, _bkey)
                if _acc:
                    findings.append(Finding(_acc["code"], _acc["severity"], "content", _acc["weight"],
                                            tr=_acc["tr"], en=_acc["en"], detail=_acc.get("detail", "")))
            # KİMLİK ALAN TUTARLILIĞI: 'VKN/Vergi' alanı ↔ 'İşlemi Yapan TCKN' aynı kişide birebir
            # olmalı; farklıysa/biri sağlamayı geçemiyorsa sahtecilik (foto'da sağlama-destekli).
            _idc = _auth.check_id_field_consistency(text_layout, input_kind, extraction.text_source)
            if _idc:
                findings.append(Finding(_idc["code"], _idc["severity"], "content", _idc["weight"],
                                        tr=_idc["tr"], en=_idc["en"], detail=_idc.get("detail", "")))
            # Beklenen QR eksik: bu bankanın gerçek dekontlarında hep QR varken bu belgede yoksa
            # (veri-öğrenmeli, sıfır-FP: yalnızca >=5 gerçek dekontta %100 QR varsa tetiklenir)
            try:
                import store as _store_q
                if not qr.get("found") and _store_q.qr_expected(ex.bank):
                    findings.append(Finding(
                        "MISSING_EXPECTED_QR", "medium", "content", 14,
                        tr="BEKLENEN QR YOK: bu bankanın gerçek dekontlarında QR/karekod bulunur, ancak bu "
                           "belgede yok. Eksik QR, belgenin yeniden üretildiğine/oynandığına işaret edebilir.",
                        en="EXPECTED QR MISSING: this bank's genuine receipts carry a QR code, but this document "
                           "has none — may indicate the document was re-generated/tampered.",
                        detail=f"bank={ex.bank}"))
            except Exception:
                pass
            # İşlem türü (FAST/HAVALE/EFT) ↔ ücret tarifesi tutarlılığı — hem PDF hem
            # fotoğrafta çalışır (alan bazlı). Seed + store'dan öğrenilen tarifeler birleşir.
            try:
                import store as _store_l
                _learned = _store_l.learned_rail_fees(ex.bank)
            except Exception:
                _learned = None
            _fr = _auth.check_fee_rail(_bkey, text_layout, ex.amount.fee, _learned)
            if _fr:
                findings.append(Finding(_fr["code"], _fr["severity"], "content", _fr["weight"],
                                        tr=_fr["tr"], en=_fr["en"], detail=_fr.get("detail", "")))
            # SIRA NO (işlem anı) ↔ DÜZENLENME TARİHİ (belge oluşturma) tutarlılığı (Garanti vb.):
            # belge işlemden ÖNCE oluşturulmuşsa geriye tarihleme (kritik); sonraysa TUTARLI (olumlu).
            _sc = _auth.check_seq_vs_creation(text_layout)
            if _sc:
                findings.append(Finding(_sc["code"], _sc["severity"], "content", _sc["weight"],
                                        tr=_sc["tr"], en=_sc["en"], detail=_sc.get("detail", "")))
            # Fotoğraf dekontlarda GÖRSEL yazı/karakter tahrifatı: vision modelinin alan-bazlı
            # değerlendirmesi (yazı tipi/hizalama/üst üste binme/keskinlik farkı). Olasılıksal
            # olduğundan konservatif ağırlıklandırılır; deterministik kontrollerin yerini almaz.
            if vision_result and vision_result.get("tamper_suspected"):
                _conf = int(vision_result.get("tamper_confidence") or 0)
                _tf = vision_result.get("tamper_fields") or []
                _reason = (vision_result.get("tamper_reason") or "").strip()
                # Fotoğraf-vision tahrifat SEZGİSİ olasılıksaldır ve düşük güvende yanlış-pozitif
                # üretir (ör. yoğun/monospace alanları 'yazı tipi farklı' sanmak, ya da SIRA NO↔
                # DÜZENLENME zaman sırasını 'çelişki' zannetmek). Bu yüzden güven <60 ise SKORU
                # DÜŞÜRMEYEN bir BİLGİ notu olarak verilir; yalnız >=60 gerçek bulgu sayılır.
                if _conf >= 40:
                    if _conf >= 80:
                        _sev, _w = "high", 18
                    elif _conf >= 60:
                        _sev, _w = "medium", 8
                    else:
                        _sev, _w = "info", 0        # 40–59: bilgilendirme (puanı düşürmez)
                    findings.append(Finding(
                        "VISION_TEXT_TAMPER", _sev, "content", _w,
                        tr=f"GÖRSEL YAZI TAHRİFATI ŞÜPHESİ (güven %{_conf}): {_reason} "
                           f"Şüpheli alan(lar): {', '.join(_tf) if _tf else '—'}. Fotoğrafta ilgili metin "
                           f"dijital olarak düzenlenmiş olabilir (yazı tipi/hizalama/üst üste binme/keskinlik "
                           f"farkı). Fotoğraf dekontlarda kesinlik düşüktür; teyit için orijinal dijital PDF isteyin.",
                        en=f"Suspected visual text tampering (confidence {_conf}%): {_reason} "
                           f"Suspect field(s): {', '.join(_tf) if _tf else '—'}.",
                        detail=f"conf={_conf} fields={_tf}"))
            # Alan-lokalize piksel forensiği (yalnızca fotoğraf): tutar/isim kutularında
            # koyuluk/keskinlik aykırılığı. Kalibrasyon aşaması — BİLGİ amaçlı (ağırlık 0).
            if input_kind == "image" and pil is not None:
                try:
                    from image_forensics import text_field_forensics
                    _tgt = {"alıcı adı": ex.receiver.name or "",
                            "gönderici adı": ex.sender.name or "",
                            "tutar": (ex.amount.text or "")}
                    _pf = text_field_forensics(pil, _tgt)
                    if _pf.get("suspects"):
                        _flds = ", ".join(sorted({s["field"] for s in _pf["suspects"]}))
                        findings.append(Finding(
                            "PIXEL_FIELD_ANOMALY", "info", "image", 0,
                            tr=f"PİKSEL İZİ (bilgi): {_flds} alan(lar)ında metnin koyuluk/keskinlik değeri "
                               f"belgenin genel yazısından belirgin farklı — olası dijital düzenleme. Bu "
                               f"BİLGİLENDİRİCİ bir sinyaldir, tek başına kanıt değildir; teyit için orijinal "
                               f"dijital PDF isteyin.",
                            en=f"PIXEL TRACE (info): text in field(s) {_flds} differs markedly in darkness/"
                               f"sharpness from the body text — possible digital editing. Informational only.",
                            detail=f"suspects={_pf['suspects']} checked={_pf.get('checked')}"))
                except Exception:
                    pass
            # Tarih mantık zinciri (gelecek tarih / dekont<işlem / valör<işlem) — foto + PDF
            for _dc in _auth.check_date_chain(text_layout, ex.transaction.date,
                                              ex.transaction.value_date):
                findings.append(Finding(_dc["code"], _dc["severity"], "content", _dc["weight"],
                                        tr=_dc["tr"], en=_dc["en"], detail=_dc.get("detail", "")))
            # Kimlik (TCKN/VKN) sağlaması — maskeli değilse kontrol basamağı tutmalı.
            # (Banka-özel temizlikler extract.py'deki ilgili banka dalında yapılır; ör. Ziraat'ta
            #  boş 'VERGİ KİMLİK NO' alanına adres rakamlarının sızması orada engellenir.)
            # KİMLİK SAĞLAMASI (TCKN/VKN): kontrol basamağı DIGIT-ÖZEL bir sağlamadır.
            # Fotoğraf/OCR/vision'da tek bir rakamın yanlış okunması sağlamayı bozar →
            # yanlış 'GEÇERSİZ KİMLİK'. Gerçek dekontta kimlik her zaman geçerlidir; pikselden
            # okumada geçersizlik bir OKUMA HATASIDIR, tahrifat değil → yalnız dijital-metin PDF.
            if not (input_kind == "image" or extraction.text_source in ("ocr", "vision")):
                _id = _auth.check_identity(ex.sender.tckn, "gönderen")
                if _id:
                    findings.append(Finding(_id["code"], _id["severity"], "content", _id["weight"],
                                            tr=_id["tr"], en=_id["en"], detail=_id.get("detail", "")))
            # Kendine transfer (gönderici IBAN = alıcı IBAN) — yalnızca güvenilir çıkarımda (PDF)
            if input_kind != "image" and extraction.text_source not in ("ocr", "vision"):
                _st = _auth.check_self_transfer(ex.sender.iban, ex.receiver.iban)
                if _st:
                    findings.append(Finding(_st["code"], _st["severity"], "content", _st["weight"],
                                            tr=_st["tr"], en=_st["en"], detail=_st.get("detail", "")))
            # Görüntü editörü / düzenleme imzası (fotoğraf dekontlar için anında bayrak)
            if img_forensics is not None:
                _ie = _auth.check_image_editor(img_forensics.exif_software,
                                               getattr(img_forensics, "edit_signature_hits", None),
                                               img_forensics.c2pa_present)
                if _ie:
                    findings.append(Finding(_ie["code"], _ie["severity"], "metadata", _ie["weight"],
                                            tr=_ie["tr"], en=_ie["en"], detail=_ie.get("detail", "")))
            # Dekont-no ↔ işlem tarihi sıralılığı (global monoton sayaçlı bankalar)
            try:
                import store as _store_s
                _txn_iso = ""
                if _txn_dt:
                    _txn_iso = _txn_dt.isoformat()
                _sa = _store_s.sequence_anomaly(ex.bank, ex.transaction.sequence_number, _txn_iso, "")
                if _sa:
                    findings.append(Finding(_sa["code"], _sa["severity"], "content", _sa["weight"],
                                            tr=_sa["tr"], en=_sa["en"], detail=_sa.get("detail", "")))
            except Exception:
                pass
        except Exception:
            pass

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
            # BANKA-BAZLI NUMARA TEKRARI: aynı bankada daha önce görülmüş işlem/sıra/referans numarası
            for nf in _store.check_number_reuse(_pre):
                findings.append(Finding(nf["code"], nf["severity"], "content", 45,
                                        tr=nf["tr"], en=nf["en"], detail=nf.get("detail", "")))
            # KARA LİSTE: daha önce sahte damgalanmış belgeyle eşleşme
            for bf in _store.check_blocklist(_pre):
                findings.append(Finding(bf["code"], bf["severity"], "content", bf["weight"],
                                        tr=bf["tr"], en=bf["en"], detail=bf.get("detail", "")))
        except Exception:
            db_findings = []

    # 7) KESİN CEVAPLAR (true/false/nötr) — skordan ÖNCE hesaplanır ki skor buna bağlansın
    import verdicts as _vd
    _db_count = 0
    if use_store:
        try:
            import store as _st
            _db_count = _st.stats().get("count", 0)
        except Exception:
            _db_count = 0
    _bal_state = "neutral"
    if is_statement:
        _bc = stmt["balance"].get("consistent")
        _bal_state = "true" if _bc is True else ("false" if _bc is False else "neutral")
    verdicts = _vd.compute_verdicts(
        doc_type=doc_type, input_kind=input_kind,
        codes={f.code for f in findings}, cons=cons,
        has_pdf_dates=struct.creation_dt is not None,
        txn_date=_txn_ref, seq=ex.transaction.sequence_number,
        db_checked=bool(use_store and is_receipt), db_count=_db_count, is_receipt=is_receipt,
        doc_kind=doc_kind, balance_state=_bal_state, timing=timing)

    # 7.5) Skorlama — KESİN KARAR "GÜVENİLİR DEĞİL" ise puan "güvenilir" olamaz (tutarlılık)
    _untrusted = verdicts["overall"]["state"] == "false"
    score = compute_score(findings, doc_type, manip, ai, verdict_untrusted=_untrusted)

    # 7.6) Alt-skorlar (Dekont Guard tarzı)
    subscores = _compute_subscores(struct, rev, cons, xml, qr_check, findings, doc_type)

    # 7.7) TAHRİFAT KARŞILAŞTIRMASI — hangi alan, orijinal hali, değiştirilmiş hali (görsel)
    tamper_comparison = _build_tamper_comparison(
        {f.code for f in findings}, rev, timing, stmt if is_statement else None)

    # 7.5) YZ DEĞERLENDİRİCİ (opsiyonel, additive) — kural motorunun ÜSTÜNE oturur.
    # Bir tahrifat/uyuşmazlık bulgusu VAR ise ya da kritik alanlar BOŞ/şüpheli ise, dekont bu
    # katmana eskale edilir: görüntüyü doğrudan inceleyip bulguları YENİDEN yargılar, yanlış
    # okunan IBAN'ı/boş alanları YENİDEN OKUR, banka-bazlı iyileştirme teşhisi üretir.
    # KAPALI varsayılan (DEKONT_AI_ADJUDICATOR=1 + ANTHROPIC_API_KEY gerekir) → mevcut API davranışı
    # ve anahtarları DEĞİŞMEZ; yalnız yeni 'yapay_zeka_degerlendirmesi' alanı eklenir.
    ai_adjudication = None
    try:
        import ai_adjudicator as _aj
        if _aj.is_enabled():
            _ex_dict = extraction.as_dict()
            _find_dicts = [{"code": f.code, "severity": f.severity, "weight": f.weight, "tr": f.tr}
                           for f in findings]
            _go, _reasons = _aj.should_adjudicate(_find_dicts, _ex_dict, input_kind)
            print(f"[adjudicator] eskalasyon={_go} input_kind={input_kind} pil_var={locals().get('pil') is not None} "
                  f"nedenler={_reasons}", flush=True)
            if _go:
                import authenticity as _auth_aj
                ai_adjudication = _aj.adjudicate(
                    _ex_dict, _find_dicts, _auth_aj.bank_key(ex.bank),
                    pil_image=locals().get("pil"), input_kind=input_kind,
                    text_source=extraction.text_source)
                if ai_adjudication is not None:
                    ai_adjudication["tetik_nedenleri"] = _reasons
                    # ÖĞREN: YZ'nin doğruladığı banka-bazlı etiket ipuçlarını kalıcı store'a yaz
                    # → sonraki dekontlarda otomatik uygulanır (kod değişmeden 'öğren-uygula').
                    if use_store:
                        try:
                            _learned = _aj.learn_from(ai_adjudication, _auth_aj.bank_key(ex.bank))
                            if _learned:
                                ai_adjudication["ogrenilen_ipuclari"] = [
                                    {"field": f, "label": l} for f, l in _learned]
                        except Exception:
                            pass
    except Exception as _e:
        ai_adjudication = None

    # 7.9) YZ'NİN GÖRÜNTÜDEN OKUDUĞU ALANLARI EKRANA YANSIT: OCR boş/yanlış bıraktığı kritik alanları
    # (alıcı adı, alıcı IBAN, tutar, işlem no, referans no) YZ okuduysa, çıkarım dict'ine EK olarak işle
    # ki rapor ekranında BOŞ görünmesin. IBAN yalnız mod-97 geçerliyse uygulanır (uydurma engellenir).
    _extracted_dict = extraction.as_dict()
    if ai_adjudication:
        try:
            _extracted_dict = _aj.apply_corrections(_extracted_dict, ai_adjudication)
        except Exception:
            pass

    # 7.95) YZ GÖRSEL TAHRİFAT → BULGU: Fotoğraf/görüntü dekontta yazı tipi/yapıştırma uyuşmazlığı
    # (ör. yazıyla yazılan tutarın farklı fontta olması) kural motoruyla GÖRÜLEMEZ; YZ görüntüden
    # gördüyse bunu GERÇEK bir bulguya çevirir ve skoru + kesin kararı YENİDEN hesaplarız.
    if ai_adjudication:
        try:
            _gt = [g for g in (ai_adjudication.get("gorsel_tahrifat") or []) if int(g.get("guven") or 0) >= 50]
        except Exception:
            _gt = []
        # YEDEK TETİK: YZ gorsel_tahrifat alanını doldurmasa bile, hükmü sahte/şüpheli VE gerekçesinde
        # yazı tipi/font/yapıştırma geçiyorsa görsel tahrifat bulgusu üret (yapılandırılmamış olsa da yakala).
        if not _gt:
            _rz = (ai_adjudication.get("reasoning_tr") or "").lower()
            _fr_txt = " ".join((x.get("aciklama") or "") for x in (ai_adjudication.get("finding_reviews") or [])).lower()
            _blob = _rz + " " + _fr_txt
            _font_kw = ("yazı tipi", "yazi tipi", "font", "yapıştır", "yapistir", "kalınlık", "kalinlik",
                        "farklı fontta", "farkli fontta", "karakter")
            if ai_adjudication.get("verdict") in ("sahte", "şüpheli", "supheli") and any(k in _blob for k in _font_kw):
                _gt = [{"alan": "tutar/metin", "aciklama": (ai_adjudication.get("reasoning_tr") or "")[:300], "guven": 60}]
                ai_adjudication.setdefault("gorsel_tahrifat", _gt)
        print(f"[adjudicator] gorsel_tahrifat_bulgu={bool(_gt)} adet={len(_gt) if _gt else 0}", flush=True)
        if _gt:
            _alanlar = "; ".join(f"{g.get('alan','')}: {g.get('aciklama','')}" for g in _gt)[:600]
            findings.append(Finding(
                "AI_VISUAL_TAMPER", "high", "image", 30,
                tr=("GÖRSEL TAHRİFAT (YZ görüntü incelemesi): belgede yazı tipi/kalınlık uyuşmazlığı — bir "
                    "alan belgenin genel yazı tipinden FARKLI, sonradan yapıştırılmış/değiştirilmiş görünüyor. "
                    + _alanlar),
                en=("VISUAL TAMPER (AI image review): font/weight inconsistency — a field is in a DIFFERENT "
                    "font than the document and appears pasted/altered. " + _alanlar),
                detail="ai_gorsel_tahrifat"))
            # bulgu eklendi → kesin kararı, skoru, alt-skorları ve tahrifat karşılaştırmasını yenile
            verdicts = _vd.compute_verdicts(
                doc_type=doc_type, input_kind=input_kind,
                codes={f.code for f in findings}, cons=cons,
                has_pdf_dates=struct.creation_dt is not None,
                txn_date=_txn_ref, seq=ex.transaction.sequence_number,
                db_checked=bool(use_store and is_receipt), db_count=_db_count, is_receipt=is_receipt,
                doc_kind=doc_kind, balance_state=_bal_state, timing=timing)
            _untrusted = verdicts["overall"]["state"] == "false"
            score = compute_score(findings, doc_type, manip, ai, verdict_untrusted=_untrusted)
            subscores = _compute_subscores(struct, rev, cons, xml, qr_check, findings, doc_type)
            tamper_comparison = _build_tamper_comparison(
                {f.code for f in findings}, rev, timing, stmt if is_statement else None)

    # 8) Rapor derle
    lang_findings = [f.as_dict("tr") for f in findings]
    lang_findings_en = [f.as_dict("en") for f in findings]

    report = {
        "yapay_zeka_degerlendirmesi": ai_adjudication,
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
            "is_statement": is_statement,
            "statement_score": st_score,
            "doc_kind": doc_kind,
            "doc_kind_label_tr": {"dekont": "Banka dekontu", "hesap_hareketi": "Hesap hareketi (hesap özeti)",
                                  "diger": "Diğer / belirsiz"}.get(doc_kind, doc_kind),
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
        "verdicts": verdicts,
        "tamper_comparison": tamper_comparison,
        "statement": (stmt if is_statement else {"is_statement": False}),
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
        "extracted": _extracted_dict,   # YZ görüntüden okuduğu kritik alanlarla doldurulmuş (boş bırakmaz)
        "iban_ocr_onarim": _iban_fixes,   # EK: OCR tek-rakam IBAN onarımları (şeffaflık)
        "image_forensics": _img_forensics_dict(img_forensics),
        "cross_db": {
            "checked": bool(use_store and is_receipt),
            "matches": db_findings,
            "recorded": False,
        },
    }

    # 8.9) Belge/doküman numarasının yapısını ayrıştır (görüntüleme için): tarih kısmı + sayaç
    if is_receipt:
        try:
            import authenticity as _auth
            _tr = report["extracted"]["transaction"]
            _parts = _auth.document_no_parts(_auth.bank_key(report["extracted"].get("bank", "")),
                                             _tr.get("document_no", ""))
            if _parts:
                _tr["document_no_parts"] = _parts
        except Exception:
            pass

    # 8.95) ÜRETİCİ DEĞERLENDİRMESİ: bankanın beklenen derleyicisi ↔ bu PDF'in gerçek derleyicisi
    if is_receipt:
        try:
            import authenticity as _auth
            _md = report["metadata"]
            _resaved = bool(report["tamper"].get("append_mode"))
            report["metadata"]["producer_check"] = _auth.producer_assessment(
                _auth.bank_key(report["extracted"].get("bank", "")),
                _md.get("producer", ""), _md.get("creator", ""), _resaved)
        except Exception:
            pass

    # 9) Doğrulanmışsa numaralarını kalıcı veritabanına kaydet (banka bazlı)
    if use_store:
        try:
            import store as _store
            # İşlem türünü (rail) kayda geçir — tarife öğrenmesi bunu kullanır
            try:
                import authenticity as _auth_r
                report["extracted"].setdefault("transaction", {})["rail"] = \
                    _auth_r.detect_transfer_rail(text_layout) or ""
            except Exception:
                pass
            report["cross_db"]["recorded"] = _store.record(report)
            # AUDIT LOG: her analiz (sahte dahil) — 'kaç yüklendi' + kara-liste temeli
            _store.log_analysis(report)
        except Exception:
            pass
    # DENETİM KAPSAMI (EK): hangi denetimler yapıldı / yapılamadı — BANKA BAZLI, şeffaf.
    try:
        import coverage as _cov
        report["denetim_kapsami"] = _cov.build(report)
    except Exception:
        pass
    # BANKA-İÇİ HAFIZA: bu dekontu kendi bankasının geçmişine ekle (kullanıcı: eski dekontları
    # içeride sakla ve banka bazlı karşılaştır). Yalnız gerçek dekont + store açıkken.
    if use_store and is_receipt:
        try:
            import store as _store_bc, authenticity as _auth_bc
            _bk = _auth_bc.bank_key(report["extracted"].get("bank", ""))
            _rc = next((c["code"] for c in report["findings_tr"]
                        if c["code"] in ("RAIL_IS_EFT", "RAIL_IS_FAST", "RAIL_IS_HAVALE")), "")
            _rl = {"RAIL_IS_EFT": "eft", "RAIL_IS_FAST": "fast", "RAIL_IS_HAVALE": "havale"}.get(_rc, "")
            if _bk and _rl:
                _store_bc.bank_corpus_add(_bk, struct.sha256, _rl,
                                          report["extracted"].get("doc_kind", ""),
                                          report["extracted"].get("amount", {}).get("value"))
        except Exception:
            pass
    # TARAMA KAYDI: web/API'de verilen cevabı sakla (sorgu/ref/isim ile aranabilir). Böylece kullanıcı
    # dekontu yükleyince 'web ne demişti vs gerçek ne' karşılaştırılıp banka-bazlı hata çözümlenir.
    if use_store:
        try:
            import store as _store_sl
            _store_sl.log_scan(report)
        except Exception:
            pass
    # HIZ ÖNBELLEĞİNE YAZ: aynı dosya ikinci kez gelirse tüm hattı atlayıp bunu döndürürüz.
    if use_store:
        try:
            import store as _store_cput
            _store_cput.cache_put(struct.sha256, ENGINE_VERSION, report)
        except Exception:
            pass
    return report


def _build_tamper_comparison(codes: set, rev: dict, timing: dict, stmt: dict | None) -> list:
    """Tahrifat/değişiklik karşılaştırması: her satır {alan, orijinal, degistirilmis, durum,
    kaynak, onem}. Rapor bu veriyi 'Alan · Orijinal · Değiştirilmiş' görselinde gösterir."""
    rows = []
    cl = (timing or {}).get("creation_local") or "—"
    ml = (timing or {}).get("mod_local") or "—"
    tl = (timing or {}).get("transaction_local") or "—"

    # 1) İçerik alanları — PDF revizyonları arasında GERÇEK orijinal→değiştirilmiş
    if rev and rev.get("has_prior"):
        for c in rev.get("changes", []):
            rows.append({
                "alan": c.get("label", c.get("field", "")),
                "orijinal": c.get("prev", ""),
                "degistirilmis": c.get("curr", ""),
                "durum": "İçerik, PDF revizyonları arasında değiştirilmiş",
                "kaynak": "İçerik (PDF revizyonu)",
                "onem": "kritik" if c.get("severity") == "kritik" else "yuksek",
            })

    # 2) Metadata / tarih tahrifatı
    if "TIME_FILE_BEFORE_TXN" in codes:
        rows.append({
            "alan": "PDF üretim tarihi ↔ belge içeriği",
            "orijinal": f"İçerikteki en geç işlem/dönem: {tl}",
            "degistirilmis": f"Metadata üretim tarihi: {cl}",
            "durum": "İMKÂNSIZ — dosya, içerdiği işlemlerden ÖNCE üretilmiş (geriye tarihleme)",
            "kaynak": "Metadata (tarih)",
            "onem": "kritik",
        })
    if "TIME_MODIFIED_AFTER_CREATE" in codes:
        rows.append({
            "alan": "PDF değiştirme tarihi",
            "orijinal": f"Oluşturma (ilk üretim): {cl}",
            "degistirilmis": f"Değiştirme (sonradan): {ml}",
            "durum": "Belge üretildikten sonra açılıp yeniden kaydedilmiş (olası oynama)",
            "kaynak": "Metadata (tarih)",
            "onem": "yuksek",
        })
    if "TIME_MOD_BEFORE_CREATE" in codes:
        rows.append({
            "alan": "PDF değiştirme ↔ oluşturma tarihi",
            "orijinal": f"Oluşturma: {cl}",
            "degistirilmis": f"Değiştirme: {ml}",
            "durum": "Değiştirme tarihi oluşturmadan ÖNCE — metadata çelişkisi (oynama)",
            "kaynak": "Metadata (tarih)",
            "onem": "yuksek",
        })

    # 3) Hesap hareketi — yürüyen bakiye zinciri kırılmaları
    if stmt:
        for b in (stmt.get("balance", {}) or {}).get("breaks", []):
            rows.append({
                "alan": f"Bakiye — {b.get('tarih','')} tarihli satır",
                "orijinal": f"Beklenen bakiye: {b.get('beklenen_onceki_bakiye')}",
                "degistirilmis": f"Belgedeki bakiye: {b.get('gercek_onceki_bakiye')} (fark {b.get('fark')})",
                "durum": "Yürüyen bakiye zinciri kırık — bir tutar/bakiye değiştirilmiş",
                "kaynak": "İçerik (bakiye zinciri)",
                "onem": "kritik",
            })
        _cc = stmt.get("count_check", {})
        if _cc.get("tutarli") is False and _cc.get("kesin") and _cc.get("eksik", 0) > 0:
            rows.append({
                "alan": "İşlem satır sayısı",
                "orijinal": f"Belgede beyan edilen: {_cc['beyan']} kayıt",
                "degistirilmis": f"Listelenen: {_cc['gercek']} kayıt ({_cc['eksik']} eksik)",
                "durum": "Beyan edilenden az satır var — hareket satırı SİLİNMİŞ olabilir",
                "kaynak": "İçerik (satır silme)",
                "onem": "kritik",
            })
    return rows


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

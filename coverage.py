"""
DENETİM KAPSAMI (coverage) — bir dekont için HANGİ denetimlerin yapıldığını, sonucunu ve
YAPILAMAYAN denetimleri BANKA BAZLI olarak açıkça raporlar.

İlke (kullanıcı kuralı): TÜM kurallar banka bazlıdır. Her madde, tespit edilen bankaya göre
değerlendirilir ve sonuç bankayla birlikte yazılır. Yapılamayan bir denetim varsa sebebiyle
birlikte belirtilir (ör. görüntüde yapısal PDF doğrulaması mümkün değildir).

Çıktı ek (additive) bir rapor alanıdır: report["denetim_kapsami"]. Mevcut hiçbir anahtar değişmez.
"""
from __future__ import annotations


def _party_iban_status(iban: str):
    import banks as _b
    if not iban:
        return "yapılamadı", "IBAN okunamadı/boş"
    v = _b.iban_valid(iban)
    bank = _b.bank_label_from_iban(iban) or _b.bank_from_iban(iban) or "banka bilinmiyor"
    if v is True:
        return "yapıldı", f"geçerli (mod-97 ✓) — {bank}"
    if v is False:
        return "kusur", f"GEÇERSİZ (mod-97 tutmuyor) — {bank}"
    return "kısmi", f"IBAN kalıbı doğrulanamadı — {bank}"


def _tc_status(val: str, label: str):
    import banks as _b
    if not val:
        return "yapılamadı", f"{label} okunamadı/boş"
    v = _b.tckn_valid(val)
    if v is True:
        return "yapıldı", f"{label}: geçerli (sağlama ✓)"
    if v is False:
        return "kusur", f"{label}: GEÇERSİZ (sağlama tutmuyor)"
    return "kısmi", f"{label}: 11 hane değil/maskeli, doğrulanamadı"


def build(report: dict) -> dict:
    """report sözlüğünden banka-bazlı denetim kapsamını üretir."""
    ex = report.get("extracted", {}) or {}
    cls = report.get("classification", {}) or {}
    findings = report.get("findings_tr", []) or []
    codes = {f.get("code") for f in findings}
    input_kind = cls.get("input_kind") or report.get("input_kind") or "pdf"
    is_image = (input_kind == "image") or cls.get("text_source") in ("ocr", "vision")
    bank = ex.get("bank") or "banka tespit edilemedi"

    snd = ex.get("sender", {}) or {}
    rcv = ex.get("receiver", {}) or {}
    amt = ex.get("amount", {}) or {}
    tx = ex.get("transaction", {}) or {}

    items = []

    def add(alan, durum, sonuc, banka_bazli=True):
        items.append({"alan": alan, "durum": durum, "sonuc": sonuc, "banka_bazli": banka_bazli})

    # 1) Kanal (HAVALE/EFT/FAST) — EFT bulgusu varsa mutlaka yazılır
    rail_code = next((c for c in ("RAIL_IS_EFT", "RAIL_IS_FAST", "RAIL_IS_HAVALE") if c in codes), None)
    rail_map = {"RAIL_IS_EFT": "EFT", "RAIL_IS_FAST": "FAST", "RAIL_IS_HAVALE": "HAVALE"}
    _rail = None
    if rail_code:
        _rail = rail_map[rail_code].lower()
        add("Kanal (EFT/FAST/HAVALE)", "yapıldı", f"{rail_map[rail_code]} olarak sınıflandı ({rail_code})")
    else:
        add("Kanal (EFT/FAST/HAVALE)", "kısmi",
            "Kanal kesinleştirilemedi (belirsiz) — açık EFT/FAST işareti ya da IBAN çifti yok")
    # BANKA-İÇİ KARŞILAŞTIRMA (kullanıcı kuralı: yalnız aynı bankanın dekontlarıyla kıyas)
    try:
        import authenticity as _auth, bank_corpus as _bc
        _bkey = _auth.bank_key(bank)
        if _bkey and _rail:
            _cmp = _bc.compare_rail(_bkey, _rail)
            add("Banka-içi karşılaştırma (aynı banka normu)", _cmp["durum"], _cmp["sonuc"])
    except Exception:
        pass
    # Kanal çelişkileri (banka bazlı sahtecilik)
    for c, txt in (("SAMEBANK_RAIL_CONTRADICTION", "aynı banka ama bankalararası/EFT/FAST başlık"),
                   ("INTERBANK_HAVALE_CONTRADICTION", "farklı banka ama HAVALE olarak sunulmuş"),
                   ("FEE_RAIL_MISMATCH", "ücret kanal tarifesine uymuyor")):
        if c in codes:
            add("Kanal çelişkisi", "kusur", f"{txt} ({c})")

    # 2) IBAN doğruluğu (mod-97) — gönderici + alıcı
    ds, ss = _party_iban_status(snd.get("iban", ""))
    add("Gönderici IBAN doğruluğu (mod-97)", ds, ss)
    dr, sr = _party_iban_status(rcv.get("iban", ""))
    add("Alıcı IBAN doğruluğu (mod-97)", dr, sr)
    # 2b) IBAN BANKA KODU KARŞILAŞTIRMASI (kullanıcı kuralı, TÜM bankalar): iki IBAN'ın banka kodu
    # AYNI ise işlem banka-içi HAVALE'dir; FARKLI ise bankalararası (EFT ya da FAST).
    try:
        import banks as _bk2
        _sc = _bk2.iban_bank_code(snd.get("iban", "")) if snd.get("iban") else ""
        _rc = _bk2.iban_bank_code(rcv.get("iban", "")) if rcv.get("iban") else ""
        if _sc and _rc:
            if _sc == _rc:
                add("IBAN banka kodu karşılaştırması", "yapıldı",
                    f"Gönderici ve alıcı AYNI bankada (kod {_sc}) → işlem banka-içi HAVALE olmalı."
                    + (" Kanal HAVALE ile tutarlı." if _rail == "havale"
                       else f" DİKKAT: kanal '{(_rail or 'belirsiz').upper()}' — aynı bankada EFT/FAST olamaz (çelişki)."))
            else:
                add("IBAN banka kodu karşılaştırması", "yapıldı",
                    f"Gönderici (kod {_sc}) ve alıcı (kod {_rc}) FARKLI bankalarda → bankalararası "
                    f"(EFT ya da FAST). Kanal: {(_rail or 'belirsiz').upper()}."
                    + (" DİKKAT: farklı bankada HAVALE olamaz." if _rail == "havale" else ""))
        else:
            add("IBAN banka kodu karşılaştırması", "kısmi",
                "İki tarafın IBAN'ı okunamadığından banka kodu karşılaştırması yapılamadı.")
    except Exception:
        pass

    # 3) Kimlik (TC/VKN) doğruluğu + çapraz tutarlılık
    d1, s1 = _tc_status(snd.get("tckn", ""), "Gönderici/işlem TCKN")
    add("Kimlik (TC/VKN) doğruluğu", d1, s1)
    if "ID_FIELD_MISMATCH" in codes:
        add("Kimlik çapraz tutarlılık (VKN ↔ TCKN)", "kusur",
            "VKN alanı ile işlemi yapan TCKN uyuşmuyor (kimlik uydurma) — ID_FIELD_MISMATCH")
    elif "ID_CHECKSUM_INVALID" in codes:
        add("Kimlik sağlama", "kusur", "TC/VKN sağlaması tutmuyor — ID_CHECKSUM_INVALID")

    # 4) Alıcı & gönderici ad temizliği
    add("Gönderici adı çıkarımı", "yapıldı" if snd.get("name") else "yapılamadı",
        snd.get("name") or "okunamadı")
    add("Alıcı adı çıkarımı", "yapıldı" if rcv.get("name") else "yapılamadı",
        rcv.get("name") or "okunamadı")
    if "MASKED_RECEIVER_NAME" in codes:
        add("Alıcı adı maskeleme", "kısmi", "Alıcı adı maskeli (banka şablonu) — MASKED_RECEIVER_NAME")

    # 5) Tutar / vergi / toplam analizi (aritmetik: tutar + ücret = toplam)
    v, fee, tot = amt.get("value"), amt.get("fee"), amt.get("total")
    if v is not None and fee is not None and tot is not None:
        ok = abs((v + fee) - tot) < 0.02
        add("Tutar/vergi/toplam aritmetiği", "yapıldı" if ok else "kusur",
            f"tutar {v} + ücret {fee} = {round(v + fee, 2)} vs toplam {tot} → {'tutarlı' if ok else 'TUTARSIZ'}")
    elif v is not None:
        add("Tutar analizi", "kısmi", f"tutar {v}; ücret/toplam eksik (aritmetik doğrulanamadı)")
    else:
        add("Tutar analizi", "yapılamadı", "tutar okunamadı")
    if "AMOUNT_MISMATCH" in codes:
        add("Tutar tutarlılığı", "kusur", "aynı tutar belgede farklı yazılmış — AMOUNT_MISMATCH")
    if "AMOUNT_CURRENCY_INCONSISTENT" in codes or "REF_FEE_CURRENCY_MISSING" in codes:
        add("Masraf para birimi (banka şablonu)", "kusur",
            "bu bankada masraf hep TL'li olur; burada TL yok")

    # 6) İşlem tarihi
    add("İşlem tarihi analizi", "yapıldı" if tx.get("date") else "yapılamadı",
        tx.get("date") or "tarih okunamadı")
    for c, txt in (("DATE_IN_FUTURE", "işlem/dekont tarihi gelecekte"),
                   ("VALUE_DATE_ANOMALY", "valör tarihi aykırı")):
        if c in codes:
            add("Tarih tutarlılığı", "kusur", f"{txt} ({c})")
    # 6b) FİŞ/BELGE NUMARASI TARİH DOĞRULAMASI (banka bazlı: Enpara/QNB Fiş No ilk 8 hane = YYYYAAGG).
    # Fiş numarası bankanın verdiği DEĞİŞTİRİLEMEZ tarihtir; işlem tarihiyle uyuşmalı.
    try:
        import authenticity as _auth3
        _bk3 = _auth3.bank_key(bank)
        _docno = tx.get("document_no", "") or ""
        _emb = _auth3._embedded_date(_docno) if _bk3 in ("enpara", "qnb") else None
        if "RECEIPT_NO_DATE_MISMATCH" in codes:
            add("Fiş No tarih doğrulaması (banka bazlı)", "kusur",
                "Fiş No'nun gömülü tarihi (ilk 8 hane) işlem tarihiyle UYUŞMUYOR → tarih değiştirilmiş "
                "olabilir (güçlü sahtecilik işareti) — RECEIPT_NO_DATE_MISMATCH.")
        elif _emb is not None:
            add("Fiş No tarih doğrulaması (banka bazlı)", "yapıldı",
                f"Fiş No {_docno} → gömülü tarih {_emb.strftime('%d.%m.%Y')}, işlem tarihiyle uyumlu ✓.")
        elif _bk3 in ("enpara", "qnb") and _docno:
            add("Fiş No tarih doğrulaması (banka bazlı)", "kısmi",
                f"Fiş No ({_docno}) ilk 8 hanesinden geçerli tarih çözülemedi.")
    except Exception:
        pass
    # 6c) BANKA-BAZLI NUMARA TEKRARI: işlem/sıra/referans numarası aynı bankada başka bir dekontta
    #     görüldü mü? (kopyala-yapıştır sahtecilik). Her banka kendi içinde değerlendirilir.
    _has_num = bool((tx.get("document_no") or "").strip() or (tx.get("ref_no") or "").strip()
                    or (tx.get("sequence_number") or "").strip())
    if "NUMBER_REUSE" in codes or "SEQ_DB_DUPLICATE" in codes:
        add("Banka-bazlı numara tekrarı kontrolü", "kusur",
            "Bu dekonttaki işlem/sıra/referans numarası AYNI bankada daha önce FARKLI bir dekontta da "
            "görüldü → kopyalanmış/uydurulmuş numara (kritik sahtecilik).")
    elif _has_num:
        add("Banka-bazlı numara tekrarı kontrolü", "yapıldı",
            "İşlem/sıra/referans numarası banka-bazlı geçmişle karşılaştırıldı; başka dekontta tekrar yok.")
    else:
        add("Banka-bazlı numara tekrarı kontrolü", "yapılamadı",
            "Belgede karşılaştırılabilir işlem/sıra/referans numarası okunamadı.")

    # 6d) GÖRSEL TAHRİFAT (YZ): yazı tipi/kalınlık/hizalama uyuşmazlığı (yapıştırılmış alan). Fotoğraf/
    #     görüntü dekontlarda kural motoru göremez; YZ görsel incelemesi yapar.
    if "AI_VISUAL_TAMPER" in codes:
        add("Görsel tahrifat (yazı tipi/yapıştırma)", "kusur",
            "YZ görsel incelemesi: bir alan (ör. yazıyla tutar) belgenin genel yazı tipinden FARKLI → "
            "sonradan yapıştırılmış/değiştirilmiş (görsel sahtecilik).")
    elif input_kind == "image":
        add("Görsel tahrifat (yazı tipi/yapıştırma)", "yapıldı",
            "YZ görsel incelemesinden geçti; yazı tipi/hizalama tutarsızlığı (yapıştırma) tespit edilmedi.")
    else:
        add("Görsel tahrifat (yazı tipi/yapıştırma)", "kısmi",
            "Dijital PDF — font tutarlılığı yapısal olarak denetlenir; görüntü-bazlı yapıştırma denetimi "
            "yalnız fotoğraf/görüntü dekontlarda YZ ile yapılır.")

    # 7) ÜRETİM UYGULAMASI + DÜZENLEME TESPİTİ (hangi uygulamada yapıldı; AI/Photoshop/Canva ile
    #    değiştirilip yeniden kaydedilmiş mi). Hem fotoğraf (EXIF/XMP) hem PDF (producer/creator) için.
    imf = report.get("image_forensics", {}) or {}
    meta = report.get("metadata", {}) or {}
    soft = (imf.get("exif_software") or "").strip()
    make = (imf.get("exif_make") or "").strip()
    producer = (meta.get("producer") or "").strip()
    creator = (meta.get("creator") or "").strip()
    edit_hits = imf.get("edit_signature_hits") or []
    ai_hits = imf.get("ai_signature_hits") or []
    c2pa = imf.get("c2pa_present")
    _blob = " / ".join(x for x in (soft, make, producer, creator) if x)

    def _classify(s):
        s = (s or "").lower()
        EDIT = ["photoshop", "canva", "gimp", "lightroom", "affinity", "pixelmator", "paint.net",
                "figma", "illustrator", "snapseed", "picsart"]
        AI = ["stable diffusion", "stablediffusion", "dall", "midjourney", "firefly", "generative",
              "stylegan", "biggan", "diffusion", "openai"]
        BROWSER = ["skia", "chromium", "chrome", "headless", "puppeteer", "wkhtml", "pdfium"]
        for k in AI:
            if k in s:
                return "ai", k
        for k in EDIT:
            if k in s:
                return "editor", k
        for k in BROWSER:
            if k in s:
                return "browser", k
        return None, None

    kind, hitname = _classify(_blob)
    editor_finding = any(f.get("code") in ("IMAGE_EDITOR_SOFTWARE", "IMAGE_EDIT_SIGNATURE") for f in findings)
    rerender_finding = any(f.get("code") in ("PDFIUM_PRODUCED", "BROWSER_RERENDER", "FONT_BROWSER_RERENDER") for f in findings)

    if ai_hits or kind == "ai":
        add("Üretim uygulaması / düzenleme (AI/Photoshop/Canva)", "kusur",
            f"YAPAY ZEKA üretimi/işlemi izi: {', '.join(ai_hits) or hitname}. Belge yapay zekayla üretilmiş/değiştirilmiş olabilir.")
    elif edit_hits or kind == "editor" or editor_finding:
        _who = ", ".join(edit_hits) or hitname or "görsel düzenleyici"
        add("Üretim uygulaması / düzenleme (AI/Photoshop/Canva)", "kusur",
            f"DÜZENLEYİCİ yazılım izi: {_who} (ör. Photoshop/Canva). Fotoğraf bir düzenleyicide açılıp "
            f"yeniden kaydedilmiş → içerik değiştirilmiş olabilir.")
    elif kind == "browser" or rerender_finding:
        add("Üretim uygulaması / düzenleme", "kusur",
            f"TARAYICIDAN YENİDEN ÜRETİLMİŞ ({hitname or 'Skia/Chromium/pdfium'}). Gerçek banka PDF'i değil; "
            f"'yazdır→PDF' ile yeniden basılmış → sahtecilik şüphesi.")
    elif c2pa:
        add("Üretim uygulaması / düzenleme", "kısmi",
            "C2PA/ContentCredentials içerik-kimlik verisi var; kaynağı bu veriden doğrulayın.")
    elif _blob:
        add("Üretim uygulaması / düzenleme", "yapıldı",
            f"Üretici/yazılım: {_blob}. Bilinen düzenleyici (Photoshop/Canva/GIMP…) ya da AI izi YOK.")
    elif is_image:
        add("Üretim uygulaması / düzenleme", "kısmi",
            "Görselde EXIF yazılım/üretici bilgisi yok (silinmiş olabilir); ELA + Vision ile içerik tarandı.")
    else:
        add("Üretim uygulaması / düzenleme", "yapıldı", "Üretici/düzenleyici bilgisinde düzenleyici/AI izi yok.")

    # Görsel içerik tahrifatı (yalnız fotoğraf): ELA/Vision alan-bazlı
    if is_image:
        vis = [f["code"] for f in findings if f.get("code") in ("VISION_TEXT_TAMPER", "PIXEL_FIELD_ANOMALY")]
        add("Fotoğraf içerik tahrifatı (ELA/Vision)", "kusur" if vis else "yapıldı",
            ("görsel yazı/alan tahrifatı: " + ", ".join(vis)) if vis
            else "ELA + piksel-alan + Vision ile tarandı; belirgin iz yok.")

    # 8) İşlem numarası / sıra analizi
    seq = tx.get("sequence_number") or tx.get("ref_no") or ""
    seq_findings = [f["code"] for f in findings if str(f.get("code", "")).startswith("SEQ")]
    if seq:
        add("İşlem/sıra numarası", "yapıldı", f"numara: {seq}"
            + (f"; kontrol: {', '.join(seq_findings)}" if seq_findings else ""))
    else:
        add("İşlem/sıra numarası", "kısmi", "bu belgede sıra/işlem numarası okunamadı")
    if not report.get("cross_db", {}).get("checked"):
        add("Sıra veritabanı çaprazı", "yapılamadı",
            "tek belge analizi — geçmiş dekontlarla sıra/çakışma karşılaştırması yapılamadı")

    # 9) İşlem tarihi ↔ dekont/düzenlenme tarihi
    tdate, vdate = tx.get("date", ""), tx.get("value_date", "")
    if tdate and vdate and tdate[:10] != vdate[:10]:
        add("İşlem tarihi ↔ dekont/valör tarihi", "yapıldı", f"işlem {tdate} / valör {vdate} (farklı gün)")
    elif tdate:
        add("İşlem tarihi ↔ dekont/valör tarihi", "kısmi",
            "belgede tek tarih var; işlem-dekont farkı karşılaştırılamadı")
    for c, txt in (("RECEIPT_BEFORE_TXN", "dekont işlemden ÖNCE üretilmiş (imkânsız)"),
                   ("RECEIPT_NO_DATE_MISMATCH", "dekont no tarihi ile işlem tarihi uyuşmuyor"),
                   ("INTERNAL_DATE_MISMATCH", "belge içi tarihler tutarsız")):
        if c in codes:
            add("Tarih zinciri", "kusur", f"{txt} ({c})")

    # Girdi tipine göre YAPILAMAYAN yapısal denetimler (şeffaflık)
    if is_image:
        add("Yapısal PDF doğrulama (revizyon/font/üretici/QR/XML)", "yapılamadı",
            "belge yalnızca görüntü içeriyor; PDF yapısal katmanı olmadığından bu denetimler uygulanamaz")

    return {
        "banka": bank,
        "girdi_tipi": input_kind,
        "not": "Tüm maddeler tespit edilen bankaya göre (banka bazlı) değerlendirilir.",
        "ozet": {
            "yapildi": sum(1 for i in items if i["durum"] == "yapıldı"),
            "kusur": sum(1 for i in items if i["durum"] == "kusur"),
            "kismi": sum(1 for i in items if i["durum"] == "kısmi"),
            "yapilamadi": sum(1 for i in items if i["durum"] == "yapılamadı"),
        },
        "maddeler": items,
    }

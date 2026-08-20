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
    if rail_code:
        add("Kanal (EFT/FAST/HAVALE)", "yapıldı", f"{rail_map[rail_code]} olarak sınıflandı ({rail_code})")
    else:
        add("Kanal (EFT/FAST/HAVALE)", "kısmi",
            "Kanal kesinleştirilemedi (belirsiz) — açık EFT/FAST işareti ya da IBAN çifti yok")
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

    # 7) Üretim / sahtecilik analizi (görsel) — banka bazlı şablon + görsel adli
    if is_image:
        imf = report.get("image_forensics", {}) or {}
        soft = imf.get("exif_software") or ""
        vis = [f for f in findings if f.get("code") in ("VISION_TEXT_TAMPER", "PIXEL_FIELD_ANOMALY",
                                                        "IMAGE_EDIT_SIGNATURE", "IMAGE_EDITOR_SOFTWARE")]
        if vis:
            add("Fotoğraf üretim/tahrifat analizi", "kusur",
                "görsel tahrifat/düzenleyici izi: " + ", ".join(f["code"] for f in vis))
        else:
            add("Fotoğraf üretim/tahrifat analizi", "yapıldı",
                f"ELA + piksel-alan + Vision + AI-imza tarandı; belirgin iz yok"
                + (f" (EXIF yazılım: {soft})" if soft else ""))
    else:
        add("Fotoğraf üretim analizi", "yapılamadı", "belge görüntü değil (PDF); görsel adli analiz uygulanmaz")

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

#!/usr/bin/env python3
"""
REGRESYON BEKÇİSİ — Motorun kendi kendini denetlemesi.
=====================================================
Bu betik, IYILESTIRME_GUNLUGU.md içinde kayıtlı HER kritik iyileştirmeyi bir
"değişmez (invariant)" testine dönüştürür. Amaç: gelecekte bir güncelleme, daha
önce kazandığımız bir denetimi SESSİZCE EZERSE burada FAIL versin.

Kullanım:
    python3 regresyon_kontrol.py
Çıkış kodu 0 = tüm değişmezler korunuyor. !=0 = en az bir iyileştirme ezilmiş.

Yeni bir iyileştirme yaptığında: (1) IYILESTIRME_GUNLUGU.md'ye ekle,
(2) buraya onu koruyan bir test ekle. Böylece "analiz motorunu analiz eden yapı"
büyümeye devam eder.
"""
import sys, io, traceback

# --- import şimi: hem kaynak (app.engine.X) hem düz (X) yerleşimini destekle ---
try:
    from app.engine import (analyze, banks, ocr, authenticity, image_forensics,
                             reference_profiles)
    from app.engine.extract import Extraction
    LAYOUT = "kaynak (app.engine)"
except Exception:
    import analyze, banks, ocr, authenticity, image_forensics, reference_profiles  # type: ignore
    from extract import Extraction  # type: ignore
    LAYOUT = "düz (flat)"

RESULTS = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"İSTİSNA: {e}\n{traceback.format_exc(limit=2)}"
    RESULTS.append((name, ok, detail))


# =====================================================================
# 1) KRİTİK: Geçersiz IBAN Vision denetimini TETİKLEMELİ (sessizce onarıp atlamamalı)
#    2026-08-19'da bu açık üretilmiş ve düzeltilmiştir: IBAN onarımı Vision kararından
#    ÖNCE çalışırsa, checksum tutmayan (en şüpheli) dekont VISION_TEXT_TAMPER ve
#    vision-zenginleştirilmiş tüm metin denetimlerinden KAÇAR.
# =====================================================================
def _t_vision_escalates_on_bad_iban():
    # KONTROLLÜ VAKA: 4 kritik alan (alıcı adı/IBAN/tutar/tarih) TAMAMEN DOLU, ama alıcı IBAN
    # GEÇERSİZ (checksum tutmuyor). Böylece Vision'ı tetikleyebilecek TEK sebep _iban_bad'dir;
    # eksik-alan tetikleyicisi devre dışı. IBAN onarımı Vision'dan önce çalışırsa (eski hata),
    # IBAN geçerli olur, _iban_bad False olur, Vision ATLANIR → bu test yakalar.
    ocr_text = (
        "AKBANK T.A.S. Dekont\n"
        "Gonderen Ad Soyad: SINAN OZTURK\n"
        "Alici Ad Soyad: ELIF YILMAZ\n"
        "Gonderen IBAN: TR08 0006 4000 0012 2480 2487 85\n"
        "Alici IBAN: TR17 0015 7000 0000 0205 2180 58\n"   # 58 = GEÇERSİZ (gerçek 56)
        "Islem Tutari: 5.000,00 TL\n"
        "Islem Tarihi: 07.08.2026 13:20:00\n"
        "Alici Banka: Enpara\n")
    pdf = open("synthetic/akbank.pdf", "rb").read()
    img = ocr.render_page_to_image(pdf, 0, scale=2.0)
    buf = io.BytesIO(); img.save(buf, "PNG")
    wrapped, kind = analyze.prepare_input(buf.getvalue(), "x.png")

    from app.engine import vision_ocr
    calls = {"n": 0}
    _o_cfg, _o_ext = vision_ocr.is_configured, vision_ocr.extract_from_image
    _o_ocr, _o_avail = ocr.ocr_pdf_candidates, ocr.ocr_available
    vision_ocr.is_configured = lambda: True
    def _spy(pil, timeout=30.0):
        calls["n"] += 1
        return None
    vision_ocr.extract_from_image = _spy
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]   # OCR'ı kontrollü metne sabitle
    try:
        analyze.analyze_document(wrapped, "x.png", input_kind=kind, use_store=False)
    finally:
        vision_ocr.is_configured, vision_ocr.extract_from_image = _o_cfg, _o_ext
        ocr.ocr_pdf_candidates, ocr.ocr_available = _o_ocr, _o_avail
    return (calls["n"] >= 1,
            f"Vision çağrı sayısı={calls['n']} (4 alan dolu + IBAN geçersiz → Vision >=1 olmalı; "
            f"0 ise onarım Vision'ı EZMİŞ demektir)")


# =====================================================================
# 2) OCR TAM ÇÖZÜNÜRLÜK: fast modda bile küçük görsel ~1600px'e ölçeklenmeli
#    (1200'e düşürmek IBAN rakamlarını bozuyordu → 56'yı 58 okumak).
# =====================================================================
def _t_ocr_full_resolution():
    import numpy as np
    gray = (np.ones((400, 800), dtype="uint8") * 200)
    v = ocr._variants(gray, fast=True)
    h, w = v[0].shape[:2]
    return (max(h, w) >= 1500,
            f"fast varyant maks kenar={max(h,w)}px (>=1500 olmalı; <1300 ise çözünürlük EZİLMİŞ)")


# =====================================================================
# 3) IBAN ONARIM GÜVENLİĞİ: banka kodunu değiştirmez, benzersiz değilse tahmin etmez.
# =====================================================================
def _t_iban_repair_safety():
    a = banks.repair_iban_ocr("TR170015700000000205218058")  # hesap-kuyruğu hatası → benzersiz düzelir
    fixed_tail = (a == "TR170015700000000205218056")
    b = banks.repair_iban_ocr("TR910006200000000012345678")  # banka kodu bozuk → DOKUNMA
    kept_bankcode = (b == "TR910006200000000012345678")
    c = banks.repair_iban_ocr("TR080006400000122480248785")  # zaten geçerli → DOKUNMA
    kept_valid = (c == "TR080006400000122480248785")
    ok = fixed_tail and kept_bankcode and kept_valid
    return (ok, f"kuyruk-onar={fixed_tail}, banka-kodu-korundu={kept_bankcode}, geçerli-korundu={kept_valid}")


# =====================================================================
# 4) DİJİTAL PDF'DE IBAN ONARIMI ÇALIŞMAZ (orada geçersiz IBAN gerçek tahrifattır).
# =====================================================================
def _t_digital_pdf_no_repair():
    ex = Extraction(); ex.text_source = "digital"
    ex.receiver.iban = "TR170015700000000205218058"; ex.all_ibans = [ex.receiver.iban]
    fixes = analyze._repair_party_ibans(ex, "pdf")
    return (fixes == [] and ex.receiver.iban == "TR170015700000000205218058",
            f"dijital onarım={fixes} (boş olmalı; doluysa dijital tahrifat sinyali EZİLMİŞ)")


# =====================================================================
# 5) FOTOĞRAFTA IBAN_INVALID BASKILANIR (OCR yanlış okuması sahte bulgu üretmesin).
# =====================================================================
def _t_image_iban_invalid_suppressed():
    pdf = open("synthetic/akbank.pdf", "rb").read()
    img = ocr.render_page_to_image(pdf, 0, scale=2.0)
    buf = io.BytesIO(); img.save(buf, "PNG")
    wrapped, kind = analyze.prepare_input(buf.getvalue(), "x.png")
    rep = analyze.analyze_document(wrapped, "x.png", input_kind=kind, use_store=False)
    codes = [f.get("code") for f in rep.get("findings_tr", [])]
    return ("IBAN_INVALID" not in codes,
            f"IBAN_INVALID fotoğrafta {'YOK (doğru)' if 'IBAN_INVALID' not in codes else 'VAR — baskılama EZİLMİŞ'}")


# =====================================================================
# 6) RAIL SINIFLAMA: bankalararası + EFT işaretleri → 'eft' (FAST değil).
# =====================================================================
def _t_rail_eft():
    text = ("AKBANK Dekont  İşlem Türü: Bankalararası Para Transferi  "
            "Açıklama: GEÇ EFT  EFT Ücreti 16,76 TL")
    r = authenticity.classify_rail(text, "", "", "akbank")
    rail = (r or {}).get("rail")
    return (rail == "eft", f"classify_rail={rail} (bankalararası+EFT için 'eft' olmalı)")


# =====================================================================
# 7) AI-İMZA YANLIŞ-POZİTİFİ: ham metindeki 'gan' gibi 3-harf parçalar imza SAYILMAZ.
# =====================================================================
def _t_ai_signature_no_gan_fp():
    # kelime-sınırı denetimi: 'gan' düz metinde eşleşmemeli
    hit = image_forensics._sig_in("gan", "organ transplant management plan")
    return (hit is False, f"_sig_in('gan', düz-metin)={hit} (False olmalı; True ise 'gan' FP EZİLMİŞ)")


# =====================================================================
# 8) REFERANS PARMAK İZİ: VakıfBank masraf alanı 'always' TL (parmak izi korunuyor).
# =====================================================================
def _t_reference_vakif_fee():
    prof = reference_profiles.REFERENCE_PROFILES.get("vakif", {})
    fee = prof.get("fee_currency")
    val = fee[0] if isinstance(fee, (list, tuple)) else fee
    return (val == "always", f"vakif fee_currency={fee} ('always' olmalı)")


CHECKS = [
    ("1. Geçersiz IBAN → Vision tetiklenir (KRİTİK)", _t_vision_escalates_on_bad_iban),
    ("2. OCR tam çözünürlük (1600px)", _t_ocr_full_resolution),
    ("3. IBAN onarım güvenliği (banka kodu/benzersizlik)", _t_iban_repair_safety),
    ("4. Dijital PDF'de onarım yok", _t_digital_pdf_no_repair),
    ("5. Fotoğrafta IBAN_INVALID baskılanır", _t_image_iban_invalid_suppressed),
    ("6. Rail sınıflama: bankalararası+EFT → eft", _t_rail_eft),
    ("7. AI-imza 'gan' yanlış-pozitifi yok", _t_ai_signature_no_gan_fp),
    ("8. Referans parmak izi: VakıfBank masraf TL", _t_reference_vakif_fee),
]


def main():
    print(f"REGRESYON BEKÇİSİ — yerleşim: {LAYOUT}\n" + "=" * 60)
    for name, fn in CHECKS:
        check(name, fn)
    fails = 0
    for name, ok, detail in RESULTS:
        mark = "✅ GEÇTİ " if ok else "❌ EZİLMİŞ"
        print(f"{mark} | {name}")
        if not ok:
            fails += 1
            print(f"          ↳ {detail}")
    print("=" * 60)
    if fails:
        print(f"SONUÇ: {fails}/{len(RESULTS)} iyileştirme EZİLMİŞ — acilen bak!")
        sys.exit(1)
    print(f"SONUÇ: {len(RESULTS)}/{len(RESULTS)} iyileştirme korunuyor. ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()

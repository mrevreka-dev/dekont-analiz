"""
ÖZ-DENETİM (self-check) — motorun kendi kendini analiz etmesi.
==============================================================
Bu modül iki şeyi TEK KAYNAKTA tutar:

1) IMPROVEMENTS — "Bulunan Hata → Yapılan Değişiklik" geçmişi (günlük kayıt defteri).
2) run() — her kritik iyileştirmeyi koruyan "değişmez (invariant)" testleri. Bir güncelleme
   eski bir denetimi SESSİZCE ezerse ilgili test FAIL verir.

Kullanım (kod içi / test): from .self_check import run; run()
Kullanım (web): GET /api/v1/self_check  ve  GET /gunluk  bu modülü kullanır.

Testler DIŞ DOSYAYA BAĞIMLI DEĞİLDİR (canlı sunucuda da çalışır): Vision değişmezi için
bellek-içi boş PDF + kontrollü OCR metni kullanılır.
"""
from __future__ import annotations
import io
import traceback

# ------------------------------------------------------------------
# 1) GELİŞTİRME GÜNLÜĞÜ — "Bulunan Hata → Yapılan Değişiklik"
#    Yeni geliştirme = buraya yeni kayıt + run() içine yeni test.
# ------------------------------------------------------------------
IMPROVEMENTS = [
    {"id": "A", "date": "2026-08-19", "area": "Vision / tahrifat denetimi", "test": 1,
     "bug": "Daha önce yakalanan tahrifatlı dekont tekrar tarandığında 'doğru' göründü. IBAN "
            "onarımı Vision kararından ÖNCE çalışıyordu; geçersiz (en şüpheli) IBAN 'onarılınca' "
            "Vision hiç çağrılmıyor, VISION_TEXT_TAMPER ve Vision metnini kullanan tüm denetimler kayboluyordu.",
     "fix": "Vision kararı DAİMA ham OCR okumasına dayanır. IBAN onarımı yalnızca Vision hiç "
            "çalışamadığında devreye giren şeffaf yedektir; incelemeyi asla azaltmaz."},
    {"id": "B", "date": "2026-08-19", "area": "OCR çözünürlük", "test": 2,
     "bug": "IBAN yanlış okundu (…218056 → …218058), süre 15-20 sn. 'Hızlı OCR' çözünürlüğü "
            "1600→1200px, render 2.0→1.5 düşürüyordu; yoğun rakamlar bozuluyordu.",
     "fix": "Fast modda da tam 1600px + render 2.0. Hız yalnızca tek OCR varyantından gelir."},
    {"id": "C", "date": "2026-08-19", "area": "IBAN onarımı", "test": 3,
     "bug": "Tek-rakam OCR hatası IBAN'ı geçersiz kılıp yanlış sonuç/gereksiz Vision çağrısı üretiyordu.",
     "fix": "Görsel-karışan rakamları deneyip BENZERSİZ geçerli adaya onarır. Banka kodunu asla "
            "değiştirmez; dijital PDF'de çalışmaz; iban_ocr_onarim ile şeffaf."},
    {"id": "D", "date": "2026-08-19", "area": "Görünürlük", "test": None,
     "bug": "YZ denetleyicinin açık/kapalı olduğu dışarıdan görülemiyordu.",
     "fix": "/api/v1/health artık ai_adjudicator_enabled döndürür."},
    {"id": "E", "date": "önceki", "area": "Fotoğraf AI-imza FP", "test": 7,
     "bug": "JPEG ham baytlarında rastgele 'gan' 3-harfi AI-imza sanılıyor, orijinal dekontlar "
            "'yapay zeka üretimi' işaretleniyordu.",
     "fix": "İmza taraması yalnızca metadata (EXIF software + XMP), kelime-sınırıyla. 'gan' → 'stylegan'/'biggan'."},
    {"id": "F", "date": "önceki", "area": "Fotoğraf/OCR sahte bulgu", "test": 5,
     "bug": "Tek-rakam OCR hatası IBAN_INVALID, INTERNAL_DATE_MISMATCH, ID_CHECKSUM, "
            "RECEIPT_NO_DATE_MISMATCH, CONSISTENCY_FAIL sahte alarmları üretiyordu.",
     "fix": "Bu denetimler yalnızca dijital PDF'de gerçek tahrifat sayılır; fotoğraf/OCR/vision'da baskılanır."},
    {"id": "G", "date": "önceki", "area": "Rail (EFT/FAST/HAVALE)", "test": 6,
     "bug": "'Bankalararası' başlıklı EFT'ler FAST sanılabiliyordu; Akbank 'GEÇ EFT' kaçıyordu.",
     "fix": "classify_rail katmanlı sınıflama; aynı-banka+bankalararası çelişkisi; FAST 100.000 TL limit."},
    {"id": "H", "date": "önceki", "area": "Referans parmak izi", "test": 8,
     "bug": "Fotoğraf verisinin orijinal formatlardan sapması (ör. VakıfBank masrafta TL eksikliği) kaçıyordu.",
     "fix": "reference_profiles: orijinal PDF'lerden banka parmak izleri (para birimi, kimlik basamağı) ile kıyas."},
    {"id": "I", "date": "önceki", "area": "Banka bilgi tabanı + YZ", "test": None,
     "bug": "Her banka aynı mantıkla değerlendiriliyor, tahrifatta insan-gibi muhakeme yapılamıyordu.",
     "fix": "bank_knowledge (17 banka) + ai_adjudicator (kural → gerekiyorsa YZ; guardrail'li)."},
    {"id": "J", "date": "önceki", "area": "Performans", "test": None,
     "bug": "Tekrarlı sorgular tüm hattı yeniden çalıştırıyordu.",
     "fix": "Sonuç önbelleği (SHA-256 + motor sürümü): 12,7 sn → 0,004 sn. Hızlı OCR, fail-fast timeout."},
]

# İhlal edilmemesi gereken değişmez kurallar (bilgi amaçlı, web'de gösterilir)
INVARIANT_RULES = [
    "Servise dönen mevcut cevap key'leri ve URL'leri DEĞİŞMEZ — yeni çıktılar yalnızca EK alan.",
    "Adli araçta kanıt sessizce değiştirilmez; düzeltme şeffaf olmalı ve incelemeyi azaltmamalı.",
    "Bir güncelleme önceki denetimi kapatıyorsa bilinçli olmalı ve buraya + teste yazılmalı.",
]


# ------------------------------------------------------------------
# 2) DEĞİŞMEZ TESTLERİ
# ------------------------------------------------------------------
def _blank_pdf() -> bytes:
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (300, 300), "white").save(buf, "PDF")
    return buf.getvalue()


def _t1_vision_escalates_on_bad_iban():
    """KRİTİK: 4 alan dolu + IBAN GEÇERSİZ → Vision ÇAĞRILMALI (onarım Vision'ı ezmemeli)."""
    import analyze, ocr, vision_ocr
    from PIL import Image
    ocr_text = ("Gonderen Ad Soyad: SINAN OZTURK\n"
                "Alici Ad Soyad: ELIF YILMAZ\n"
                "Alici IBAN: TR17 0015 7000 0000 0205 2180 58\n"   # 58 = GEÇERSİZ
                "Islem Tutari: 5.000,00 TL\n"
                "Islem Tarihi: 07.08.2026 13:20:00\n"
                "Alici Banka: Enpara\n")
    calls = {"n": 0}
    o = (vision_ocr.is_configured, vision_ocr.extract_from_image,
         ocr.ocr_pdf_candidates, ocr.ocr_available, ocr.render_page_to_image)
    vision_ocr.is_configured = lambda: True
    def _spy(pil, timeout=30.0):
        calls["n"] += 1; return None
    vision_ocr.extract_from_image = _spy
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]
    ocr.render_page_to_image = lambda *a, **k: Image.new("RGB", (300, 300), "white")
    try:
        analyze.analyze_document(_blank_pdf(), "x.png", input_kind="image", use_store=False)
    finally:
        (vision_ocr.is_configured, vision_ocr.extract_from_image,
         ocr.ocr_pdf_candidates, ocr.ocr_available, ocr.render_page_to_image) = o
    return calls["n"] >= 1, f"Vision çağrısı={calls['n']} (>=1 olmalı; 0 ise Vision denetimi EZİLMİŞ)"


def _t2_ocr_full_resolution():
    import ocr
    import numpy as np
    v = ocr._variants(np.ones((400, 800), dtype="uint8") * 200, fast=True)
    m = max(v[0].shape[:2])
    return m >= 1500, f"fast varyant maks kenar={m}px (>=1500 olmalı)"


def _t3_iban_repair_safety():
    import banks
    a = banks.repair_iban_ocr("TR170015700000000205218058") == "TR170015700000000205218056"
    b = banks.repair_iban_ocr("TR910006200000000012345678") == "TR910006200000000012345678"
    c = banks.repair_iban_ocr("TR080006400000122480248785") == "TR080006400000122480248785"
    return (a and b and c), f"kuyruk-onar={a}, banka-kodu-korundu={b}, geçerli-korundu={c}"


def _t4_digital_pdf_no_repair():
    import analyze
    from extract import Extraction
    ex = Extraction(); ex.text_source = "digital"
    ex.receiver.iban = "TR170015700000000205218058"; ex.all_ibans = [ex.receiver.iban]
    fixes = analyze._repair_party_ibans(ex, "pdf")
    return (fixes == [] and ex.receiver.iban == "TR170015700000000205218058"), f"dijital onarım={fixes} (boş olmalı)"


def _t5_image_iban_invalid_suppressed():
    import analyze, ocr, vision_ocr
    from PIL import Image
    ocr_text = ("Alici Ad Soyad: ELIF YILMAZ\n"
                "Alici IBAN: TR17 0015 7000 0000 0205 2180 58\n"   # geçersiz
                "Islem Tutari: 5.000,00 TL\nIslem Tarihi: 07.08.2026\n")
    o = (vision_ocr.is_configured, ocr.ocr_pdf_candidates, ocr.ocr_available, ocr.render_page_to_image)
    vision_ocr.is_configured = lambda: False       # Vision kapalı ki OCR sonucu kalsın
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]
    ocr.render_page_to_image = lambda *a, **k: Image.new("RGB", (300, 300), "white")
    try:
        rep = analyze.analyze_document(_blank_pdf(), "x.png", input_kind="image", use_store=False)
    finally:
        (vision_ocr.is_configured, ocr.ocr_pdf_candidates, ocr.ocr_available, ocr.render_page_to_image) = o
    codes = [f.get("code") for f in rep.get("findings_tr", [])]
    return ("IBAN_INVALID" not in codes), f"IBAN_INVALID fotoğrafta {'YOK' if 'IBAN_INVALID' not in codes else 'VAR — baskılama EZİLMİŞ'}"


def _t6_rail_eft():
    import authenticity
    text = ("AKBANK İşlem Türü: Bankalararası Para Transferi  Açıklama: GEÇ EFT  EFT Ücreti 16,76 TL")
    r = authenticity.classify_rail(text, "", "", "akbank")
    rail = (r or {}).get("rail")
    return rail == "eft", f"classify_rail={rail} ('eft' olmalı)"


def _t7_ai_signature_no_gan_fp():
    import image_forensics
    hit = image_forensics._sig_in("gan", "organ transplant management plan")
    return hit is False, f"_sig_in('gan', düz-metin)={hit} (False olmalı)"


def _t8_reference_vakif_fee():
    import reference_profiles
    fee = reference_profiles.REFERENCE_PROFILES.get("vakif", {}).get("fee_currency")
    val = fee[0] if isinstance(fee, (list, tuple)) else fee
    return val == "always", f"vakif fee_currency={fee} ('always' olmalı)"


_CHECKS = [
    (1, "Geçersiz IBAN → Vision tetiklenir (KRİTİK)", _t1_vision_escalates_on_bad_iban),
    (2, "OCR tam çözünürlük (1600px)", _t2_ocr_full_resolution),
    (3, "IBAN onarım güvenliği (banka kodu/benzersizlik)", _t3_iban_repair_safety),
    (4, "Dijital PDF'de onarım yok", _t4_digital_pdf_no_repair),
    (5, "Fotoğrafta IBAN_INVALID baskılanır", _t5_image_iban_invalid_suppressed),
    (6, "Rail: bankalararası+EFT → eft", _t6_rail_eft),
    (7, "AI-imza 'gan' yanlış-pozitifi yok", _t7_ai_signature_no_gan_fp),
    (8, "Referans parmak izi: VakıfBank masraf TL", _t8_reference_vakif_fee),
]


def run() -> dict:
    """Tüm değişmez testlerini çalıştırır. Döner: özet + her testin sonucu + geliştirme günlüğü."""
    checks = []
    passed = 0
    for cid, name, fn in _CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"İSTİSNA: {e} | {traceback.format_exc(limit=1)}"
        if ok:
            passed += 1
        checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail})
    return {
        "all_ok": passed == len(_CHECKS),
        "passed": passed,
        "total": len(_CHECKS),
        "checks": checks,
        "improvements": IMPROVEMENTS,
        "invariant_rules": INVARIANT_RULES,
    }

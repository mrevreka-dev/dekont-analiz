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
import datetime
import traceback


def _now_tr() -> str:
    """Şu anki Türkiye saati (Europe/Istanbul) — 'YYYY-MM-DD HH:MM' biçiminde."""
    utc = datetime.datetime.now(datetime.timezone.utc)
    try:
        import zoneinfo
        tr = utc.astimezone(zoneinfo.ZoneInfo("Europe/Istanbul"))
    except Exception:
        tr = utc + datetime.timedelta(hours=3)      # yedek: sabit UTC+3
    return tr.strftime("%Y-%m-%d %H:%M")

# ------------------------------------------------------------------
# 1) GELİŞTİRME GÜNLÜĞÜ — "Bulunan Hata → Yapılan Değişiklik"
#    Yeni geliştirme = buraya yeni kayıt + run() içine yeni test.
# ------------------------------------------------------------------
IMPROVEMENTS = [
    {"id": "P", "date": "2026-08-20 05:00", "area": "Denetim Kapsamı (banka bazlı, şeffaf)", "test": 15,
     "bug": "Fotoğrafta IBAN/TC doğruluğu, tutar aritmetiği, işlem/sıra, işlem↔dekont tarihi gibi "
            "denetimlerin YAPILIP yapılmadığı ve YAPILAMAYANLAR raporda açıkça görünmüyordu. Ayrıca "
            "kural: tüm denetimler BANKA BAZLI olmalı.",
     "fix": "coverage.py eklendi → report['denetim_kapsami'] (API + /analyze web kartı). Her madde "
            "(kanal, IBAN mod-97, TC/VKN, taraf adları, tutar+ücret=toplam, işlem tarihi, fotoğraf "
            "üretim/tahrifat, işlem-sıra, işlem↔dekont tarihi) tespit edilen BANKAYA göre 'yapıldı/kusur/"
            "kısmi/yapılamadı(sebep)' olarak yazılır. Görüntüde yapısal PDF denetimi 'yapılamadı' işaretlenir."},
    {"id": "O", "date": "2026-08-20 04:25", "area": "Rail matrisi + metin-tabanlı HAVALE", "test": 14,
     "bug": "VakıfBank 'Hesaptan Havale' dekontunda gönderici IBAN maskeli olduğundan rail belirsiz "
            "kalıp bildirim çıkmıyordu. Ayrıca tüm banka tiplerinin rail sınıflaması tek bir testle "
            "korunmuyordu (bir banka düzeltmesi başka bankayı bozabilirdi).",
     "fix": "classify_rail'e metin-tabanlı HAVALE eklendi ('Hesaptan Havale' + interbank/EFT/FAST yok → "
            "havale). 9 banka tipini (Garanti/İşbank/Papara/VakıfBank/YapıKredi/Alternatif/Akbank/"
            "Denizbank) kapsayan rail matrisi test #14 ile kalıcı kilitlendi."},
    {"id": "N", "date": "2026-08-20 04:05", "area": "İnterbank-havale çelişkisi PUAN düşürür", "test": 13,
     "bug": "Bir işlem HAVALE olarak sunulup IBAN'lar farklı bankalarsa, sistem bunu açıkça yazıp "
            "puanı düşürmüyordu (kullanıcı kuralı: 'havale/fast değilse açıkça yaz ve puanı düşür').",
     "fix": "check_interbank_havale_contradiction eklendi: farklı bankalar + havale ücreti/kalemi + "
            "EFT/FAST yok → INTERBANK_HAVALE_CONTRADICTION (high, weight 30), skor tavanı 35. Akbank "
            "EFT-başlıklı genel şablon muaf (yanlış-pozitif yok)."},
    {"id": "M", "date": "2026-08-20 03:50", "area": "OTORİTER KURAL: interbank ≠ havale", "test": 12,
     "bug": "Dekont başlığında 'HAVALE' geçiyor ama IBAN'lar FARKLI bankalar (Akbank→Denizbank). "
            "_detect_garanti_kind başlıktaki 'HAVALE' kelimesini alıp işlem türünü 'HAVALE' gösteriyordu "
            "— oysa bankalararası bir işlem HAVALE OLAMAZ (havale banka-içidir).",
     "fix": "İki katman: (1) _detect_garanti_kind'te 'BANKALAR ARASI' varsa EFT/FAST, HAVALE'ye öncelikli. "
            "(2) analyze.py'de OTORİTER uzlaştırma: IBAN banka kodları farklıysa doc_kind asla HAVALE "
            "kalmaz, kanal kanıtına (EFT/FAST) göre düzeltilir. Banka-içi gerçek havale korunur."},
    {"id": "L", "date": "2026-08-20 03:35", "area": "Rail sınıflama — başlık-temelli EFT", "test": 11,
     "bug": "Akbank 'EFT BANKALAR ARASI HESABA HAVALE' dekontunda 'GEÇ EFT' ücret etiketi yoksa "
            "classify_rail 'belirsiz' dönüyor, rapora HİÇBİR kanal bulgusu düşmüyordu → kullanıcı "
            "'hiçbir şey bulamadı' görüyordu (78/100, tahrifat yok, ama EFT/FAST bilgisi yok).",
     "fix": "Başlıkta 'EFT BANKALAR ARASI' + işlem bankalararası + belgede HİÇBİR FAST işareti yoksa "
            "→ EFT (başlık-temelli, conf 75). FAST işareti varsa EFT'ye kaymaz (yanlış-pozitif koruması). "
            "Bildirim başlık-temelli tespiti dürüstçe belirtir ('büyük olasılıkla EFT, sıra no ile teyit)."},
    {"id": "K", "date": "2026-08-20 03:05", "area": "Türkçe İ hatası (SİSTEMİK KÖK)", "test": 9,
     "bug": "İki Akbank 'EFT BANKALAR ARASI HESABA HAVALE' dekontu denetimden geçti; banka Halkbank "
            "sanıldı, gönderici/alıcı isimleri boştu. Kök neden SİSTEMİK: 'İ'.lower() = 'i'+U+0307 "
            "(birleşik nokta) üretiyor; _issuer_ctx düz .lower() kullandığından 'AKBANK DİREKT' → "
            "'akbank di̇rekt' oluyor ve 'akbank direkt' imzası eşleşmiyordu. Aynı hata QNB('İNTERNET'), "
            "ING('ANONİM'), Garanti('GARANTİ') gibi İ-içeren imzaları da sessizce bozuyordu.",
     "fix": "Normalizasyonun KAYNAĞI İ-güvenli yapıldı: _issuer_ctx tüm anahtarlardan (low/nlow/up/"
            "zsig/lc_ns) U+0307'yi temizler. Artık imza hangi anahtarı kullanırsa kullansın İ hatası "
            "oluşmaz. Akbank branch'i 'Adı Soyad/Unvan' yazımını da tanır. Test #9 (İ-güvenli tespit) + "
            "#10 (Akbank EFT şablonu) bu hatanın geri gelmesini kalıcı olarak engeller."},
    {"id": "A", "date": "2026-08-20 02:23", "area": "Vision / tahrifat denetimi", "test": 1,
     "bug": "Daha önce yakalanan tahrifatlı dekont tekrar tarandığında 'doğru' göründü. IBAN "
            "onarımı Vision kararından ÖNCE çalışıyordu; geçersiz (en şüpheli) IBAN 'onarılınca' "
            "Vision hiç çağrılmıyor, VISION_TEXT_TAMPER ve Vision metnini kullanan tüm denetimler kayboluyordu.",
     "fix": "Vision kararı DAİMA ham OCR okumasına dayanır. IBAN onarımı yalnızca Vision hiç "
            "çalışamadığında devreye giren şeffaf yedektir; incelemeyi asla azaltmaz."},
    {"id": "B", "date": "2026-08-20 02:05", "area": "OCR çözünürlük", "test": 2,
     "bug": "IBAN yanlış okundu (…218056 → …218058), süre 15-20 sn. 'Hızlı OCR' çözünürlüğü "
            "1600→1200px, render 2.0→1.5 düşürüyordu; yoğun rakamlar bozuluyordu.",
     "fix": "Fast modda da tam 1600px + render 2.0. Hız yalnızca tek OCR varyantından gelir."},
    {"id": "C", "date": "2026-08-20 02:05", "area": "IBAN onarımı", "test": 3,
     "bug": "Tek-rakam OCR hatası IBAN'ı geçersiz kılıp yanlış sonuç/gereksiz Vision çağrısı üretiyordu.",
     "fix": "Görsel-karışan rakamları deneyip BENZERSİZ geçerli adaya onarır. Banka kodunu asla "
            "değiştirmez; dijital PDF'de çalışmaz; iban_ocr_onarim ile şeffaf."},
    {"id": "D", "date": "2026-08-20 01:58", "area": "Görünürlük", "test": None,
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


def _t9_turkish_i_safe_issuer():
    """KALICI: Türkçe 'İ'.lower() birleşik nokta (U+0307) üretir; imza eşleşmesini bozardı.
    İ içeren imzalarla banka tespiti DOĞRU çalışmalı. Biri _issuer_ctx'i düz .lower()'a döndürürse
    bu test yakalar (ör. Akbank 'AKBANK DİREKT', QNB 'İNTERNET', ING 'ANONİM')."""
    import extract as E
    cases = {
        "akbank": "İşlem AKBANK DİREKT üzerinden yapıldı",      # .com YOK, yalnız 'akbank direkt'
        "qnb": "QNB İNTERNET BANKACILIĞI",
        "ing": "ING BANK ANONİM ŞİRKETİ",
        "garanti": "HESAPTAN FAST GARANTİ BBVA",
    }
    bad = [f"{k}→{E.detect_issuer(v)}" for k, v in cases.items() if E.detect_issuer(v) != k]
    return (not bad), ("hepsi doğru" if not bad else f"YANLIŞ tespit: {bad} (İ hatası geri gelmiş)")


def _t10_akbank_eft_template():
    """Akbank 'EFT BANKALAR ARASI HESABA HAVALE' şablonu: banka=Akbank, gönderici/alıcı isimleri
    dolu, işlem EFT sınıflanmalı. (18-19 Ağustos'ta 2 dekontun denetimden geçmesine yol açan vaka.)"""
    from extract import extract_fields
    import authenticity
    txt = ("AKBANK\nEFT BANKALAR ARASI HESABA HAVALE\n"
           "Düzenleyen Şube : 7777 - AKBANK DİREKT MOBİL CEP\n"
           "Adı Soyad/Unvan : SEDAT BİRTAN\n"
           "ALICI BİLGİLERİ\nAlacaklı Hesap No : TR91 0001 2009 7660 0001 0378 74\n"
           "Adı Soyad/Unvan : Uğur Bibo\n"
           "GECEFT KOMİSYON 0,00 TL 15,96 TL\nGEC EFT BSMV 0,00 TL 0,80 TL\n")
    ex = extract_fields(txt, txt, None)
    rail = (authenticity.classify_rail(txt, "", "", "akbank") or {}).get("rail")
    ok = (ex.bank == "Akbank T.A.Ş." and bool(ex.sender.name) and bool(ex.receiver.name) and rail == "eft")
    return ok, (f"banka={ex.bank!r}, gönderici={ex.sender.name!r}, alıcı={ex.receiver.name!r}, rail={rail}")


def _t11_akbank_eft_title_based():
    """Akbank EFT dekontu 'GEÇ EFT' ücret etiketi OLMADAN (yalnız başlıkta EFT, bankalararası,
    hiçbir FAST işareti yok) → EFT sınıflanmalı. Aksi hâlde rapor 'hiçbir şey bulamadı' der.
    Ayrıca FAST işareti varsa EFT'ye KAYMAMALI (yanlış-pozitif koruması)."""
    import authenticity as A
    eft_txt = ("EFT BANKALAR ARASI HESABA HAVALE\nKOMİSYON 0,00 TL 7,97 TL\nBSMV 0,00 TL 0,40 TL\n")
    r1 = (A.classify_rail(eft_txt, "TR420004600121888000006245", "TR830013400002646836500002", "akbank") or {}).get("rail")
    # Karşı-koruma: FAST işareti varsa EFT DEĞİL fast olmalı
    fast_txt = "EFT BANKALAR ARASI HESABA HAVALE\nGiden FAST\nFAST Sorgu No: 123456\n"
    r2 = (A.classify_rail(fast_txt, "TR420004600121888000006245", "TR830013400002646836500002", "akbank") or {}).get("rail")
    ok = (r1 == "eft" and r2 == "fast")
    return ok, f"başlık-temelli EFT={r1} (eft olmalı), FAST-korumalı={r2} (fast olmalı)"


def _t12_interbank_never_havale():
    """OTORİTER KURAL: HAVALE banka-İÇİDİR. IBAN'lar FARKLI bankalarsa (bankalararası) işlem HAVALE
    OLAMAZ. classify_rail interbank'ta asla 'havale' dönmemeli; _detect_garanti_kind interbank EFT
    başlığını 'HAVALE' etiketlememeli. (Kullanıcının bildirdiği çelişki: başlıkta HAVALE ama farklı bankalar.)"""
    import authenticity as A
    from extract import _detect_garanti_kind
    # Bankalararası (Akbank 00046 → Denizbank 00134), başlıkta 'HAVALE' kelimesi geçen dekont
    txt = "EFT BANKALAR ARASI HESABA HAVALE\nKOMİSYON 7,97 TL\n"
    rail = (A.classify_rail(txt, "TR420004600121888000006245", "TR830013400002646836500002", "akbank") or {}).get("rail")
    kind = _detect_garanti_kind("EFT BANKALAR ARASI HESABA HAVALE")
    # Banka-İÇİ gerçek havale hâlâ 'HAVALE' olmalı (yanlış-düzeltme koruması)
    kind_intrabank = _detect_garanti_kind("HESABA HAVALE")
    ok = (rail != "havale" and kind != "HAVALE" and kind_intrabank == "HAVALE")
    return ok, f"interbank rail={rail} (havale OLMAMALI), interbank kind={kind} (HAVALE OLMAMALI), banka-içi kind={kind_intrabank} (HAVALE olmalı)"


def _gen_iban(bankcode, acc="0000000000012345"):
    body = bankcode + "0" + acc                        # 5+1+16 = 22 hane BBAN
    rear = body + "TR" + "00"
    digits = "".join(c if c.isdigit() else str(ord(c) - 55) for c in rear)
    cd = 98 - (int(digits) % 97)
    return "TR%02d%s" % (cd, body)


def _t13_interbank_havale_penalized():
    """Farklı bankalar arası bir işlem HAVALE olarak sunuluyorsa (havale ücreti/kalemi, EFT/FAST yok)
    → INTERBANK_HAVALE_CONTRADICTION bulgusu üretilmeli ve PUANI DÜŞÜRMELİ. Akbank EFT-başlıklı
    genel şablon (interbank ama 'EFT/bankalararası' ibaresi taşır) bu cezadan MUAF olmalı."""
    import authenticity as A
    a, b = _gen_iban("00046"), _gen_iban("00134")           # farklı bankalar, geçerli IBAN
    hit = A.check_interbank_havale_contradiction("HAVALE DEKONTU\nHavale Ücreti 8,37 TL\n", a, b)
    fired = bool(hit) and hit.get("code") == "INTERBANK_HAVALE_CONTRADICTION" and hit.get("weight", 0) >= 20
    # Akbank EFT şablonu YANLIŞ-POZİTİF üretmemeli
    fp = A.check_interbank_havale_contradiction("EFT BANKALAR ARASI HESABA HAVALE\nKOMİSYON 7,97 TL\n", a, b)
    ok = fired and (fp is None)
    return ok, f"çelişki tetikledi={fired} (w={hit.get('weight') if hit else '-'}), Akbank-EFT yanlış-poz={fp is not None} (False olmalı)"


def _t14_rail_matrix_all_banks():
    """TÜM banka dekont tiplerinin rail (EFT/FAST/HAVALE) sınıflaması — geniş matris. Bir banka-özel
    değişiklik başka bir bankanın rail'ini bozarsa bu test yakalar. (Kullanıcının attığı gerçek
    dekont setinden türetildi: Garanti/İşbank/Papara/VakıfBank/YapıKredi/Alternatif/Akbank/Denizbank.)"""
    import authenticity as A
    a = _gen_iban
    cases = [
        ("Garanti FAST", "HESAPTAN FAST\nFAST REF NO 584000018\nMASRAF 7,97 BSMV 0,40", a("00062"), a("00015"), "fast"),
        ("İşbank Giden Fast", "e-Dekont\nGiden Fast İşlemi\nFAST Ücreti ve Vergi 8,37", a("00064"), a("00111"), "fast"),
        ("Papara FAST", "FAST Para Transferi\nİşlem Türü FAST Para Transferi", a("00082"), a("00134"), "fast"),
        ("VakıfBank FAST Anlık", "İŞLEM TÜRÜ FAST Giden Anlık Ödeme\nMASRAF TUTARI 16,76", a("00015"), a("00046"), "fast"),
        ("YapıKredi karışık EFT/FAST", "FAST GÖNDERİMİ\nDEKONT TİPİ : EFT\nGİDEN FAST TUTARI -8600\nAÇIKLAMA:ELEKTRONİK FON TRANSFERİ (EFT) ÜCRETİ - FAST/", a("00067"), a("00134"), "fast"),
        ("Alternatif Giden FAST", "İŞLEM TÜRÜ Giden FAST Ödemesi\nFAST Sorgu Numarası 12985127", a("00124"), a("00067"), "fast"),
        ("Akbank GECEFT", "EFT BANKALAR ARASI HESABA HAVALE\nGECEFT KOMİSYON 15,96\nGEC EFT BSMV 0,80", a("00046"), a("00067"), "eft"),
        ("Akbank EFT başlık", "EFT BANKALAR ARASI HESABA HAVALE\nKOMİSYON 7,97\nBSMV 0,40", a("00046"), a("00134"), "eft"),
        ("VakıfBank Hesaptan Havale", "İŞLEM Hesaptan Havale", a("00015"), a("00015", "0000000000099999"), "havale"),
    ]
    wrong = []
    for name, txt, s, r, exp in cases:
        got = (A.classify_rail(txt, s, r, "") or {}).get("rail")
        if got != exp:
            wrong.append(f"{name}: beklenen={exp} bulunan={got}")
    return (not wrong), ("9/9 doğru" if not wrong else f"YANLIŞ: {wrong}")


def _t15_coverage_bank_based():
    """Denetim kapsamı BANKA BAZLI üretilmeli; her ana madde (kanal, IBAN doğruluğu, kimlik, taraf
    adları, tutar aritmetiği, işlem tarihi, fotoğraf üretim analizi) yer almalı; görüntüde
    YAPILAMAYAN yapısal PDF denetimi açıkça işaretlenmeli."""
    import coverage
    rep = {
        "classification": {"input_kind": "image", "text_source": "ocr"},
        "extracted": {
            "bank": "Akbank T.A.Ş.",
            "sender": {"name": "MEHMET ERGİN", "iban": "TR670004600002888000078637", "tckn": "14636431312"},
            "receiver": {"name": "Yiğithan Özden", "iban": "TR720006200074700006602748"},
            "amount": {"value": 75000.0, "fee": 16.76, "total": 75016.76},
            "transaction": {"date": "15.08.2026 18:57:56", "value_date": "15.08.2026", "sequence_number": ""},
        },
        "findings_tr": [{"code": "RAIL_IS_EFT"}],
        "image_forensics": {"exif_software": ""},
        "cross_db": {"checked": False},
    }
    cov = coverage.build(rep)
    alanlar = " | ".join(m["alan"] for m in cov["maddeler"])
    have_bank = cov.get("banka") == "Akbank T.A.Ş."
    have_rail = "Kanal (EFT/FAST/HAVALE)" in alanlar
    have_iban = "Alıcı IBAN doğruluğu (mod-97)" in alanlar
    have_amount = any("aritmet" in m["alan"].lower() for m in cov["maddeler"])
    have_prod = any("üretim" in m["alan"].lower() or "tahrifat" in m["alan"].lower() for m in cov["maddeler"])
    have_cannot = any(m["durum"] == "yapılamadı" and "Yapısal PDF" in m["alan"] for m in cov["maddeler"])
    ok = all([have_bank, have_rail, have_iban, have_amount, have_prod, have_cannot])
    return ok, (f"banka={have_bank}, kanal={have_rail}, iban={have_iban}, tutar={have_amount}, "
                f"üretim={have_prod}, yapılamadı-yazıldı={have_cannot}")


_CHECKS = [
    (1, "Geçersiz IBAN → Vision tetiklenir (KRİTİK)", _t1_vision_escalates_on_bad_iban),
    (2, "OCR tam çözünürlük (1600px)", _t2_ocr_full_resolution),
    (3, "IBAN onarım güvenliği (banka kodu/benzersizlik)", _t3_iban_repair_safety),
    (4, "Dijital PDF'de onarım yok", _t4_digital_pdf_no_repair),
    (5, "Fotoğrafta IBAN_INVALID baskılanır", _t5_image_iban_invalid_suppressed),
    (6, "Rail: bankalararası+EFT → eft", _t6_rail_eft),
    (7, "AI-imza 'gan' yanlış-pozitifi yok", _t7_ai_signature_no_gan_fp),
    (8, "Referans parmak izi: VakıfBank masraf TL", _t8_reference_vakif_fee),
    (9, "Türkçe İ-güvenli banka tespiti (KALICI)", _t9_turkish_i_safe_issuer),
    (10, "Akbank EFT şablonu doğru ayrıştırılır", _t10_akbank_eft_template),
    (11, "Başlık-temelli EFT (GEÇ EFT etiketi olmadan)", _t11_akbank_eft_title_based),
    (12, "Bankalararası işlem ASLA havale olamaz", _t12_interbank_never_havale),
    (13, "İnterbank-havale çelişkisi puanı düşürür", _t13_interbank_havale_penalized),
    (14, "Rail matrisi: 9 banka tipi doğru (EFT/FAST/HAVALE)", _t14_rail_matrix_all_banks),
    (15, "Denetim kapsamı banka bazlı + yapılamayanı yazar", _t15_coverage_bank_based),
]


def run() -> dict:
    """Tüm değişmez testlerini çalıştırır. Döner: özet + her testin sonucu + geliştirme günlüğü."""
    # Her testi, onu doğuran geliştirme kaydıyla eşleştir → o iyileştirmenin tarih+saati
    _date_by_test = {it["test"]: it["date"] for it in IMPROVEMENTS if it.get("test")}
    checks = []
    passed = 0
    for cid, name, fn in _CHECKS:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"İSTİSNA: {e} | {traceback.format_exc(limit=1)}"
        if ok:
            passed += 1
        checks.append({"id": cid, "name": name, "ok": bool(ok), "detail": detail,
                       "date": _date_by_test.get(cid, "")})
    return {
        "all_ok": passed == len(_CHECKS),
        "passed": passed,
        "total": len(_CHECKS),
        "generated_at": _now_tr(),          # en son denetim tarih+saati (Türkiye)
        "generated_tz": "Europe/Istanbul",
        "checks": checks,
        "improvements": IMPROVEMENTS,
        "invariant_rules": INVARIANT_RULES,
    }

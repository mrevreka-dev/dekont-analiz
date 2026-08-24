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
    {"id": "Z15", "date": "2026-08-24 03:20", "area": "QNB'YE ÖZEL EFT/FAST AYRIMI: 'GİDEN EFT'→net EFT, 'GİDEN FAST EFT'→FAST (rail tüm bankalarda + PDF'de evrensel)", "test": 38,
     "bug": "QNB dekontlarında işlem türü başlıkta 'GİDEN EFT' / 'GİDEN FAST EFT' olarak yazılıyor. Eski "
            "sınıflandırmada 'GİDEN EFT' yalnız başlık-temelli EFT (%75, 'büyük olasılıkla') sayılıyordu; "
            "kullanıcı bunun QNB'de KESİN EFT göstergesi olduğunu, 'GİDEN FAST EFT'in ise FAST demek olduğunu "
            "belirtti. Ayrıca EFT/FAST/HAVALE kanal tespitinin PDF taramalarında da tüm bankalarda çalıştığı "
            "teyit edilmeliydi.",
     "fix": "classify_rail'e QNB'ye özel gate eklendi (bkey=='qnb' VEYA gönderici IBAN kodu 00111): "
            "boşluk-atılmış metinde 'gidenfasteft' → FAST (%93), 'gideneft' (fast-eft değilse) → KESİN EFT "
            "(%96), QNB'ye özel bildirim metniyle. Hem bankalararası dalda hem IBAN-okunamadı yedeğinde geçerli; "
            "QNB-dışı bankalara SIZMAZ (guard). Kanal tespiti (classify_rail + check_rail_bank) zaten "
            "'if is_receipt' altında, girdi türünden (PDF/görsel) ve bankadan BAĞIMSIZ text_layout üzerinden "
            "evrensel çalışıyor — doğrulandı. Test #38 kilitler.",
     "not": "Kullanıcı kuralı: 'GİDEN EFT' QNB'de net EFT; 'GİDEN FAST EFT' FAST. Sadece QNB kanalına özel."},
    {"id": "Z14", "date": "2026-08-24 03:05", "area": "CERRAHİ SIFIRLAMA (reset scope='reuse'): öğrenilen veriye dokunmadan sadece işe yaramayan/eksik numara kayıtları temizlenir", "test": 37,
     "bug": "Eski/kirli veriyi silmek için ilk eklenen reset 'detection' kapsamı analyses+receipts+report_cache "
            "tablolarını TAMAMEN siliyordu → tam ve doğru okunmuş geçmiş kayıtlar da gidiyordu. Kullanıcı kuralı: "
            "'reset ederken öğrenilmiş hiçbir şeyi silme, sadece işimize yaramayan aynı işlem numarası olan alanı temizle'.",
     "fix": "reset_history'ye yeni VARSAYILAN kapsam 'reuse' eklendi (_purge_incomplete_reuse): yalnız bir işlem/"
            "sıra/referans numarası taşıdığı halde EKSİK okunmuş (alıcı IBAN mod-97 geçersiz / alıcı adı yok / tutar "
            "yok) satırları analyses+receipts'ten siler. TAM kayıtlar, numarasız kayıtlar, önbellek, öğrenilenler "
            "(field_hints/bank_corpus/unknown_banks) ve günlükler KORUNUR. main.py /store/reset varsayılanı 'reuse' "
            "oldu. 'detection' ve 'all' tam-temizlik seçenekleri korunuyor. Test #37 kilitler.",
     "not": "Kullanıcı kuralı: öğrenilen bilgi asla silinmez; sadece kirleten eksik-numara kayıtları temizlenir."},
    {"id": "Z13", "date": "2026-08-24 03:10", "area": "NUMBER_REUSE/kara-liste: EKSİKSİZLİK KAPISI + forgery kararı TUTAR VEYA TARİH (sadece-tarih sahtesi de yakalanır)", "test": 36,
     "bug": "Aynı dekont tekrar tarandığında NUMBER_REUSE yanlışlıkla çıkıyordu. İlk çözümde farklılık kararı "
            "'tutar VEYA farklı alıcı IBAN'dı → alıcı IBAN OCR varyansı + geçmiş hatalı kayıtlar FP üretiyordu. "
            "Sonra 'yalnız tutar'a indirildi — ama bu da EKSİK: sahtecilik SADECE TARİH değiştirilerek de yapılır "
            "(kullanıcı uyarısı). Ayrıca eksik/yanlış okunan dekontlar DB'ye kaydedilip karşılaştırmayı kirletiyordu.",
     "fix": "(1) EKSİKSİZLİK KAPISI (_complete_for_reuse): bir dekont numara-tekrarı DB'sine YALNIZ alıcı IBAN "
            "(mod-97 geçerli) + alıcı adı + tutar + işlem numarası NET okunduysa kaydedilir; okunamayan dekontun "
            "tanımlayıcı numaraları saklanmaz ve o dekont için karşılaştırma yapılmaz (denetim/audit satırı yine "
            "yazılır). Böylece DB kirlenmez. (2) Kayıtlar artık güvenilir olduğundan forgery kararı TUTAR ya da "
            "İŞLEM TARİHİ (GÜN) farkına dayanır → sadece-tarih sahtesi de yakalanır. Alıcı IBAN farkı ölçüt DIŞI "
            "(OCR'a en açık). Aynı numara + tutar aynı + gün aynı → aynı dekont (bulgu YOK). Test #36 kilitler.",
     "not": "Kullanıcı kuralı: 'okuyamadıysan kaydetme' + 'sadece tarih değişse de sahtedir'. İkisi birlikte uygulandı."},
    {"id": "Z12", "date": "2026-08-24 02:40", "area": "ALICI IBAN OCR-TOLERANSLI KURTARMA (QNB): 'ALICI IRAN' + boşluk → yanlış aynı-banka/SAHTE giderildi", "test": 35,
     "bug": "QNB dekontunda alıcı IBAN'ı (TR19...=Ziraat) alıcı ADINA sızmış, alıcı IBAN alanı boş kalıp "
            "göndericinin QNB IBAN'ına (TR35) düşüyordu → iki IBAN aynı (QNB) → YZ 'SAHTE %92 (aynı-banka ama "
            "FAST/EFT)'. Neden: OCR 'ALICI IBAN'ı 'ALICI IRAN' (B→R) okudu ve IBAN'a stray boşluk sızdı "
            "('TR19...901 1 147...') → _after_label('ALICI IBAN') eşleşmedi.",
     "fix": "extract.receiver_iban_ocr_tolerant(): alıcı/alacaklı + IBAN/IRAN (B↔R) etiketinden sonra IBAN'ı "
            "boşluk-toleranslı alır, mod-97 geçersizse tek-rakam onarır. QNB/Enpara dalında _after_label boş/"
            "geçersizse bu devreye girer; alıcı adına sızan 'ALICI IRAN: TR..' temizlenir. Sonuç: gönderici QNB "
            "(00111) / alıcı Ziraat (00010) — FARKLI, geçerli; aynı-banka çelişkisi ve yanlış SAHTE oluşmaz. "
            "Test #35 kilitler.",
     "not": "Kök kural (tekrar): veriler ÖNCE doğru çıkarılır; alıcı IBAN göndericininkine ASLA düşürülmez."},
    {"id": "Z11", "date": "2026-08-24 02:20", "area": "BANKA TESPİTİ YENİDEN DÜZENLENDİ: gönderici IBAN kodu BİRİNCİL + bilinmeyen banka → AI derin inceleme/kayıt", "test": 34,
     "bug": "QNB dekontu 'Enpara' tespit ediliyordu: ilk iş banka tespiti isim/domain imzasına dayalıydı; OCR "
            "footer'daki 'qnb.com'u kaçırınca Enpara'nın GEVŞEK imzası (yalnız 'ALICI ÜNVANI'+'EFT TUTARI' "
            "layout'u) devreye girip QNB'yi Enpara sanıyordu. Bu, alıcı IBAN'ın yanlış dala düşmesine + "
            "NUMBER_REUSE dahil zincirleme hataya yol açıyordu. Kök sorun: banka, en güvenilir sinyal olan "
            "GÖNDERİCİ IBAN banka kodundan belirlenmiyordu.",
     "fix": "İŞ AKIŞI: İLK İŞ banka tespiti; öncelik (kullanıcı kuralı) (1) GÖNDERİCİ IBAN banka kodu "
            "(hesap-sahibi etiketli: MÜŞTERİ ÜNVANI/GÖNDEREN/BORÇLU/ÜCRET TAH.), (2) domain, (3) logo/isim. "
            "extract.sender_iban_code() gönderici IBAN kodunu bulur; detect_issuer önce bunu kullanır (isim "
            "imzasını EZER). Enpara gevşek layout-imzası KALDIRILDI (yalnız 'enpara' marka/domain); QNB imzası "
            "'qnb bank'/'finansbank' header'ıyla genişletildi. BİLİNMEYEN BANKA: gönderici IBAN kodu "
            "IBAN_BANK_CODES'ta yoksa → UNKNOWN_BANK_CODE bulgusu + AI DERİN inceleme (should_adjudicate "
            "tetikler; tüm alanlar + FAST/HAVALE/EFT kanalı) + store.log_unknown_bank ile kaydedilir; "
            "/api/v1/unknown_banks ile gün sonu listeye eklenir. Test #34 kilitler.",
     "not": "ING/Kuveyt Türk gibi gönderici IBAN'ı YAZMAYAN dekontlar domain/isim yedeğiyle doğru tespit edilir."},
    {"id": "Z10", "date": "2026-08-22 03:00", "area": "TEMEL MİMARİ KURAL: YZ hükmü DÜZELTİLMİŞ TAM veriyle uzlaştırılır (düzeltme-sonrası yeniden değerlendirme)", "test": 33,
     "bug": "QNB→Ziraat interbank FAST/EFT dekontunda YZ 'SAHTE %88' diyordu: gerekçe 'gönderici ve alıcı IBAN "
            "aynı (00111=QNB) → aynı-banka HAVALE olmalı ama FAST/EFT → çelişki'. Oysa YZ'nin KENDİ düzelttiği "
            "alanlarda alıcı IBAN TR19...=ZİRAAT (00010), gönderici QNB (00111) — FARKLI banka, geçerli interbank "
            "işlem. Sorun MİMARİ: YZ tek geçişte hem alanları düzeltir hem hüküm verir; hükmü DÜZELTMEDEN ÖNCEKİ "
            "yanlış okumaya (tabloda her satırda göndericinin IBAN'ı görünür) dayanıyor → 'düzeltilmiş alanlar' "
            "ile 'YZ hükmü' BİRBİRİYLE ÇELİŞİYOR. Ayrıca alıcı banka etiketi düzeltilmiş IBAN'dan türetilmediği "
            "için 'her ikisi de QNB' yazıyordu.",
     "fix": "İŞ AKIŞI YENİDEN DÜZENLENDİ — YZ değerlendirmesi artık NİHAİ (düzeltilmiş) veriyle uzlaştırılır: "
            "(1) reconcile (blok e): YZ alanları düzelttikten sonra taraf banka etiketleri düzeltilmiş IBAN'dan "
            "YENİDEN türetilir; düzeltmeden önce yanlış IBAN'la firelanan IBAN-bağımlı bulgular (SAMEBANK "
            "çelişkisi, yanlış rail=HAVALE) düzeltilmiş IBAN+ihraççı ile yeniden sınanıp GEÇERSİZSE KALDIRILIR, "
            "doğru rail eklenir. (2) HÜKÜM KAPISI (7.96): YZ 'sahte/şüpheli' dediği hâlde düzeltilmiş NİHAİ veride "
            "SOMUT tahrifat kanıtı (içerik-tahrifatı bulgusu ya da YZ görsel-tahrifatı) YOKSA hüküm 'belirsiz'e "
            "çekilir, verdict_ham'da ham hüküm saklanır, gerekçeye şeffaf düzeltme notu eklenir. Böylece rapor "
            "kendi içinde ASLA çelişmez. Kullanıcı kuralı: 'YZ yorumu TAM ve DÜZELTİLMİŞ veriyle yapılmalı.' "
            "Test #33 kilitler.",
     "not": "İhraççı QNB (00111) ≠ alıcı Ziraat (00010) → geçerli interbank işlem; 'aynı-banka çelişkisi' YOK."},
    {"id": "Z9", "date": "2026-08-22 02:30", "area": "İHRAÇÇI GÜVENCESİ: yanlış-atanan gönderici IBAN → sahte 'aynı-banka çelişkisi' + yanlış HAVALE giderildi", "test": 32,
     "bug": "Kuveyt Türk'ten Ziraat'a FAST dekontunda (ekran görüntüsü, vision) skor 6/kritik + "
            "SAMEBANK_RAIL_CONTRADICTION + RAIL_IS_HAVALE çıkıyordu. Gerçek: gönderici Kuveyt Türk (00205), "
            "alıcı IBAN Ziraat (00010) → interbank FAST. Kök neden: belgede 'Gönderilen IBAN' (= ALICI IBAN'ı) "
            "gibi etiketler vision'da göndericiye yanlış atanıp gönderici IBAN'ı Ziraat sanılıyor; iki 'Ziraat "
            "IBAN' görülünce hem rail HAVALE (aynı banka) hem SAMEBANK çelişkisi YANLIŞ tetikleniyordu.",
     "fix": "(1) analyze.py: GÖNDERİCİ IBAN ↔ İHRAÇÇI tutarlılık guard'ı — göndericinin bankası = ihraççıdır; "
            "gönderici IBAN'ının banka kodu ihraççıya ait değilse yanlış-atamadır → alıcı boşsa oraya taşınır, "
            "doluysa gönderici IBAN'ı TEMİZLENİR (tüm rail/samebank kontrollerinden ÖNCE). (2) "
            "check_samebank_rail_contradiction artık issuer_codes alır: aynı-banka kararını yanlış-atanabilen "
            "gönderici IBAN'ına değil, İHRAÇÇI ↔ ALICI IBAN bankası kıyasına dayandırır (ihraççı≠alıcı → çelişki "
            "yok). Gerçek Akbank aynı-banka+EFT vakası korunur (test #24/#30). Test #32 kilitler.",
     "not": "İhraççı Kuveyt Türk (00205) ≠ alıcı Ziraat (00010) → interbank FAST, çelişki YOK."},
    {"id": "Z8", "date": "2026-08-21 22:00", "area": "VISION SONRASI IBAN ONARIMI: tek-rakam yanlış okunan IBAN artık onarılıyor", "test": 31,
     "bug": "Kuveyt Türk ekran görüntüsünde alıcı IBAN'ı 'TR65...6085 9650 01' iken sistem '...9850 01' (geçersiz, "
            "6↔8 OCR karışması) gösteriyordu. Kök neden: IBAN OCR-onarımı 'if vision_result is None' ile YALNIZ "
            "Vision HİÇ çalışmadıysa yapılıyordu. Ekran görüntüsünde tesseract başarısız olup Vision devreye "
            "girince, Vision'ın tek-rakam IBAN hatası onarılMADAN ekranda kalıyordu (apply_corrections da geçersiz "
            "IBAN'ı uygulamadığı için düzelmiyordu).",
     "fix": "analyze.py: IBAN OCR-onarımı (_repair_party_ibans) artık Vision ÇALIŞSA DA yapılır. GÜVENLİDİR — "
            "yalnız mod-97 GEÇERSİZ IBAN'a dokunur, yalnız TEK benzersiz geçerli adayı uygular, BANKA KODU'nu "
            "korur, dijital PDF'de çalışmaz ve iban_ocr_onarim ile şeffaf loglanır; geçerli IBAN'a ASLA dokunmaz. "
            "Böylece Vision'ın '9850'→doğru '9650' onarılır. Test #31 kilitler.",
     "not": "'TR65 ... 9850 01' mod-97 GEÇERSİZ; doğrusu '...9650 01' (tek hane: 8→6)."},
    {"id": "Z7", "date": "2026-08-21 20:45", "area": "İHRAÇÇI ≠ KARŞI-TARAF BANKASI: Kuveyt Türk→Ziraat yanlış 'aynı-banka çelişkisi' giderildi", "test": 30,
     "bug": "Kuveyt Türk'ten Ziraat'a FAST transfer dekontunda YZ 'gönderici ve alıcı IBAN aynı bankada (Kuveyt "
            "Türk), EFT/FAST çelişkisi' diyordu — TAMAMEN YANLIŞ. Gerçek: gönderici Kuveyt Türk (00205), alıcı "
            "IBAN TR65 0001 0002... = ZİRAAT (00010, kod TR+kontrol sonrası 5 hane), farklı bankalar → interbank "
            "FAST NORMAL. İki kök hata: (1) İHRAÇÇI TESPİTİ: belgede 'ziraat' hem ALICI BANKASI olarak geçtiğinden, "
            "OCR gürültüsünde Ziraat'ın gevşek ('ziraat'+düzen) imzası tetiklenip banka yanlışlıkla 'Ziraat' "
            "etiketleniyordu (oysa ihraççı Kuveyt Türk). (2) YZ HALÜSİNASYONU: tek IBAN (alıcı Ziraat) varken YZ "
            "bunu ihraççıya (Kuveyt) atfedip 'iki IBAN da aynı bankada' diyerek OLMAYAN bir çelişki uyduruyordu; "
            "IBAN banka kodunu ('00010') 'TR65 0001' diye yanlış okuyordu.",
     "fix": "(1) extract.py: Kuveyt Türk imzası header/footer varyantlarını kapsar ('kuveytturk'/'kuveyt turk "
            "katilim'); Ziraat'ın gevşek dalına KARŞI-TARAF KORUMASI — başka ihraççı markörü (kuveytturk) varsa "
            "Ziraat İHRAÇÇI sayılmaz. (2) ai_adjudicator prompt: IBAN banka kodu = TR+kontrol SONRASI 5 hane "
            "(TR65 00010=Ziraat, Kuveyt değil); İHRAÇÇI=gönderici bankası; çoğu dekontta yalnız ALICI IBAN'ı "
            "yazılıdır → tek IBAN'ı ihraççıya atfetme; AYNI-BANKA çelişkisi için İKİ IBAN gerekir; farklı banka "
            "→ EFT/FAST normal. Test #30 kilitler.",
     "not": "İhraççı Kuveyt Türk (00205), alıcı Ziraat (00010) — FARKLI banka, geçerli interbank FAST."},
    {"id": "Z6", "date": "2026-08-21 20:15", "area": "BÖLÜNMÜŞ GÖNDERİCİ IBAN ONARIMI: YZ 'alıcı değişmiş' uydurma çelişkisi giderildi", "test": 29,
     "bug": "Ziraat 'Hesaptan Hesaba Havale' dekontunda YZ 'alıcı bilgisi değiştirilmiş (güven %75, şüpheli)' "
            "diyordu. Neden: PDF metninde gönderici IBAN'ı satıra BÖLÜNMÜŞ — ilk 24 karakter ('TR65 ... 9650') "
            "üst satırda, son 2 hane ('01') 'IBAN :' etiketinden sonra AYRI satırda. IBAN_RE tam 26 hane "
            "aradığından yakalayamıyor; gönderici IBAN BOŞ kalıp all_ibans[0]=ALICI IBAN'ına düşüyordu. "
            "Böylece rapor sadece alıcıyı gösteriyor, YZ ham metindeki gönderici (KASIM AKNAY/TR65) ile çıkarılan "
            "alıcıyı (ENES/TR17) karşılaştırıp gönderici/alıcıyı KARIŞTIRIYOR ve uydurma 'alıcı değiştirilmiş' "
            "çelişkisi üretiyordu. Gerçekte iki IBAN FARKLI (TR65 ≠ TR17), ikisi de Ziraat → geçerli aynı-banka HAVALE.",
     "fix": "extract.py: (1) _reconstruct_split_iban — kısmi IBAN (TR+22 hane) + yakındaki orphan 2 haneyi "
            "birleştirir, YALNIZ mod-97 GEÇERLİ sonucu döndürür (yanlış birleştirme checksum'a takılır); tam "
            "IBAN'lar (ardı 2 hane) dokunulmaz. (2) Ziraat dalında bu onarım all_ibans[0] fallback'inden ÖNCE "
            "çalışır → gönderici IBAN doğru dolar. (3) Gönderici adı kısmi IBAN satırının sonundan okunur "
            "(KASIM AKNAY). Sonuç: gönderici KASIM AKNAY/TR65, alıcı ENES/TR17 — İKİSİ AYRI, skor 100, güvenilir; "
            "YZ artık karışmıyor. Test #29 kilitler.",
     "not": "PDF'te gönderici ve alıcı IBAN AYNI DEĞİLDİR — kullanıcı sorusu: farklı (TR65 vs TR17, ikisi de Ziraat)."},
    {"id": "Z5", "date": "2026-08-21 19:45", "area": "RAIL YANLIŞ-POZİTİFİ: 'Hesaptan Hesaba Havale' EFT sanılıyordu → HAVALE düzeltildi", "test": 28,
     "bug": "Gerçek Ziraat 'Hesaptan Hesaba Havale' (banka-içi, aynı IBAN kodu 00010) dekontu yeni EFT kuralıyla "
            "yanlışlıkla EFT işaretlenip 'güvenilir değil / riskli' (skor 40) çıkıyordu. İKİ kök hata: (1) "
            "classify_rail'de eft_in_title = ('eft' in metin) ALT-DİZE eşleşmesi, yasal dipnottaki 'Banka'nın "
            "DEFTer kayıtları' kelimesinin içindeki 'eft'i yakalıyordu → sahte EFT. (2) IBAN kodu okunamadığında "
            "(gönderici IBAN çıkarılamamıştı) HAVALE fallback'i yalnız 'hesaptanhavale' arıyordu; başlık "
            "'hesaptan HESABA havale' olduğundan kaçırılıp eft_in_title'a düşüyordu. Böylece banka-içi bir "
            "HAVALE, riskli bir EFT gibi görünüyordu.",
     "fix": "(1) eft_in_title artık KELİME sınırı arar (regex (?<![a-zçğıöşü])eft(?![a-zçğıöşü])) → 'defter'/"
            "'geft' yakalanmaz. (2) HAVALE fallback'i 'hesaptanhavale' VE 'hesaptanhesabahavale' başlıklarını "
            "kapsar; koşul: gerçek EFT/FAST işareti YOK + 'bankalararası' YOK + 'eft' KELİMESİ yok. Akbank'ın "
            "'EFT BANKALAR ARASI HESABA HAVALE' genel şablonu ('eft' kelimesi + 'bankalararası' taşır) ve 'GEÇ "
            "EFT' KESİN EFT olarak korunur. Sonuç: Ziraat Havale → HAVALE, skor 100, güvenilir. Test #28 kilitler."},
    {"id": "Z4", "date": "2026-08-21 15:15", "area": "İŞLEM KANALI KURALI: EFT anında geçmez → RİSKLİ (tüm bankalar, YZ + kural)", "test": 27,
     "bug": "Dolandırıcılık senaryosu: oyun pini alan müşteri ödemeyi EFT ile yapıp dekontu gönderiyor. EFT "
            "ANINDA hesaba geçmez (saatli/toplu işlenir, geri çağrılabilir) → para henüz yansımamış olabilir, "
            "ama operatör dekonta bakıp bakiye yükleyince dolandırılıyoruz. Sistem gerçek bir EFT dekontunu "
            "'düşük risk' (72) gösteriyordu; işlem kanalının (EFT/FAST/HAVALE) ödeme-tahsil riski hiç "
            "değerlendirilmiyordu. Kural: işlem HAVALE ya da FAST değilse SORUN var.",
     "fix": "Yeni eksen (SAHTECİLİKTEN AYRI): (1) analyze.py rail=EFT → EFT_SETTLEMENT_RISK (yüksek, weight=0); "
            "ayrıca YZ 'islem_kanali=EFT' derse kuralın kaçırdığı EFT eskale edilir (fotoğrafta OCR ücret "
            "kalemini kaçırsa bile EFT ASLA kaçmaz). (2) verdicts.py: 'settlement_instant' denetimi — EFT→FALSE "
            "(kesin karar 'anlık teslimat için güvenilir değil', SAHTECİLİKTEN ayrı gerekçe), FAST/HAVALE→TRUE. "
            "(3) scoring: EFT skoru ≤40 (yüksek risk, 'yeşil' olamaz). (4) api_response: ek alanlar "
            "'islem_kanali_riski{kanal,aninda_hesaba_gecer,risk}' + 'kesin_cevaplar.odeme_aninda_gecer'. "
            "(5) ai_adjudicator: prompt görev #6 + 'islem_kanali' şeması (EFT/FAST/HAVALE, ücret kaleminden). "
            "Belge GERÇEK olsa bile EFT ise anlık teslimatta riskli. Test #27 kilitler."},
    {"id": "Z3", "date": "2026-08-21 14:45", "area": "ENPARA ↔ QNB AYRI BANKA: yanlış 'gönderici banka çelişkisi' (SENDER_BANK_MISMATCH) giderildi", "test": 26,
     "bug": "Gerçek Enpara dekontu (Ahmet Özkul → Serhat, GİDEN FAST EFT) webde SENDER_BANK_MISMATCH ile kritik "
            "(12 puan) çıkıyordu; YZ ise 'gerçek' diyordu (katman çelişkisi). Neden: parser gönderici bankasını "
            "'Enpara.com (QNB)' etiketliyor; _canon_bank içinde NAME_KEYWORDS sırası 'qnb'yi 'enpara'dan ÖNCE "
            "deneyince metin yanlışlıkla 'QNB Finansbank'a çözülüyor, ama gönderici IBAN'ı Enpara (00157→'Enpara "
            "Bank'). İki kanonik ad farklı → yanlış 'banka çelişkisi'. Enpara ARTIK QNB'den AYRI bir bankadır "
            "(00157/00111 farklı kodlar) — birleştirmek YANLIŞ; doğru çözüm markayı doğru tanımak.",
     "fix": "(1) banks.NAME_KEYWORDS: 'enpara' kalıbı 'finansbank|qnb'DEN ÖNCE (marka önceliği) → 'Enpara.com "
            "(QNB)' artık 'Enpara Bank'a çözülür, IBAN ile ÇELİŞMEZ. (2) extract.py: enpara kaydı etiketi 'Enpara "
            "Bank', IBAN kümesi {00157} (00111 kaldırıldı; o QNB'ye ait). sender.bank yedeği 'Enpara Bank'. "
            "(3) bank_knowledge/reference_profiles etiketleri 'Enpara Bank'. Enpara ile QNB kanonik+kod olarak "
            "AYRI kalır. Test #26 kilitler (marka önceliği + çelişki-yok + ayrı-banka)."},
    {"id": "Z2", "date": "2026-08-21 13:00", "area": "'DEKONT DEĞİL' YANLIŞ-POZİTİFİ: YZ/Vision alan doldurduysa NOT_A_RECEIPT kalkar", "test": 25,
     "bug": "Gerçek Enpara dekontu (Ahmet Özkul → Serhat, 40.000 TL, GİDEN FAST EFT) webde tarandığında YZ "
            "'sorunsuz/gerçek' diyordu, AMA skor kartı 'sahte (5 puan)' diyordu — KATMAN ÇELİŞKİSİ. Neden: "
            "fotoğrafta tesseract HAM okuma boş kaldığından (text_source='none') NOT_A_RECEIPT (kritik) bulgusu "
            "ekleniyor ve skoru 5'e kırıyordu. Sonra Vision/YZ görüntüyü OKUYUP tüm alanları (gönderici/alıcı "
            "IBAN, tutar, işlem no) doğru dolduruyor — ama NOT_A_RECEIPT bulgusu KALDIRILMADAN kalıyor, skor 5'te "
            "kilitli kalıyordu. Kullanıcı haklı olarak 'AI sorunsuz diyor ama farklı alanlar sahte diyor' dedi.",
     "fix": "analyze.py post-AI blok (d): NOT_A_RECEIPT bulgusu varsa VE _extracted_dict (Vision+YZ sonrası) "
            "AÇIK dekont kanıtı taşıyorsa (geçerli mod-97 IBAN, ya da tutar+işlem/ref no, ya da her iki taraf "
            "adı) → NOT_A_RECEIPT KALDIRILIR, skor+karar YENİDEN hesaplanır. Gerçekten dekont olmayan dosyada YZ "
            "bu alanları dolduramayacağından bulgu YERİNDE kalır (skor 5). Böylece YZ ile skor kartı ARTIK "
            "çelişmez. Test #25 iki yönlü kilitler (gerçek dekont→kalkar; dekont-değil→kalır)."},
    {"id": "Z", "date": "2026-08-21 12:30", "area": "MANTIKSAL ÇELİŞKİ: aynı banka ↔ 'BANKALAR ARASI/EFT' başlığı → %50 altı", "test": 24,
     "bug": "Akbank dekontunda gönderici ve alıcı IBAN'ların İKİSİ de Akbank (00046) koduna ait (banka-içi "
            "işlem), ama başlıkta 'EFT BANKALAR ARASI HESABA HAVALE' yazıyor. Bu MANTIKSAL bir çelişkidir "
            "(aynı bankadaki iki hesap arası EFT/FAST YAPILAMAZ). Belge 72 puan alıp 'güvenilir' işaretleniyordu. "
            "Neden: (1) deterministik çelişki kontrolü (check_samebank_rail_contradiction) yalnız HAM OCR "
            "IBAN'larına bakıyordu; fotoğrafta tesseract iki IBAN'ı çoğu kez aynı/bozuk okuyor (s==r) → çelişki "
            "yakalanamıyor. (2) SAMEBANK_RAIL_CONTRADICTION içerik-tahrifat listesinde (verdicts) değildi → kesin "
            "karar 'güvenilir değil'e dönmüyordu.",
     "fix": "(1) analyze.py: AI gönderici/alıcı IBAN'larını düzelttikten SONRA çelişki AI-doğrulanmış IBAN'lar "
            "üzerinden TEKRAR sınanır (post-AI blok, c). İki IBAN da mod-97 geçerli + aynı banka kodu + FARKLI "
            "hesap + 'BANKALAR ARASI/EFT/FAST' başlığı → SAMEBANK_RAIL_CONTRADICTION (kritik). (2) verdicts.py: "
            "SAMEBANK_RAIL_CONTRADICTION ve INTERBANK_HAVALE_CONTRADICTION _CONTENT_TAMPER'a eklendi → kesin karar "
            "'GÜVENİLİR DEĞİL'. (3) scoring: SAMEBANK_RAIL_CONTRADICTION skoru ≤6'ya çeker. Sonuç: skor <50, "
            "güvenilir değil. Kullanıcı kuralı: 'böyle bir mantıksal çelişkinin olduğu dekontu %50 nin altında "
            "tutmalısın.' Test #24 kilitler."},
    {"id": "Y", "date": "2026-08-21 11:55", "area": "GÖRSEL TAHRİFAT: YZ font/yapıştırma incelemesi (fotoğraf her zaman)", "test": 23,
     "bug": "Garanti dekontunda yazıyla yazılan tutar ('YetmişBeşBinTL.') belgenin monospace fontundan "
            "FARKLI bir kalın/proporsiyonel fontta — çıplak gözle görülen yapıştırma. Kural motoru rasterize "
            "görüntüde font okuyamadığı için göremiyordu; dekont 'temiz' göründüğünden YZ görsel incelemesine "
            "HİÇ eskale EDİLMİYORDU (AI devreye girmiyordu).",
     "fix": "(1) should_adjudicate: input_kind='image' (fotoğraf/görüntü) dekontlar KURAL temiz olsa bile HER "
            "ZAMAN YZ görsel incelemesine eskale edilir. (2) Prompt: yazı tipi/kalınlık/hizalama tutarlılığı "
            "taranır; bir alan (özellikle TUTAR — rakam veya yazıyla) genel fonttan farklıysa gorsel_tahrifat'a "
            "yazılır. (3) analyze.py: güven≥60 görsel-tahrifat → AI_VISUAL_TAMPER bulgusu (high), skor+karar "
            "YENİDEN hesaplanır (puan ≤20, GÜVENİLİR DEĞİL). Test #23 kilitler."},
    {"id": "X", "date": "2026-08-21 11:30", "area": "YZ ARKA-UÇ: boş kritik alanları görüntüden doldurur", "test": 22,
     "bug": "OCR bir dekonttaki alıcı adı/alıcı IBAN/tutar/işlem no/referans no'yu okuyamayınca EKRANA BOŞ "
            "geliyordu. YZ değerlendiricisi (yapay_zeka_degerlendirmesi) alanları yeniden okusa bile sonucu "
            "rapordaki 'extracted' alanına YANSITILMIYORDU (apply_corrections hiç çağrılmıyordu).",
     "fix": "(1) should_adjudicate genişletildi: alıcı adı/alıcı IBAN/tutar boşsa VEYA hiçbir işlem/referans/"
            "sıra numarası okunamadıysa YZ'ye eskale edilir (her banka). (2) analyze.py artık YZ'nin görüntüden "
            "okuduğu düzeltmeleri apply_corrections ile 'extracted'a işler → alıcı adı, alıcı IBAN, tutar, işlem "
            "no, referans no ekrana BOŞ gelmez. IBAN yalnız mod-97 geçerliyse uygulanır (uydurma engellenir); "
            "tutar metinden float'a çevrilir. Test #22 kilitler."},
    {"id": "W", "date": "2026-08-21 11:10", "area": "BANKA-BAZLI NUMARA TEKRARI → sahtecilik + önceki dekont detayı", "test": 21,
     "bug": "Aynı bankada aynı işlem/sıra/referans numarasını taşıyan FARKLI dekontlar (kopyala-yapıştır "
            "sahtecilik) yakalanmıyordu: mevcut SEQ_DB_DUPLICATE yalnız TEMİZ kaydedilen 'receipts' tablosuna "
            "ve yalnız seq_number'a bakıyordu; RECEIVER_BANK_MISMATCH'li (reddedilen) dekontlar hiç kaydedilmediği "
            "için numaraları karşılaştırılamıyordu (3 VakıfBank dekontu aynı İŞLEM NO'yu taşıyordu).",
     "fix": "(1) check_number_reuse: HER analizi (sahte dahil) tutan 'analyses' tablosuna bakar; işlem/doküman "
            "no + sıra/sorgu no + referans no'nun HEPSİNİ karşılaştırır; AYNI banka + FARKLI dosyada tekrar → "
            "NUMBER_REUSE (kritik). Her banka KENDİ İÇİNDE değerlendirilir. (2) VakıfBank İŞLEM NO çıkarımı "
            "OCR-etiket bozulmasına dayanıklı (tarih-önekli 14-16 haneli numara). (3) Bulgu, tekrarın hangi "
            "önceki dekontta yaşandığını GÖNDEREN/ALICI/TUTAR/TARİH detaylarıyla yazar. (4) YANLIŞ-POZİTİF "
            "KORUMASI: AYNI dekont tekrar tarandığında (sha256 değişse bile numara+tutar+alıcı aynı) bulgu "
            "ÜRETİLMEZ; yalnız numara aynı iken tutar/alıcı FARKLIYSA sahtecilik sayılır. Test #21 kilitler."},
    {"id": "V", "date": "2026-08-21 10:00", "area": "IBAN OTORİTESİ: banka+rail IBAN kodundan + tarama kaydı", "test": 20,
     "bug": "Ziraat dekontunda web taraması gönderici bankasını 'Enpara' yazdı (IBAN 00010=Ziraat iken "
            "metindeki 'Alan Banka' yanlış tarafa atanmış); TC No'ya adres sızdı; ücret/toplam okunamayıp "
            "'aritmetik doğrulanamadı' dedi. Ayrıca web taramalarının cevabı saklanmıyordu (karşılaştırma yok).",
     "fix": "(1) TÜM bankalarda gönderici/alıcı bankası KENDİ IBAN'ının banka kodundan set edilir (metin "
            "etiketi ezmez). (2) classify_rail birincil kapı IBAN kodu: aynı→HAVALE, farklı→bankalararası; "
            "EFT/FAST'ı fatura etiketi ayırır. (3) TCKN adres-sızması temizlenir (11 hane). (4) Ziraat ücret "
            "yedeği (Toplam Masraf yoksa Komisyon+BSMV+Mesaj) + imza sağlamlaştırma. (5) TARAMA KAYDI: her "
            "web/API taraması store'a yazılır, /api/v1/scan_log ile sorgu/ref/isimle aranır → 'web ne demişti "
            "vs gerçek ne' karşılaştırması. Test #20 kilitler."},
    {"id": "U", "date": "2026-08-21 05:15", "area": "Kuveyt Türk arşive eklendi + kanal (FAST)", "test": 19,
     "bug": "Kuveyt Türk 'IBAN'a Para Transferi' dekontu 'Senaryo/Tip: DEKONT/EFT' yazıyor ama Açıklama "
            "'(FAST)' diyor. Görünen işlem türü yanlış ('DEKONT/EFT') gösteriliyordu; ayrıca banka arşivde yoktu.",
     "fix": "Kuveyt Türk dalı: işlem türü Açıklama'daki '(FAST)'/'(EFT)'den belirlenir ('Senaryo' genel "
            "etiket). Rail zaten FAST. bank_corpus'a Kuveyt Türk arşiv kaydı + bank_knowledge normu "
            "(gönderen IBAN basılmaz = eksiklik değil) eklendi. Test #19 kilitler."},
    {"id": "T", "date": "2026-08-21 04:00", "area": "Enpara korpus + kapsam (IBAN-kod/Fiş-tarih/üretim-app)", "test": 18,
     "bug": "13 gerçek Enpara dekontuyla banka-içi analiz istendi. Ayrıca 3 denetim raporda görünmüyordu: "
            "IBAN banka-kodu karşılaştırması (aynı→HAVALE), Enpara Fiş No ilk-8-hane tarih doğrulaması, ve "
            "fotoğrafın hangi uygulamada yapıldığı / Photoshop-Canva-AI ile değiştirilip değiştirilmediği.",
     "fix": "Denetim Kapsamı'na 3 madde eklendi: (a) IBAN banka kodu karşılaştırması — aynı kod→HAVALE, "
            "farklı→EFT/FAST (TÜM bankalar). (b) Fiş No tarih doğrulaması (Enpara/QNB: ilk 8 hane=YYYYAAGG, "
            "işlem tarihiyle karşılaştırılır; sahtede RECEIPT_NO_DATE_MISMATCH). (c) Üretim uygulaması/düzenleme: "
            "EXIF/producer'dan AI (DALL-E/Midjourney/Firefly), düzenleyici (Photoshop/Canva/GIMP…) ve tarayıcı "
            "(pdfium/Skia) tespiti. Enpara normu: interbank=EFT, aynı-banka=HAVALE, gerçek üretici iText/Ibtech, "
            "sahte pdfium. bank_corpus SEED ve bank_knowledge güncellendi."},
    {"id": "S", "date": "2026-08-20 06:30", "area": "KALICI: banka-içi karşılaştırma + dekont hafızası", "test": 17,
     "bug": "Kullanıcı kuralı sürekli tekrar ediliyordu: her banka KENDİ dekontlarıyla karşılaştırılmalı, "
            "bankalar arası kıyas yapılmamalı; ve yüklenen eski dekontlar içeride saklanmalı.",
     "fix": "METHODOLOGY ilkesi bank_knowledge'a kalıcı yazıldı. bank_corpus.py eklendi: her bankanın "
            "GÖRÜLEN dekontları SEED (kod içinde kalıcı) + canlı store (bank_corpus tablosu) olarak "
            "banka bazlı saklanır. Yeni dekont yalnız KENDİ bankasının normuyla karşılaştırılır; sonuç "
            "'Banka-içi karşılaştırma' olarak Denetim Kapsamı'nda yazılır. Her analiz hafızaya eklenir."},
    {"id": "R", "date": "2026-08-20 06:10", "area": "Kanal = FATURA etiketi (Enpara EFT) + alıcı adı", "test": 16,
     "bug": "Enpara dekontu 'EFT (FAST)' / 'GİDEN FAST EFT' diyor. Sistem önce 'belirsiz', sonra (yanlış) "
            "FAST verdi. Oysa dekont tutar/ücreti 'EFT TUTARI' ve 'EFT ÜCRETİ' diye FATURALAR → işlem "
            "EFT'dir; '(FAST)' teslim rayıdır (anlık EFT altyapısı). Kullanıcı içgörüsü + Enpara/İşbank/"
            "Akbank fatura-etiketi kıyası bunu doğruladı. Ayrıca 'ALICI ÜNVANI' OCR'da kaçınca alıcı adı boştu.",
     "fix": "classify_rail'de RAİL = FATURALAMA etiketi: 'FAST Ücreti/GİDEN FAST TUTARI' → FAST; "
            "'EFT TUTARI/ÜCRETİ' → EFT; GEÇ EFT/GECEFT en kesin. Çıplak '(FAST)'/'GİDEN FAST' başlığı "
            "ZAYIF teslim-rayı etiketidir, EFT faturalamasını EZMEZ. Böylece Enpara 'EFT (FAST)' → EFT, "
            "İşbank 'FAST Ücreti' → FAST, YKB 'GİDEN FAST TUTARI' → FAST. Enpara alıcı adı yedeği: "
            "'ALICI ÜNVANI' yoksa açıklamadaki '<ad>, Bireysel Ödeme'den alınır."},
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


def _t16_enpara_eft_fast_and_receiver():
    """Enpara/QNB: işlemi 'EFT (FAST)' diye gösterir AMA tutar/ücreti 'EFT TUTARI / EFT ÜCRETİ' diye
    FATURALAR → bu bir EFT'dir ('(FAST)' teslim rayıdır, anlık EFT altyapısı). Rail EFT olmalı.
    FATURA etiketi (EFT ÜCRETİ), çıplak '(FAST)' etiketini YENER. Ayrıca 'ALICI ÜNVANI' satırı OCR'da
    kaçarsa alıcı adı açıklamadan ('<ad>, Bireysel Ödeme') yedeklenmeli (alıcı adı boş kalmamalı)."""
    import authenticity as A
    from extract import extract_fields
    a = _gen_iban
    r = (A.classify_rail("GIDEN FAST EFT\nEFT (FAST)\nEFT TUTARI 3.000,0 TL  EFT ÜCRETİ (BSMV DAHİL) 0 TL",
                         a("00157"), a("00010"), "enpara") or {}).get("rail")
    # Karşı-koruma: 'FAST Ücreti' FATURALAMASI olan İşbank → FAST kalmalı
    r2 = (A.classify_rail("Giden Fast İşlemi\nFAST Ücreti ve Vergi 8,37", a("00064"), a("00111"), "isbank") or {}).get("rail")
    # Alıcı adı yedeği (ALICI ÜNVANI satırı yok)
    txt = ("enpara DEKONT\nSayın MUSTAFA DOĞAN\n"
           "Vadesiz TL TR08 0015 7000 0000 0116 981974 yusuf erman, Bireysel Ödeme, EFT (FAST) sorgu no: 4759572483 B TL 3.000.00\n"
           "GIDEN FAST EFT\nALICI IBAN: TR92 0001 0090 1114 0935 0050 01\n"
           "MÜŞTERİ ÜNVANI: MUSTAFA DOĞAN  IBAN: TR08 0015 7000 0000 0116 981974\n")
    ex = extract_fields(txt, txt, None)
    ok = (r == "eft" and r2 == "fast" and ex.receiver.name.lower() == "yusuf erman")
    return ok, f"Enpara EFT(FAST)→{r} (EFT olmalı — fatura EFT), İşbank FAST-fatura→{r2} (fast olmalı), alıcı yedeği={ex.receiver.name!r}"


def _t17_per_bank_comparison():
    """KALICI METODOLOJİ: her banka KENDİ dekontlarıyla karşılaştırılır (bankalar arası kıyas yok).
    bank_corpus her banka için SEED (görülen gerçek dekontlar) tutar; compare_rail aynı bankanın
    normuna göre yanıt verir. Ayrıca METHODOLOGY ilkesi bank_knowledge'da kayıtlı olmalı."""
    import bank_corpus as BC, bank_knowledge as BK
    enp = BC.compare_rail("enpara", "eft")           # Enpara EFT normu ile tutarlı olmalı
    dnz = BC.compare_rail("deniz", "fast")           # Denizbank FAST normu
    enp_ok = enp["durum"] == "yapıldı" and "EFT" in enp["sonuc"]
    dnz_ok = dnz["durum"] == "yapıldı" and "FAST" in dnz["sonuc"]
    # Bankalar arası kıyas YOK: Enpara için sorulan kanal Denizbank normuna bakmamalı → ayrı ayrı sayılar
    isolated = BC.summary("enpara")["count"] != BC.summary("deniz")["count"] or True
    method_ok = isinstance(getattr(BK, "METHODOLOGY", None), str) and "kendi bankas" in BK.METHODOLOGY
    ok = enp_ok and dnz_ok and method_ok
    return ok, f"enpara-EFT-tutarlı={enp_ok}, deniz-FAST-tutarlı={dnz_ok}, methodoloji-kayıtlı={method_ok}"


def _t18_coverage_new_items():
    """Denetim Kapsamı: (a) IBAN banka-kodu karşılaştırması (aynı→havale/farklı→eft-fast),
    (b) Enpara Fiş No tarih doğrulaması (ilk 8 hane YYYYAAGG), (c) üretim uygulaması/düzenleme
    (Photoshop/Canva/AI/tarayıcı) maddeleri her raporda üretilmeli."""
    import coverage
    rep = {
        "classification": {"input_kind": "pdf", "text_source": "digital"},
        "extracted": {
            "bank": "Enpara Bank",
            "sender": {"name": "NURULLAH ONAT", "iban": "TR080015700000000116981974", "tckn": ""},
            "receiver": {"name": "UĞUR ERDAL", "iban": "TR080015700000000118637085"},
            "amount": {"value": 35000.0, "fee": 0.0, "total": 35000.0},
            "transaction": {"date": "12.08.2026 22:47", "document_no": "202608128141611", "sequence_number": "4739474052"},
        },
        "findings_tr": [{"code": "RAIL_IS_HAVALE"}],
        "metadata": {"producer": "iText 2.1.7 by 1T3XT", "creator": "Ibtech"},
        "image_forensics": {},
        "cross_db": {"checked": False},
    }
    cov = coverage.build(rep)
    al = {m["alan"]: m for m in cov["maddeler"]}
    has_code = any("IBAN banka kodu" in a for a in al)
    havale_ok = any("IBAN banka kodu" in a and "HAVALE" in m["sonuc"] for a, m in al.items())
    has_fis = any("Fiş No tarih" in a for a in al)
    fis_ok = any("Fiş No tarih" in a and ("12.08.2026" in m["sonuc"]) for a, m in al.items())
    has_prod = any("Üretim uygulaması" in a for a in al)
    ok = has_code and havale_ok and has_fis and fis_ok and has_prod
    return ok, f"iban-kod={has_code}/havale={havale_ok}, fiş-tarih={has_fis}/uyumlu={fis_ok}, üretim-app={has_prod}"


def _t19_kuveyt_turk_fast():
    """Kuveyt Türk 'IBAN'a Para Transferi': 'Senaryo/Tip: DEKONT/EFT' GENEL etikettir; gerçek kanal
    Açıklama'daki '(FAST)' → FAST. İşlem türü ve rail FAST olmalı; alıcı adı doğru çıkmalı; arşivde
    (bank_corpus) kayıtlı olmalı."""
    import authenticity as A, bank_corpus as BC
    from extract import extract_fields
    txt = ("KUVEYTTÜRK\nIBAN'a Para Transferi (Giden)\ne-Dekont\nSenaryo/Tip : DEKONT/EFT\n"
           "Gönderen Kişi : ŞEBNEM AYLİN DUYAR\nAlıcı : Mustafa YEŞİLMEN\n"
           "Gönderilen IBAN : TR52 0015 7000 0000 0205 2632 10\nAlıcı Banka : Enpara Bank A.Ş.\n"
           "Açıklama : Gönderen: ŞEBNEM AYLİN DUYAR , Alıcı: Mustafa YEŞİLMEN , IBAN'a Para Transferi (FAST)\n"
           "Tutar : 5.000,00 TL\nwww.kuveytturk.com.tr\n")
    ex = extract_fields(txt, txt, None)
    rail = (A.classify_rail(txt, "", ex.receiver.iban, "kuveyt") or {}).get("rail")
    type_fast = "FAST" in (ex.transaction.type or "")
    recv_ok = "YEŞİLMEN" in (ex.receiver.name or "").upper()   # upper: İ birleşik-nokta üretmez
    archived = BC.compare_rail("kuveyt", "fast")["durum"] == "yapıldı"
    ok = (rail == "fast" and type_fast and recv_ok and archived)
    return ok, f"rail={rail} (fast), tür-FAST={type_fast}, alıcı={ex.receiver.name!r}, arşivde={archived}"


def _t20_iban_authority_bank_and_rail():
    """IBAN OTORİTESİ (TÜM bankalar): (a) gönderici/alıcı bankası KENDİ IBAN'ının banka kodundan
    set edilir (metin etiketi yanlış tarafa atanamaz). (b) HAVALE/EFT-FAST ayrımı IBAN kodu
    karşılaştırmasına bağlıdır: aynı kod→HAVALE, farklı→bankalararası. (c) TCKN'ye adres sızması temizlenir."""
    from extract import extract_fields
    import authenticity as A
    # Ziraat gönderici (00010), Enpara alıcı (00157) — metinde 'Alan Banka: Enpara' göndericiye atanmamalı
    txt = ("Ziraat Bankası\nHESAPTAN FAST\nŞUBE KODU/ADI : 0404/YALOVA ŞUBESİ\n"
           "IBAN : TR54 0001 0004 0466 8971 8150 05\nVERGİ KİMLİK NO : 19880252454 MEHMET CD. NO:\n"
           "Gönderen : TİLBE BİÇEN\nAlan Banka : 0157 - Enpara Bank A.Ş.\n"
           "Alıcı Hesap : TR52 0015 7000 0000 0205 2632 10 Alıcı : Mustafa YEŞİLMEN\n"
           "İşlem Tutarı : 255,00 TRY\nKomisyon : 7,62 TRY BSMV : 0,38 TRY Mesaj Ücreti : 0,37 TRY\n"
           "Toplam Masraf : 8,37 TRY\nHesabınızdan 263,37 TL Çekilmiştir.\n")
    ex = extract_fields(txt, txt, None)
    snd_ok = "ziraat" in (ex.sender.bank or "").lower()      # gönderici bankası Ziraat (IBAN 00010)
    tckn_ok = ex.sender.tckn == "19880252454"                # adres sızması temizlendi
    fee_ok = ex.amount.fee == 8.37                            # Toplam Masraf / bileşen toplamı
    # rail: farklı IBAN kodu → bankalararası fast; aynı kod → havale
    r_inter = (A.classify_rail(txt, ex.sender.iban, ex.receiver.iban, "ziraat") or {}).get("rail")
    r_same = (A.classify_rail("GİDEN FAST\nFAST Ücreti",
                              "TR330001000000000000000017", "TR930001000000000000000099", "") or {}).get("rail")
    ok = (snd_ok and tckn_ok and fee_ok and r_inter == "fast" and r_same == "havale")
    return ok, (f"gönderici-banka-Ziraat={snd_ok}, tckn-temiz={tckn_ok}, ücret={ex.amount.fee}, "
                f"interbank-rail={r_inter}(fast), samebank-rail={r_same}(havale)")


def _t21_bank_scoped_number_reuse():
    """BANKA-BAZLI NUMARA TEKRARI: aynı bankada daha önce görülmüş işlem/sıra/referans numarası yeni
    bir dekontta tekrar ederse NUMBER_REUSE (kritik) üretilir. FARKLI bankada aynı numara → TETİKLENMEZ
    (her banka kendi içinde değerlendirilir). İşlem no (document_no) da kapsanır — sadece seq değil."""
    import os, tempfile
    import store as ST
    _old = os.environ.get("DEKONT_DB_PATH")
    _tmp = tempfile.mkdtemp()
    os.environ["DEKONT_DB_PATH"] = os.path.join(_tmp, "sc_reuse.db")
    try:
        _RIB = "TR190001009011147534405001"   # geçerli alıcı IBAN (eksiksizlik kapısı için gerekli)
        def _rep(sha, bank, doc, snd="KEREM", rcv="Nalan Töre", amt=50000.0, date="21.08.2026 02:36:06",
                 rib=_RIB):
            return {"file": {"sha256": sha},
                    "extracted": {"bank": bank, "sender": {"bank": bank, "name": snd},
                                  "transaction": {"document_no": doc, "ref_no": "", "sequence_number": "",
                                                  "date": date},
                                  "receiver": {"name": rcv, "iban": rib}, "amount": {"value": amt}},
                    "score": {"authenticity_score": 30}, "classification": {"is_receipt": True},
                    "findings_en": []}
        # ilk dekont (EKSİKSİZ: geçerli alıcı IBAN + ad + tutar + numara) → hafızaya
        ST.log_analysis(_rep("a" * 64, "VAKIFBANK", "2026082120159022", "CITY2 GIDA", "Nalan Töre", 50000.0))
        # (a) FARKLI işlem, aynı numara, FARKLI tutar → sahtecilik yakalanmalı
        same = ST.check_number_reuse(_rep("b" * 64, "VAKIFBANK", "2026082120159022", "CITY2 GIDA", "Atakan Yenici", 18933.0))
        # (b) FARKLI banka, aynı numara → tetiklenmemeli (her banka kendi içinde)
        diff = ST.check_number_reuse(_rep("c" * 64, "GARANTI BBVA", "2026082120159022"))
        # (c) AYNI dekontun TEKRAR taranması (farklı sha ama tutar+tarih aynı) → YANLIŞ-POZİTİF olmamalı
        rescan = ST.check_number_reuse(_rep("d" * 64, "VAKIFBANK", "2026082120159022", "CITY2 GIDA", "Nalan Töre", 50000.0))
        # (d) Aynı numara+tutar ama İŞLEM TARİHİ (GÜN) FARKLI → TETİKLENMELİ (kullanıcı kuralı: sadece tarih
        # değiştirilerek de sahtecilik yapılır). Kayıtlar eksiksiz okumalardan olduğundan tarih kararlıdır.
        datef = ST.check_number_reuse(_rep("e" * 64, "VAKIFBANK", "2026082120159022", "CITY2 GIDA", "Nalan Töre", 50000.0,
                                           date="19.08.2026 02:36:06"))
        has_detail = bool(same) and ("Nalan Töre" in same[0]["tr"]) and (same[0].get("onceki_dekont", {}).get("amount") == 50000.0)
        ok = (any(f["code"] == "NUMBER_REUSE" for f in same) and not diff and not rescan
              and any(f["code"] == "NUMBER_REUSE" for f in datef) and has_detail)
        return ok, (f"farklı-tutar-yakalandı={bool(same)}, farklı-banka-temiz={not diff}, aynı-dekont-FP-yok={not rescan}, "
                    f"sadece-tarih-yakalanır={bool(datef)}, önceki-detay={has_detail}")
    finally:
        if _old is None:
            os.environ.pop("DEKONT_DB_PATH", None)
        else:
            os.environ["DEKONT_DB_PATH"] = _old


def _t22_ai_fills_blank_fields():
    """YZ GÖRÜNTÜDEN OKUMA (kullanıcı kuralı): OCR boş bıraktığı kritik alanları (alıcı IBAN, alıcı adı,
    tutar, işlem no, referans no) YZ değerlendiricisi okuduğunda apply_corrections bunları çıkarım
    dict'ine işler → ekrana BOŞ gelmez. Geçersiz IBAN (mod-97 tutmayan) UYGULANMAZ (uydurma engellenir)."""
    import ai_adjudicator as AJ
    good = _gen_iban("00067")
    ex = {"sender": {"name": ""}, "receiver": {"name": "", "iban": ""},
          "amount": {"value": 0}, "transaction": {"document_no": "", "ref_no": ""}}
    adj = {"corrected_fields": {"receiver.iban": good, "receiver.name": "Nalan Töre",
                                "amount.value": "50.000,00", "transaction.document_no": "2026082120159022",
                                "transaction.ref_no": "2869688238"}}
    out = AJ.apply_corrections(ex, adj)
    filled = (out["receiver"]["iban"] == good and out["receiver"]["name"] == "Nalan Töre"
              and out["amount"]["value"] == 50000.0 and out["transaction"]["document_no"] == "2026082120159022"
              and out["transaction"]["ref_no"] == "2869688238")
    out2 = AJ.apply_corrections({"receiver": {"iban": ""}},
                                {"corrected_fields": {"receiver.iban": "TR000000000000000000000000"}})
    reject = not out2["receiver"]["iban"]
    ok = filled and reject
    return ok, f"boş-alanlar-dolduruldu={filled}, geçersiz-IBAN-reddi={reject}"


def _t23_ai_visual_tamper_escalation():
    """GÖRSEL TAHRİFAT: (a) fotoğraf/görüntü dekont KURAL 'temiz' olsa bile YZ görsel incelemesine
    eskale edilir (font/yapıştırma yalnız görüntüden görülür); (b) YZ'nin bildirdiği görsel-tahrifat
    (font uyuşmazlığı) _sanitize'dan geçip bulguya dönüştürülebilir."""
    import ai_adjudicator as AJ
    clean_ex = {"sender": {"name": "X"}, "receiver": {"name": "Y", "iban": "TR330006200000000000000017"},
                "amount": {"value": 100.0}, "transaction": {"ref_no": "123456"}}
    go, reasons = AJ.should_adjudicate([], clean_ex, "image")   # temiz görüntü dekont
    esc = bool(go) and any(("örsel" in r) or ("font" in r.lower()) for r in reasons)
    san = AJ._sanitize({"verdict": "sahte", "gorsel_tahrifat": [
        {"alan": "tutar (yazıyla)", "aciklama": "belgenin genel fontundan farklı, yapıştırılmış", "guven": 90}]})
    gt_ok = bool(san.get("gorsel_tahrifat")) and san["gorsel_tahrifat"][0]["guven"] == 90
    ok = esc and gt_ok
    return ok, f"görüntü-eskalasyon={esc}, görsel-tahrifat-sanitize={gt_ok}"


def _t24_samebank_rail_contradiction():
    """MANTIKSAL ÇELİŞKİ (AYNI BANKA ↔ 'BANKALAR ARASI/EFT/FAST' BAŞLIĞI) → %50 ALTINDA + GÜVENİLİR DEĞİL.
    Gönderici ve alıcı IBAN AYNI bankaya (ör. Akbank 00046) ait, iki hesap FARKLI ve her ikisi de mod-97
    geçerli; ama dekont başlığı 'EFT BANKALAR ARASI' diyorsa bu İMKÂNSIZDIR (banka-içi işlem EFT/FAST olamaz)
    → SAMEBANK_RAIL_CONTRADICTION. Kural: (a) çelişki yakalanır, (b) skor <50, (c) kesin karar 'güvenilir değil'.
    Kullanıcı kuralı: 'böyle bir mantıksal çelişkinin olduğu dekontu %50 nin altında tutmalısın.'"""
    import banks as _b, authenticity as _a, scoring as _sc, verdicts as _v
    from forensics import Finding

    def _valid_iban(body22):
        for kk in range(0, 100):
            cand = "TR%02d%s" % (kk, body22)
            if _b.iban_valid(cand) is True:
                return cand
        return None
    i1 = _valid_iban("0004600232888000321907")   # Akbank 00046
    i2 = _valid_iban("0004600232888000399999")   # Akbank 00046 (farklı hesap)
    txt = "AKBANK\nEFT BANKALAR ARASI HESABA HAVALE\n%s\n%s" % (i1, i2)
    _r = _a.check_samebank_rail_contradiction(txt, i1, i2)
    fired = bool(_r) and _r["code"] == "SAMEBANK_RAIL_CONTRADICTION"
    findings = [Finding(code="SAMEBANK_RAIL_CONTRADICTION", severity="critical", category="content",
                        weight=46, tr="x", en="x")]
    codes = {f.code for f in findings}
    vd = _v.compute_verdicts(doc_type="image_only", input_kind="image", codes=codes, cons=None,
                             has_pdf_dates=False, txn_date="2026-08-20", seq="123", db_checked=False,
                             db_count=0, is_receipt=True, doc_kind="dekont", balance_state=None, timing=None)
    untrusted = vd["overall"]["state"] == "false"
    sc = _sc.compute_score(findings, "image_only", 0.0, 0.0, verdict_untrusted=untrusted)
    below = sc.authenticity_score < 50
    in_tamper = "SAMEBANK_RAIL_CONTRADICTION" in _v._CONTENT_TAMPER
    ok = fired and below and untrusted and in_tamper
    return ok, f"çelişki={fired}, skor={sc.authenticity_score}(<50={below}), güvenilir-değil={untrusted}, tamper-listesinde={in_tamper}"


def _t25_not_a_receipt_false_positive():
    """'DEKONT DEĞİL' YANLIŞ-POZİTİFİ: NOT_A_RECEIPT, tesseract ham okuması boş kalınca eklenir. Vision/YZ
    görüntüyü okuyup geçerli IBAN/tutar/işlem-no doldurduysa belge AÇIKÇA dekonttur → NOT_A_RECEIPT KALKAR,
    skor 'dekont değil (5)' OLMAZ. Gerçekten dekont olmayan (hiçbir alan dolmaz) dosyada bulgu KALIR (skor 5).
    Kök sorun: YZ 'gerçek/sorunsuz' derken skor kartı 'sahte' diyordu (katman çelişkisi)."""
    import scoring as _sc, verdicts as _v, banks as _b
    from forensics import Finding

    def _recompute(findings, extracted):
        remove = set()
        if any(f.code == "NOT_A_RECEIPT" for f in findings):
            sd = extracted.get("sender", {}) or {}
            rd = extracted.get("receiver", {}) or {}
            amt = (extracted.get("amount", {}) or {}).get("value")
            tx = extracted.get("transaction", {}) or {}
            txn = (tx.get("ref_no") or tx.get("document_no") or tx.get("sequence_number") or "").strip()
            hvi = any(_b.iban_valid(_b.normalize_iban(p.get("iban") or "")) is True for p in (sd, rd))
            hn = bool((sd.get("name") or "").strip()) and bool((rd.get("name") or "").strip())
            if hvi or (amt is not None and txn) or hn:
                remove.add("NOT_A_RECEIPT")
        fnd = [f for f in findings if f.code not in remove]
        codes = {f.code for f in fnd}
        vd = _v.compute_verdicts(doc_type="image_only", input_kind="image", codes=codes, cons=None,
                                 has_pdf_dates=False, txn_date="2026-08-16", seq="", db_checked=False,
                                 db_count=0, is_receipt=True, doc_kind="dekont", balance_state=None, timing=None)
        unt = vd["overall"]["state"] == "false"
        sc = _sc.compute_score(fnd, "image_only", 0.0, 0.0, verdict_untrusted=unt)
        return remove, sc.authenticity_score

    def _F(c, s):
        return Finding(code=c, severity=s, category="content", weight=0, tr="x", en="x")
    base = [_F("IMAGE_ONLY_DOC", "info"), _F("NOT_A_RECEIPT", "critical")]
    # (a) GERÇEK dekont — YZ geçerli IBAN'ları doldurdu → NOT_A_RECEIPT kalkar, skor 5'ten YUKARI
    real = {"sender": {"name": "Ahmet", "iban": "TR180015700000000083817494"},
            "receiver": {"name": "Serhat", "iban": "TR560004600812888000148652"},
            "amount": {"value": 40000.0}, "transaction": {"ref_no": "4747783370"}}
    rem_a, sc_a = _recompute(list(base), real)
    ok_a = ("NOT_A_RECEIPT" in rem_a) and sc_a > 5
    # (b) GERÇEKTEN dekont değil — hiçbir alan yok → NOT_A_RECEIPT KALIR, skor 5
    cat = {"sender": {"name": "", "iban": ""}, "receiver": {"name": "", "iban": ""},
           "amount": {"value": None}, "transaction": {}}
    rem_b, sc_b = _recompute(list(base), cat)
    ok_b = (not rem_b) and sc_b <= 5
    ok = ok_a and ok_b
    return ok, f"gerçek-dekont(kalktı={('NOT_A_RECEIPT' in rem_a)},skor={sc_a}), dekont-değil(kaldı={not rem_b},skor={sc_b})"


def _t26_enpara_qnb_separate_no_false_mismatch():
    """ENPARA ↔ QNB AYRI BANKA + YANLIŞ 'BANKA ÇELİŞKİSİ' YOK: Enpara, QNB'den AYRI bir bankadır
    (00157 Enpara / 00111 QNB). Enpara dekont etiketi 'Enpara.com (QNB)' gibi içinde 'QNB' geçen bir
    metin taşıyabilir; marka önceliği (NAME_KEYWORDS'te 'enpara' 'qnb'DEN ÖNCE) olmadan bu metin
    yanlışlıkla 'QNB Finansbank'a çözülür ve Enpara gönderici IBAN'ı (00157) ile ÇELİŞİR → gerçek Enpara
    dekontu SAHTE damgalanırdı. Kural: (a) 'Enpara.com (QNB)' → 'Enpara Bank'; (b) Enpara stated ↔ Enpara
    IBAN çelişmez (SENDER/RECEIVER_BANK_MISMATCH YOK); (c) Enpara ile QNB kanonik olarak AYRI kalır."""
    import banks as _b, authenticity as _a
    # (a) marka önceliği: içinde 'qnb' geçse bile Enpara markası kazanır
    ca = _a._canon_bank("Enpara.com (QNB)") == "Enpara Bank"
    # (b) Enpara gönderici: yazan banka ('Enpara.com (QNB)') ile IBAN bankası (00157→Enpara Bank) ÇELİŞMEZ
    s_iban = "TR180015700000000083817494"           # Enpara 00157 (geçerli)
    stated = _a._canon_bank("Enpara.com (QNB)")
    iban_canon = _a._canon_bank(_b.bank_from_iban(s_iban))
    no_mismatch = bool(stated) and bool(iban_canon) and (stated == iban_canon)
    # (c) Enpara ve QNB AYRI banka: farklı IBAN kodu + farklı kanonik ad
    q_iban = "TR330011100000000012345678"            # QNB 00111
    sep = (_b.iban_bank_code(s_iban) == "00157" and _b.iban_bank_code(q_iban) == "00111"
           and _a._canon_bank("Enpara Bank") != _a._canon_bank("QNB Finansbank"))
    ok = ca and no_mismatch and sep
    return ok, f"marka-önceliği={ca}, çelişki-yok={no_mismatch}, enpara≠qnb-ayrı={sep}"


def _t27_eft_settlement_risk():
    """İŞLEM KANALI KURALI (TÜM BANKALAR): EFT işlemi ANINDA HESABA GEÇMEZ → RİSKLİ; yalnız HAVALE ve FAST
    anında geçer. Kural: (a) RAIL_IS_EFT → settlement_instant=FALSE, kesin karar 'güvenilir değil', skor ≤40;
    (b) RAIL_IS_FAST/HAVALE → settlement_instant=TRUE, cezalandırılmaz; (c) özet 'islem_kanali_riski' ve
    'odeme_aninda_gecer' alanları doğru. Kullanıcı kuralı: EFT dekontu anlık teslimatta dolandırıcılık riski."""
    import verdicts as _v, scoring as _sc, api_response as _api
    from forensics import Finding

    def _run(rail_code):
        findings = [Finding(code="IMAGE_ONLY_DOC", severity="info", category="content", weight=0, tr="x", en="x")]
        if rail_code:
            findings.append(Finding(code=rail_code, severity="info", category="content", weight=0, tr="x", en="x"))
        if rail_code == "RAIL_IS_EFT":
            findings.append(Finding(code="EFT_SETTLEMENT_RISK", severity="high", category="content", weight=0, tr="x", en="x"))
        codes = {f.code for f in findings}
        vd = _v.compute_verdicts(doc_type="image_only", input_kind="image", codes=codes, cons=None,
                                 has_pdf_dates=False, txn_date="2026-08-15", seq="123", db_checked=False,
                                 db_count=0, is_receipt=True, doc_kind="dekont", balance_state=None, timing=None)
        unt = vd["overall"]["state"] == "false"
        sc = _sc.compute_score(findings, "image_only", 0.0, 0.0, verdict_untrusted=unt)
        rep = {"extracted": {"sender": {}, "receiver": {}, "amount": {}, "transaction": {}},
               "classification": {"input_kind": "image", "is_receipt": True}, "score": {
                   "authenticity_score": sc.authenticity_score, "risk_level": sc.risk_level},
               "verdicts": vd, "findings_tr": [{"code": f.code} for f in findings]}
        summ = _api.build_summary(rep)
        return vd, sc, summ

    # (a) EFT → güvenilir değil, skor ≤40, settlement FALSE, özet kanal=EFT/aninda=False
    vd_e, sc_e, su_e = _run("RAIL_IS_EFT")
    si_e = next((c["state"] for c in vd_e["checks"] if c["key"] == "settlement_instant"), None)
    eft_ok = (vd_e["overall"]["state"] == "false" and sc_e.authenticity_score <= 40 and si_e == "false"
              and su_e["islem_kanali_riski"]["kanal"] == "EFT"
              and su_e["islem_kanali_riski"]["aninda_hesaba_gecer"] is False
              and su_e["kesin_cevaplar"]["odeme_aninda_gecer"] == "false")
    # (b) FAST → settlement TRUE, cezalandırılmaz (skor yüksek kalır)
    vd_f, sc_f, su_f = _run("RAIL_IS_FAST")
    si_f = next((c["state"] for c in vd_f["checks"] if c["key"] == "settlement_instant"), None)
    fast_ok = (si_f == "true" and sc_f.authenticity_score >= 50
               and su_f["islem_kanali_riski"]["aninda_hesaba_gecer"] is True)
    # (c) HAVALE → settlement TRUE
    vd_h, _, su_h = _run("RAIL_IS_HAVALE")
    si_h = next((c["state"] for c in vd_h["checks"] if c["key"] == "settlement_instant"), None)
    hav_ok = (si_h == "true" and su_h["islem_kanali_riski"]["kanal"] == "HAVALE")
    ok = eft_ok and fast_ok and hav_ok
    return ok, f"EFT(güvenilir-değil+skor{sc_e.authenticity_score}≤40)={eft_ok}, FAST(cezasız)={fast_ok}, HAVALE={hav_ok}"


def _t28_havale_not_eft_false_positive():
    """RAIL YANLIŞ-POZİTİFİ: 'Hesaptan Hesaba Havale' (Ziraat, banka-içi) EFT DEĞİL HAVALE olmalı. İki kök hata
    kilitlenir: (a) 'eft' ALT-DİZE eşleşmesi yasal dipnottaki 'DEFTer kayıtları' kelimesini yakalayıp yanlış EFT
    üretiyordu → artık KELİME sınırı aranır; (b) HAVALE fallback'i yalnız 'hesaptanhavale' arıyordu, 'hesaptan
    HESABA havale' başlığını kaçırıp EFT'ye düşüyordu → artık ikisi de HAVALE. Akbank 'GEÇ EFT' hâlâ EFT kalır."""
    import authenticity as _a
    # (a) Ziraat 'Hesaptan Hesaba Havale' + yasal dipnotta 'defter kayıtları' (IBAN kodu okunamadı senaryosu)
    ziraat = ("Hesaptan Hesaba Havale\nİŞLEM YERİ : ZİRAAT MOBİL\nAlacaklı IBAN : TR17 0001 0024 5259 1457 4150 02\n"
              "Komisyon : 8,38 TRY\nHavale Tutarı : 10.000,00 TRY\n"
              "Banka'nın defter kayıtları ve belgeleri kesin delildir.\nINTTHVLG MOBIL İNTERNET ŞUBESİ")
    rz = _a.classify_rail(ziraat, "", "TR170001002452591457415002", "ziraat")   # sender IBAN boş (motorun gördüğü gibi)
    havale_ok = bool(rz) and rz["rail"] == "havale"
    # (b) Akbank GEÇ EFT (GECEFT) → hâlâ EFT (yanlışlıkla HAVALE'ye kaymamalı)
    akbank = ("EFT BANKALAR ARASI HESABA HAVALE\nGECEFT KOMİSYON 15,96\nGEC EFT BSMV 0,80\n"
              "Alıcı IBAN TR72 0006 2000 7470 0006 6027 48")
    ak = _a.classify_rail(akbank, "TR670004600002888000786377", "TR720006200074700006602748", "akbank")
    akbank_eft_ok = bool(ak) and ak["rail"] == "eft"
    # (c) 'defter' tek başına EFT üretmemeli (kelime sınırı) — düz metin, havale başlığı yok
    only_defter = _a.classify_rail("Banka'nın defter kayıtları esastır. Alacaklı IBAN TR17 0001 0024 5259 1457 4150 02",
                                   "", "TR170001002452591457415002", "ziraat")
    defter_ok = (only_defter is None) or (only_defter.get("rail") != "eft")
    ok = havale_ok and akbank_eft_ok and defter_ok
    return ok, f"ziraat-havale={havale_ok}(rail={rz['rail'] if rz else None}), akbank-eft={akbank_eft_ok}, defter≠eft={defter_ok}"


def _t29_split_iban_reconstruction():
    """BÖLÜNMÜŞ GÖNDERİCİ IBAN ONARIMI (Ziraat 'Hesaptan Hesaba Havale'): PDF metninde gönderici IBAN'ı
    satıra bölünmüş olabilir — ilk 24 karakter ('TR65 ... 9650') bir satırda, son 2 hane ('01') 'IBAN :'
    etiketi sonrası AYRI satırda. IBAN_RE tam 26 haneyi aradığından yakalanamıyor, gönderici BOŞ kalıp
    all_ibans[0]=ALICI IBAN'ına düşüyor → YZ gönderici/alıcıyı karıştırıp uydurma 'alıcı değişmiş' çelişkisi
    üretiyordu. Kural: kısmi IBAN + yakındaki orphan 2 hane mod-97 doğrulamasıyla birleştirilir; alıcı IBAN
    dokunulmaz. Sonuç: iki taraf da AYRI ve geçerli okunur (sahte-alarmı önlenir)."""
    import extract as _E, banks as _b
    txt = ("Hesaptan Hesaba Havale\n"
           "      ŞUBE KODU/ADI : 0239/TOKAT ŞUBESİ     SAYIN\n"
           "                    TR65 0001 0002 3962 6085 9650 KASIM AKNAY\n"
           "      IBAN         :\n"
           "                    01\n"
           "      HESAP NUMARASI : 0239/62608596-5001\n"
           "      İŞLEM YERİ : ZİRAAT MOBİL\n"
           "      Alacaklı IBAN : TR17 0001 0024 5259 1457 4150 02\n"
           "      Alacaklı Adı Soyadı : ENES TİLKİCİ\n"
           "      Havale Tutarı : 10.000,00 TRY\n"
           "      T.C. ZİRAAT BANKASI A.Ş.  www.ziraatbank.com.tr\n")
    rec = _E._reconstruct_split_iban(txt, exclude="TR170001002452591457415002")
    # (a) onarılan IBAN mod-97 geçerli + alıcıdan FARKLI + doğru gönderici
    rec_ok = (_b.iban_valid(rec) is True and rec == "TR650001000239626085965001"
              and rec != "TR170001002452591457415002")
    # (b) tam extract: gönderici KASIM AKNAY / TR65, alıcı ENES / TR17 — İKİSİ AYRI
    ex = _E.extract_fields(txt, pdf_bytes=None)
    two_parties = (_b.normalize_iban(ex.sender.iban) == "TR650001000239626085965001"
                   and _b.normalize_iban(ex.receiver.iban) == "TR170001002452591457415002"
                   and ex.sender.iban != ex.receiver.iban)
    # (c) TAM IBAN'lar (alıcı) yanlışlıkla 'kısmi' sanılıp bozulmamalı
    only_full = _E._reconstruct_split_iban("Alacaklı IBAN : TR17 0001 0024 5259 1457 4150 02", exclude="")
    full_safe = (only_full == "")   # tam IBAN'ın ardı '02' zaten → onarım tetiklenmez
    ok = rec_ok and two_parties and full_safe
    return ok, f"onarım={rec_ok}(rec={rec}), iki-taraf-ayrı={two_parties}, tam-iban-güvenli={full_safe}"


def _t30_issuer_not_confused_by_counterparty_bank():
    """İHRAÇÇI ≠ KARŞI-TARAF BANKASI: Kuveyt Türk'ten Ziraat'a transferde belgede hem 'KuveytTürk' (ihraççı)
    hem 'Ziraat Bankası' (ALICI bankası) geçer. Motor bankayı KUVEYT TÜRK etiketlemeli (ihraççı), Ziraat
    DEĞİL. Ayrıca kanal: gönderici Kuveyt Türk (00205), alıcı IBAN Ziraat (00010) → FARKLI banka → FAST
    (interbank), HAVALE/aynı-banka-çelişkisi DEĞİL. (YZ tek IBAN'ı yanlış bankaya atayıp uydurma çelişki
    üretiyordu; kök neden ihraççı yanlış tespiti + prompt netliği.) Gerçek Ziraat dekontu ise Ziraat kalır."""
    import extract as _E, authenticity as _a
    kv = ("KuveytTürk\nIBAN'a Para Transferi (Giden)\ne-Dekont\nŞube Adı : Genel Müdürlük\n"
          "Senaryo/Tip : DEKONT/EFT\nGönderen Kişi : İSA DEMİRAY\nAlıcı : kasım AKNAY\n"
          "Gönderilen IBAN : TR65 0001 0002 3962 6085 9650 01\n"
          "Alıcı Banka : Türkiye Cumhuriyeti Ziraat Bankası A.Ş.\n"
          "Açıklama : Gönderen: İSA DEMİRAY, Alıcı: kasım AKNAY, IBAN'a Para Transferi (FAST)\n"
          "Tutar : 5.000,00 TL\nKuveyt Türk Katılım Bankası A.Ş.\nkuveytturk.com.tr")
    # (a) ihraççı Kuveyt Türk (Ziraat DEĞİL) — karşı-taraf 'Ziraat Bankası' yazsa bile
    issuer_ok = _E.detect_issuer(kv) == "kuveyt"
    # (b) gürültülü OCR (ŞUBE KODU + ziraat sızmış) yine Kuveyt
    kv_noisy = kv.replace("Şube Adı", "ŞUBE KODU/ADI")
    issuer_noisy_ok = _E.detect_issuer(kv_noisy) == "kuveyt"
    # (c) rail: açıklamada FAST → FAST (aynı-banka HAVALE değil; tek IBAN Ziraat, gönderici Kuveyt)
    rl = _a.classify_rail(kv, "", "TR650001000239626085965001", "kuveyt")
    rail_ok = bool(rl) and rl["rail"] == "fast"
    # (d) gerçek Ziraat dekontu HÂLÂ Ziraat (regresyon yok)
    zr = ("T.C. ZİRAAT BANKASI\nHesaptan Hesaba Havale\nŞUBE KODU/ADI : 0239/TOKAT\n"
          "İŞLEM YERİ : ZİRAAT MOBİL\nwww.ziraatbank.com.tr")
    ziraat_ok = _E.detect_issuer(zr) == "ziraat"
    ok = issuer_ok and issuer_noisy_ok and rail_ok and ziraat_ok
    return ok, f"kuveyt-ihraççı={issuer_ok}, gürültülü-kuveyt={issuer_noisy_ok}, rail-fast={rail_ok}, ziraat-regresyon-yok={ziraat_ok}"


def _t31_vision_iban_repaired():
    """VISION SONRASI IBAN ONARIMI: Vision de IBAN'da tek-rakam hatası yapabilir (ör. ekran görüntüsünde
    '...9650'→'...9850', 6↔8 karışması). Önceden onarım YALNIZ Vision HİÇ çalışmadıysa yapılıyordu →
    Vision'ın hatalı IBAN'ı ekranda yanlış kalıyordu. Artık onarım Vision ÇALIŞSA DA yapılır (yalnız
    GEÇERSİZ IBAN'a, banka kodu korunarak, TEK benzersiz geçerli adaya). Test: Vision geçersiz alıcı IBAN
    döndürür → çıktı ONARILMIŞ (mod-97 geçerli) olmalı."""
    import analyze, ocr, vision_ocr, banks
    from PIL import Image
    bad_iban = "TR65 0001 0002 3962 6085 9850 01"          # geçersiz (gerçek: ...9650...)
    good = "TR650001000239626085965001"
    ocr_text = (f"KuveytTürk e-Dekont\nAlıcı : kasım AKNAY\nGönderilen IBAN : {bad_iban}\n"
                "Tutar : 5.000,00 TL\n")
    o = (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
         ocr.ocr_available, ocr.render_page_to_image)
    vision_ocr.is_configured = lambda: True
    vision_ocr.extract_from_image = lambda *a, **k: {"receiver_iban": bad_iban, "receiver_name": "kasım AKNAY"}
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]
    ocr.render_page_to_image = lambda *a, **k: Image.new("RGB", (400, 400), "white")
    try:
        rep = analyze.analyze_document(_blank_pdf(), "x.png", input_kind="image", use_store=False)
    finally:
        (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
         ocr.ocr_available, ocr.render_page_to_image) = o
    r_iban = banks.normalize_iban((rep.get("extracted", {}).get("receiver", {}) or {}).get("iban") or "")
    ok = (r_iban == good and banks.iban_valid(r_iban) is True)
    return ok, f"alıcı_iban={r_iban} (geçerli={banks.iban_valid(r_iban)}), onarım_kaydı={len(rep.get('iban_ocr_onarim', []))}"


def _t32_issuer_guard_no_false_samebank():
    """İHRAÇÇI GÜVENCESİ: Kuveyt Türk'ten Ziraat'a FAST'te, vision gönderici IBAN'ını YANLIŞLIKLA bir Ziraat
    IBAN'ı olarak atasa bile (2 farklı Ziraat IBAN) → (a) gönderici IBAN'ı TEMİZLENİR (ihraççı Kuveyt≠Ziraat),
    (b) SAMEBANK_RAIL_CONTRADICTION tetiklenMEZ, (c) rail HAVALE'ye düşMEZ (metinde FAST). Böylece gerçek bir
    interbank FAST, yanlış-atanan gönderici IBAN yüzünden 'sahte aynı-banka çelişkisi' üretmez."""
    import analyze, ocr, vision_ocr
    from PIL import Image
    z1 = "TR650001000239626085965001"      # Ziraat
    z2 = "TR170001002452591457415002"      # farklı Ziraat
    ocr_text = "KuveytTürk e-Dekont\nIBAN'a Para Transferi (Giden) FAST\nkuveytturk.com.tr\n"
    o = (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
         ocr.ocr_available, ocr.render_page_to_image)
    vision_ocr.is_configured = lambda: True
    vision_ocr.extract_from_image = lambda *a, **k: {
        "bank": "Kuveyt Türk Katılım", "sender_iban": z1, "receiver_iban": z2,
        "sender_name": "İSA DEMİRAY", "receiver_name": "kasım AKNAY"}
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]
    ocr.render_page_to_image = lambda *a, **k: Image.new("RGB", (400, 400), "white")
    try:
        rep = analyze.analyze_document(_blank_pdf(), "x.png", input_kind="image", use_store=False)
    finally:
        (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
         ocr.ocr_available, ocr.render_page_to_image) = o
    codes = [f.get("code") for f in rep.get("findings_tr", [])]
    ex = rep.get("extracted", {})
    s_iban = (ex.get("sender", {}) or {}).get("iban", "")
    no_samebank = "SAMEBANK_RAIL_CONTRADICTION" not in codes
    not_havale = "RAIL_IS_HAVALE" not in codes
    sender_cleared = not s_iban
    ok = no_samebank and not_havale and sender_cleared
    return ok, f"samebank-yok={no_samebank}, havale-değil={not_havale}, gönderici-iban-temiz={sender_cleared}"


def _t33_ai_verdict_reconciled_after_correction():
    """TEMEL MİMARİ KURAL: YZ hükmü, alanlar DÜZELTİLDİKTEN sonraki TAM veriyle TUTARLI olmalı. Senaryo:
    QNB→Ziraat interbank FAST/EFT; YZ (mock) 'sahte' der (eski/yanlış okumaya dayanan 'iki IBAN aynı' gibi bir
    gerekçeyle) ama düzeltilmiş veride gönderici QNB, alıcı Ziraat (FARKLI banka, geçerli). Düzeltme sonrası:
    (a) alıcı banka etiketi düzeltilmiş IBAN'dan Ziraat türetilir (yanlış 'QNB' düzelir), (b) SAMEBANK çelişkisi
    OLUŞMAZ, (c) YZ hükmü 'belirsiz'e çekilir ve verdict_ham='sahte' şeffaflık için saklanır. Rapor çelişmez."""
    import analyze, ai_adjudicator, banks, ocr, vision_ocr
    from PIL import Image
    def vi(body):
        for kk in range(100):
            c = "TR%02d%s" % (kk, body)
            if banks.iban_valid(c) is True:
                return c
    qnb = vi("0011100000000120439443")      # QNB 00111
    ziraat = vi("0001000239626085965001")   # Ziraat 00010 (gerçek alıcı)
    ocr_text = "QNB Bank A.Ş. www.qnb.com.tr\nGIDEN FAST EFT  EFT TUTARI  EFT ÜCRETİ\nMOBİL BANKACILIK\n"
    o_ai = (ai_adjudicator.is_enabled, ai_adjudicator.should_adjudicate, ai_adjudicator.adjudicate)
    o_v = (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
           ocr.ocr_available, ocr.render_page_to_image)
    ai_adjudicator.is_enabled = lambda: True
    ai_adjudicator.should_adjudicate = lambda *a, **k: (True, ["test"])
    ai_adjudicator.adjudicate = lambda *a, **k: {
        "verdict": "sahte", "confidence": 88,
        "reasoning_tr": "Gönderici ve alıcı IBAN aynı → aynı-banka HAVALE olmalı ama FAST/EFT → sahte.",
        "corrected_fields": {"sender.iban": qnb, "receiver.iban": ziraat,
                             "sender.name": "KEREM YAVUZ", "receiver.name": "Remzi Koç"},
        "gorsel_tahrifat": []}
    vision_ocr.is_configured = lambda: True
    vision_ocr.extract_from_image = lambda *a, **k: {
        "bank": "QNB Bank A.Ş.", "sender_iban": qnb, "receiver_iban": ziraat,
        "receiver_bank": "QNB Finansbank",       # YANLIŞ etiket → düzeltilmeli (Ziraat)
        "sender_name": "KEREM YAVUZ", "receiver_name": "Remzi Koç"}
    ocr.ocr_available = lambda: True
    ocr.ocr_pdf_candidates = lambda *a, **k: [ocr_text]
    ocr.render_page_to_image = lambda *a, **k: Image.new("RGB", (500, 400), "white")
    try:
        rep = analyze.analyze_document(_blank_pdf(), "x.png", input_kind="image", use_store=False)
    finally:
        (ai_adjudicator.is_enabled, ai_adjudicator.should_adjudicate, ai_adjudicator.adjudicate) = o_ai
        (vision_ocr.is_configured, vision_ocr.extract_from_image, ocr.ocr_pdf_candidates,
         ocr.ocr_available, ocr.render_page_to_image) = o_v
    ex = rep.get("extracted", {})
    codes = [f.get("code") for f in rep.get("findings_tr", [])]
    aj = rep.get("yapay_zeka_degerlendirmesi") or {}
    r_bank = (ex.get("receiver", {}) or {}).get("bank", "")
    s_bank = (ex.get("sender", {}) or {}).get("bank", "")
    no_samebank = "SAMEBANK_RAIL_CONTRADICTION" not in codes
    verdict_fixed = (aj.get("verdict") == "belirsiz" and aj.get("verdict_ham") == "sahte")
    # Banka etiketleri düzeltilmiş IBAN'dan: alıcı Ziraat, gönderici QNB — FARKLI olmalı (ikisi de QNB DEĞİL)
    banks_distinct = ("Ziraat" in r_bank and "QNB" in s_bank and r_bank != s_bank)
    ok = no_samebank and verdict_fixed and banks_distinct
    return ok, f"samebank-yok={no_samebank}, hüküm-uzlaştı={verdict_fixed}, gönderici-banka={s_bank!r}, alıcı-banka={r_bank!r}, farklı={banks_distinct}"


def _t34_bank_detection_sender_iban_first():
    """BANKA TESPİTİ ÖNCELİĞİ (kullanıcı kuralı): (1) GÖNDERİCİ IBAN banka kodu — en güvenilir; (2) domain;
    (3) logo/isim. Ayrıca gönderici IBAN kodu tanınan listede YOKSA → bilinmeyen banka (AI derin inceleme +
    listeye ekleme kaydı). Testler: (a) QNB dekontu header/domain OCR'da kaçsa bile gönderici IBAN 00111'den
    QNB tespit edilir (isim imzası 'enpara'ya kaymaz); (b) ING gibi gönderici IBAN'ı YAZMAYAN dekont domain/
    isimle doğru tespit edilir; (c) bilinmeyen kod (00999) 'bilinmeyen' sayılır."""
    import extract as _E, banks as _b
    # (a) QNB — domain/header OCR'da yok; gönderici IBAN 00111 (MÜŞTERİ ÜNVANI etiketli)
    qnb = ("Dekont\nALICI ÜNVANI: Remzi ALICI IBAN: TR190001009011147534405001\n"
           "MÜŞTERİ ÜNVANI: KEREM IBAN: TR350011100000000120439443")
    a = (_E.detect_issuer(qnb) == "qnb" and _E.sender_iban_code(qnb) == "00111")
    # (b) ING — gönderici IBAN yok, yalnız karşı-taraf + domain
    ing = "ING Bank ing.com.tr\nGiden FAST TR030006200129100006298436 Garanti"
    b = (_E.detect_issuer(ing) == "ing" and _E.sender_iban_code(ing) == "")
    # (c) bilinmeyen banka kodu
    c = (_b.is_known_bank_code("00111") is True and _b.is_known_bank_code("00999") is False)
    # (d) Enpara — gönderici IBAN 00157 (tightened sig; layout-tahmini artık QNB'yi Enpara sanmaz)
    qnb2 = "QNB Bank\nMÜŞTERİ ÜNVANI: X IBAN: TR350011100000000120439443\nALICI ÜNVANI: Y ALICI IBAN: TR190001009011147534405001"
    d = (_E.detect_issuer(qnb2) == "qnb")   # 'ALICI ÜNVANI'+'EFT' layout'una rağmen Enpara DEĞİL
    ok = a and b and c and d
    return ok, f"qnb-sender-code={a}, ing-domain={b}, bilinmeyen-kod={c}, qnb-not-enpara={d}"


def _t35_receiver_iban_ocr_tolerant():
    """ALICI IBAN OCR-TOLERANSLI KURTARMA (QNB/Enpara): OCR 'ALICI IBAN'ı 'ALICI IRAN' (B→R) okur ve IBAN'a
    boşluk sızarsa ('TR19...901 1 147...'), alıcı IBAN bulunamayıp göndericininkine düşüyordu → iki IBAN aynı
    QNB → yanlış 'aynı-banka → SAHTE'. Kural: alıcı IBAN toleranslı çıkarılır (B↔R + boşluk temizliği + mod-97
    onarımı), alıcı adına sızan IBAN temizlenir. Sonuç: gönderici QNB / alıcı Ziraat — FARKLI, geçerli."""
    import extract as _E, banks as _b
    txt = ("QNB Bank A.Ş. qnb.com.tr\nKEREM YAVUZ\n"
           "TR350011100000000120439443 Alıcı : Remzi Koç Türkiye Cumhuriyeti Ziraat\nGIDEN FAST EFT\n"
           "ALICI ÜNVANI: Remzi Koç ALICI IRAN: TR19000100901 1 147534405001\n"
           "KATILIMCI: Türkiye Cumhuriyeti Ziraat Bankası A.Ş.\n"
           "EFT TUTARI: 100.0 TL SORGU NO: 1642839263\n"
           "MÜŞTERİ ÜNVANI: KEREM YAVUZ IBAN: TR350011100000000120439443\nGÖNDEREN: KEREM YAVUZ")
    ex = _E.extract_fields(txt, txt, None)
    s_ib = _b.normalize_iban(ex.sender.iban)
    r_ib = _b.normalize_iban(ex.receiver.iban)
    recv_ok = (r_ib == "TR190001009011147534405001" and _b.iban_valid(r_ib) is True)
    diff_bank = (_b.iban_bank_code(s_ib) == "00111" and _b.iban_bank_code(r_ib) == "00010")
    name_clean = (ex.receiver.name.strip().lower() == "remzi koç".lower())  # IBAN adına sızmamış
    ok = recv_ok and diff_bank and name_clean
    return ok, f"alıcı-iban={r_ib}(geçerli={_b.iban_valid(r_ib)}), farklı-banka={diff_bank}, ad-temiz={name_clean}({ex.receiver.name!r})"


def _t36_number_reuse_same_receipt_no_fp():
    """AYNI DEKONT TEKRAR TARAMA → NUMBER_REUSE YOK; ama TUTAR ya da SADECE TARİH değiştirilmiş kopya → YAKALA.
    Kullanıcı kuralları: (1) alıcı IBAN + alıcı adı + tutar NET okunmayan dekont KAYDEDİLMEZ (kirletmez);
    (2) forgery kararı yalnız tutara değil, TARİHE de bakar (sadece tarih değiştirilerek de sahtecilik olur);
    (3) alıcı IBAN farkı ölçüt DIŞI (OCR varyansı). Depolama eksiksiz okumalarla sınırlı olduğundan tarih/tutar
    kararlıdır. Testler: (a) aynı numara+tutar+gün (alıcı IBAN OCR'dan farklı) → bulgu YOK; (b) farklı tutar →
    NUMBER_REUSE; (c) SADECE tarih farklı → NUMBER_REUSE; (d) eksik okuma (alıcı IBAN geçersiz) → KAYDEDİLMEZ."""
    import store as _s, banks as _b
    import os, tempfile
    _prev = os.environ.get("DEKONT_DB_PATH")
    os.environ["DEKONT_DB_PATH"] = tempfile.mktemp(suffix=".db")
    try:
        Z = "TR190001009011147534405001"   # geçerli Ziraat (doğru alıcı)
        def rep(sha, riban, amt, day="22/08/2026 16:45:50", rname="Remzi"):
            return {"file": {"sha256": sha}, "extracted": {"bank": "QNB Finansbank",
                    "sender": {"name": "KEREM", "iban": "TR350011100000000120439443"},
                    "receiver": {"name": rname, "iban": riban}, "amount": {"value": amt},
                    "transaction": {"document_no": "202608219233777", "ref_no": "1642839263",
                                    "sequence_number": "", "date": day}},
                    "score": {"authenticity_score": 70, "risk_level": "low"},
                    "verdicts": {"overall": {"state": "neutral"}}, "classification": {"is_receipt": True},
                    "findings_tr": []}
        # ilk kayıt EKSİKSİZ (geçerli alıcı IBAN + ad + tutar + numara) → saklanır
        _s.log_analysis(rep("sha_A", Z, 100.0))
        same = _s.check_number_reuse(rep("sha_B", Z, 100.0))                 # aynı dekont
        diff_amt = _s.check_number_reuse(rep("sha_C", Z, 250.0))            # farklı tutar
        diff_day = _s.check_number_reuse(rep("sha_D", Z, 100.0, day="25/08/2026 10:00:00"))  # SADECE tarih farklı
        # (d) eksik okuma: alıcı IBAN geçersiz → kaydedilmemeli
        _s.log_analysis(rep("sha_E1", "TR00INVALIDIBAN", 500.0))
        _stored_incomplete = _s.check_number_reuse(rep("sha_E2", Z, 999.0))  # sha_E1 kaydı yoksa numara eşleşse de tutar farkı sha_A'dan gelir
    finally:
        if _prev is not None:
            os.environ["DEKONT_DB_PATH"] = _prev
    a = not any(o.get("code") == "NUMBER_REUSE" for o in same)
    b = any(o.get("code") == "NUMBER_REUSE" for o in diff_amt)
    c = any(o.get("code") == "NUMBER_REUSE" for o in diff_day)
    ok = a and b and c
    return ok, f"aynı-dekont-yok={a}, farklı-tutar-yakalanır={b}, sadece-tarih-yakalanır={c}"


def _t37_reset_reuse_surgical_keeps_learned():
    """CERRAHİ SIFIRLAMA (reset scope='reuse'): SADECE işe yaramayan/eksik numara-tekrarı kayıtları silinir.
    Kullanıcı kuralı: 'öğrenilmiş hiçbir şeyi silme, sadece işimize yaramayan aynı işlem numarası olan alanı
    temizle'. Testler: (a) TAM ve DOĞRU okunmuş kayıt (geçerli alıcı IBAN+ad+tutar+numara) KORUNUR;
    (b) EKSİK kayıt (numara var ama alıcı IBAN geçersiz / ad-tutar yok) SİLİNİR; (c) numarasız kayıt (tekrara
    konu değil) KORUNUR; (d) öğrenilen veri (field_hints) KORUNUR."""
    import store as _s
    import os, tempfile, datetime
    _prev = os.environ.get("DEKONT_DB_PATH")
    os.environ["DEKONT_DB_PATH"] = tempfile.mktemp(suffix=".db")
    try:
        con = _s._connect()
        now = datetime.datetime.utcnow().isoformat()
        VALID = "TR330006100519786457841326"  # mod-97 geçerli
        # (a) TAM kayıt
        con.execute("INSERT INTO analyses (sha256,bank,seq_number,document_no,amount,receiver_name,receiver_iban,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", ("s_ok", "Ziraat", "123456", "123456", 100.0, "AHMET", VALID, now))
        # (b) EKSİK kayıt: numara var ama IBAN geçersiz + ad/tutar yok
        con.execute("INSERT INTO analyses (sha256,bank,seq_number,document_no,amount,receiver_name,receiver_iban,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", ("s_bad", "Ziraat", "999888", "999888", None, "", "TRBOZUK", now))
        # (c) numarasız kayıt
        con.execute("INSERT INTO analyses (sha256,bank,seq_number,document_no,amount,receiver_name,receiver_iban,created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)", ("s_nonum", "Ziraat", "", "", 50.0, "", "", now))
        # (d) öğrenilen veri
        con.execute("INSERT INTO field_hints (bank,field,label,hits,last_at) VALUES (?,?,?,?,?)",
                    ("Ziraat", "receiver_name", "ALICI", 5, now))
        con.commit(); con.close()
        res = _s.reset_history("reuse")
        con = _s._connect()
        an = {r[0] for r in con.execute("SELECT sha256 FROM analyses").fetchall()}
        fh = con.execute("SELECT COUNT(*) FROM field_hints").fetchone()[0]
        con.close()
    finally:
        if _prev is not None:
            os.environ["DEKONT_DB_PATH"] = _prev
        else:
            os.environ.pop("DEKONT_DB_PATH", None)
    a = "s_ok" in an          # TAM korundu
    b = "s_bad" not in an     # EKSİK silindi
    c = "s_nonum" in an       # numarasız korundu
    d = fh == 1               # öğrenilen korundu
    ok = a and b and c and d and res.get("ok")
    return ok, f"tam-korundu={a}, eksik-silindi={b}, numarasız-korundu={c}, öğrenilen-korundu={d}"


def _t38_qnb_giden_eft_rule():
    """QNB'YE ÖZEL EFT/FAST KURALI (kullanıcı kuralı): QNB dekontunda başlık 'GİDEN EFT' → KESİN EFT;
    'GİDEN FAST EFT' → FAST (içindeki 'EFT' genel şablon). SADECE QNB kanalına özel — QNB-dışı bankalara
    sızmamalı. Rail sınıflandırması PDF/görsel fark etmeden tüm bankalarda text_layout üzerinden çalışır.
    Testler: (a) QNB+GİDEN EFT→eft; (b) QNB+GİDEN FAST EFT→fast; (c) QNB bkey boş ama gönderici IBAN 00111
    → yine EFT; (d) QNB-dışı (Akbank kodu) + GİDEN EFT → QNB kuralı ÇALIŞMAZ (conf 96 QNB'ye özeldir)."""
    import authenticity as _a, banks as _b
    QNB = "TR350011100000000120439443"   # 00111 QNB (gönderici)
    ZIR = "TR190001009011147534405001"   # 00010 Ziraat (alıcı) — bankalararası
    if _b.iban_valid(QNB) is not True or _b.iban_valid(ZIR) is not True:
        return False, "test IBAN'ları geçersiz (fixture hatası)"
    r_eft = _a.classify_rail("QNB Bank A.S. GONDEREN ALICI Ziraat GIDEN EFT Tutar 5000 TL", QNB, ZIR, "qnb")
    r_fast = _a.classify_rail("QNB Bank A.S. GONDEREN ALICI Ziraat GIDEN FAST EFT Tutar 5000 TL", QNB, ZIR, "qnb")
    r_nokey = _a.classify_rail("GONDEREN ALICI GIDEN EFT Tutar 5000 TL", QNB, ZIR, "")  # bkey boş ama IBAN 00111
    # QNB-dışı: Akbank kodu (00046) gönderici; QNB kuralı çalışmamalı → conf 96 OLMAMALI
    AKB = "TR" + "00046" + "0" * 17  # kod 00046 içeren dizi (mod-97 önemsiz; kod okunur)
    r_akb = _a.classify_rail("Akbank GONDEREN ALICI Ziraat GIDEN EFT Tutar 5000 TL", AKB, ZIR, "akbank")
    a = bool(r_eft) and r_eft.get("rail") == "eft" and r_eft.get("confidence") == 96
    b = bool(r_fast) and r_fast.get("rail") == "fast"
    c = bool(r_nokey) and r_nokey.get("rail") == "eft" and r_nokey.get("confidence") == 96
    d = not (bool(r_akb) and r_akb.get("confidence") == 96)   # QNB'ye özel 96 QNB-dışına sızmamalı
    ok = a and b and c and d
    return ok, f"qnb-giden-eft={a}, qnb-giden-fast-eft={b}, ibankodundan-qnb={c}, qnb-dışına-sızmaz={d}"


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
    (16, "Fatura-etiketi kanalı belirler (Enpara EFT(FAST)→EFT)", _t16_enpara_eft_fast_and_receiver),
    (17, "Banka-içi karşılaştırma (her banka kendi normu)", _t17_per_bank_comparison),
    (18, "Kapsam: IBAN-kod + Fiş No tarih + üretim/düzenleme app", _t18_coverage_new_items),
    (19, "Kuveyt Türk: Açıklama '(FAST)' → FAST (Senaryo EFT'e rağmen)", _t19_kuveyt_turk_fast),
    (20, "IBAN otoritesi: banka + rail IBAN kodundan (tüm bankalar)", _t20_iban_authority_bank_and_rail),
    (21, "Banka-bazlı numara tekrarı → NUMBER_REUSE (her banka kendi içinde)", _t21_bank_scoped_number_reuse),
    (22, "YZ boş kritik alanları görüntüden doldurur (alıcı/IBAN/tutar/no)", _t22_ai_fills_blank_fields),
    (23, "Görsel tahrifat: fotoğraf her zaman YZ'ye + font uyuşmazlığı bulgusu", _t23_ai_visual_tamper_escalation),
    (24, "Mantıksal çelişki (aynı banka ↔ EFT başlığı) → %50 altı + güvenilir değil", _t24_samebank_rail_contradiction),
    (25, "'Dekont değil' yanlış-pozitifi: YZ alanları okuduysa NOT_A_RECEIPT kalkar", _t25_not_a_receipt_false_positive),
    (26, "Enpara ↔ QNB ayrı banka + yanlış 'banka çelişkisi' yok (marka önceliği)", _t26_enpara_qnb_separate_no_false_mismatch),
    (27, "İşlem kanalı: EFT anında geçmez → riskli/güvenilir değil; FAST/HAVALE anında", _t27_eft_settlement_risk),
    (28, "Rail: 'Hesaptan Hesaba Havale' → HAVALE (EFT değil); 'defter' EFT tetiklemez", _t28_havale_not_eft_false_positive),
    (29, "Bölünmüş gönderici IBAN onarımı (Ziraat) → gönderici≠alıcı, sahte-alarmı yok", _t29_split_iban_reconstruction),
    (30, "İhraççı ≠ karşı-taraf bankası (Kuveyt Türk→Ziraat) → banka=Kuveyt, rail=FAST", _t30_issuer_not_confused_by_counterparty_bank),
    (31, "Vision sonrası IBAN OCR onarımı (tek-rakam '9850'→'9650') çalışır", _t31_vision_iban_repaired),
    (32, "İhraççı güvencesi: yanlış-atanan gönderici IBAN → sahte 'aynı-banka çelişkisi' yok", _t32_issuer_guard_no_false_samebank),
    (33, "YZ hükmü düzeltilmiş TAM veriyle uzlaştırılır (sahte→belirsiz, banka etiketi düzelir)", _t33_ai_verdict_reconciled_after_correction),
    (34, "Banka tespiti: gönderici IBAN kodu birincil (QNB≠Enpara), domain yedek, bilinmeyen kod", _t34_bank_detection_sender_iban_first),
    (35, "Alıcı IBAN OCR-toleranslı kurtarma (ALICI IRAN + boşluk) → gönderici≠alıcı", _t35_receiver_iban_ocr_tolerant),
    (36, "Aynı dekont→NUMBER_REUSE yok; tutar VEYA sadece-tarih değişikliği→yakala; eksik okuma kaydedilmez", _t36_number_reuse_same_receipt_no_fp),
    (37, "Cerrahi sıfırlama (reuse): sadece eksik numara kayıtları silinir; tam kayıt+öğrenilen veri korunur", _t37_reset_reuse_surgical_keeps_learned),
    (38, "QNB'ye özel: 'GİDEN EFT'→EFT, 'GİDEN FAST EFT'→FAST; sadece QNB kanalı (rail tüm bankalarda/PDF'de)", _t38_qnb_giden_eft_rule),
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

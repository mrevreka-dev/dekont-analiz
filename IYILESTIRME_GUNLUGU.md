# İYİLEŞTİRME GÜNLÜĞÜ — "Bulunan Hata → Yapılan Değişiklik"

Bu belge, dekont-analiz motoruna yapılan HER geliştirmeyi **bulunan hata → yapılan değişiklik**
ikilisi olarak, geçmişteki TÜM geliştirmelerle birlikte tutar. Her kaydın karşısında, o
geliştirmeyi koruyan **regresyon testi** numarası vardır (`regresyon_kontrol.py`).

## İki temel kural
1. Yeni bir geliştirme yaparken: (a) buraya bir **Bulunan Hata → Yapılan Değişiklik** kaydı ekle,
   (b) `regresyon_kontrol.py`'ye onu koruyan bir test ekle.
2. **Yeni geliştirme, eski geliştirmeyi ASLA bozmamalı.** Her değişiklikten sonra
   `python3 regresyon_kontrol.py` çalıştır → 8/8 (veya güncel sayı) GEÇMELİ. Bir tanesi bile
   "EZİLMİŞ" derse, eski bir kural bozulmuş demektir; deploy etme, önce onar.

Çalıştırma: `python3 regresyon_kontrol.py`  (çıkış kodu 0 = hepsi korunuyor)

---

## KAYIT #A — 2026-08-19 — Vision denetiminin ezilmesi *(en kritik)*
- **Bulunan Hata:** Daha önce yakalanan tahrifatlı bir dekont, tekrar tarandığında "doğru" göründü.
  IBAN OCR-onarımı Vision kararından ÖNCE çalışıyordu; geçersiz (checksum tutmayan, en şüpheli) IBAN
  "onarılıp geçerli" olunca `_iban_bad=False` oluyor ve 4 alan doluysa **Vision hiç çağrılmıyordu**.
  Böylece `VISION_TEXT_TAMPER` (görsel yazı tahrifatı) ve Vision metnini kullanan tüm denetimler kayboluyordu.
- **Yapılan Değişiklik:** Vision kararı DAİMA ham OCR okumasına dayanır. IBAN onarımı yalnızca Vision
  hiç çalışamadığında (kapalı/hata) devreye giren şeffaf YEDEK'tir; incelemeyi asla azaltmaz.
- **Koruyan Test:** #1 (hatalı kodda FAIL, düzeltmede PASS olduğu kanıtlandı).

## KAYIT #B — 2026-08-19 — OCR çözünürlük düşüşü
- **Bulunan Hata:** IBAN yanlış okundu (…2180**56** → …2180**58**), süre 15-20 sn. "Hızlı OCR" modu
  çözünürlüğü 1600→1200px, render 2.0→1.5 düşürüyordu; yoğun rakamlar bozuluyordu.
- **Yapılan Değişiklik:** Fast modda da tam 1600px + render 2.0. Hız yalnızca tek OCR varyantından gelir.
- **Koruyan Test:** #2.

## KAYIT #C — 2026-08-19 — IBAN OCR-onarımı (güvenli yedek)
- **Bulunan Hata:** Tek-rakam OCR hatası IBAN'ı geçersiz kılıp yanlış sonuç/gereksiz Vision çağrısı üretiyordu.
- **Yapılan Değişiklik:** Görsel-karışan rakamları deneyip **benzersiz** geçerli adaya onarır.
  Railler: benzersiz değilse tahmin etmez; **banka kodunu asla değiştirmez**; **dijital PDF'de çalışmaz**;
  `iban_ocr_onarim` alanıyla şeffaf.
- **Koruyan Testler:** #3, #4.

## KAYIT #D — 2026-08-19 — Sağlık ucu görünürlüğü
- **Bulunan Hata:** YZ denetleyicinin açık/kapalı olduğu dışarıdan görülemiyordu.
- **Yapılan Değişiklik:** `/api/v1/health` artık `ai_adjudicator_enabled` döndürür.
- **Koruyan Test:** (manuel: health ucunu çağır.)

## KAYIT #E — Fotoğraf AI-imza yanlış-pozitifi ("gan")
- **Bulunan Hata:** JPEG ham baytlarında rastgele "gan" 3-harfi AI-imza sanılıyor, orijinal dekontlar
  "yapay zeka üretimi" diye işaretleniyordu.
- **Yapılan Değişiklik:** İmza taraması yalnızca metadata (EXIF software + XMP) bölgelerinde,
  kelime-sınırıyla yapılır. "gan" → "stylegan"/"biggan" ile değiştirildi.
- **Koruyan Test:** #7.

## KAYIT #F — Fotoğraf/OCR'da sahte tahrifat bulguları
- **Bulunan Hata:** Tek-rakam OCR hatası `IBAN_INVALID`, `INTERNAL_DATE_MISMATCH`, `ID_CHECKSUM`,
  `RECEIPT_NO_DATE_MISMATCH`, `CONSISTENCY_FAIL` gibi sahte alarmlar üretiyordu.
- **Yapılan Değişiklik:** Bu denetimler yalnızca dijital PDF'de gerçek tahrifat sayılır; fotoğraf/OCR/
  vision okumasında baskılanır.
- **Koruyan Test:** #5 (IBAN_INVALID için; diğerleri eklenebilir).

## KAYIT #G — Rail (EFT/FAST/HAVALE) sınıflama
- **Bulunan Hata:** "Bankalararası" başlıklı EFT dekontları FAST sanılabiliyordu; Akbank "GEÇ EFT" gözden kaçıyordu.
- **Yapılan Değişiklik:** `classify_rail` katmanlı sınıflama (banka-geneli); aynı-banka+bankalararası
  başlık çelişkisi (`check_samebank_rail_contradiction`); FAST 100.000 TL üst limit kontrolü.
- **Koruyan Test:** #6.

## KAYIT #H — Referans parmak izi motoru
- **Bulunan Hata:** Gelen fotoğraf verisinin orijinal formatlardan sapması (ör. VakıfBank masraf alanında
  TL eksikliği) tespit edilemiyordu.
- **Yapılan Değişiklik:** `reference_profiles.py` — onlarca orijinal PDF'ten çıkarılan banka parmak izleri
  (para birimi eki, kimlik basamak sayıları) ile kıyaslama. VakıfBank masraf = her zaman TL (3/3),
  SORGU=10 hane, İŞLEM=16 hane.
- **Koruyan Test:** #8.

## KAYIT #I — Banka bilgi tabanı + YZ değerlendirici
- **Bulunan Hata:** Her banka aynı mantıkla değerlendiriliyor, tahrifat/uyuşmazlık durumunda insan-gibi
  muhakeme yapılamıyordu.
- **Yapılan Değişiklik:** `bank_knowledge.py` (17 banka notu) + `ai_adjudicator.py` (kural → gerekiyorsa YZ).
  Guardrail: matematiksel kanıtı ezemez, yalnız mod-97 geçerli IBAN düzeltmesi, halüsinasyon yok.
- **Koruyan Test:** (eklenecek — YZ guardrail testi.)

## KAYIT #J — Performans
- **Bulunan Hata:** Tekrarlı sorgular tüm hattı yeniden çalıştırıyor, süre uzuyordu.
- **Yapılan Değişiklik:** Sonuç önbelleği (aynı SHA-256 + motor sürümü → anında): 12,7 sn → 0,004 sn.
  Hızlı OCR (tek varyant), fail-fast timeout'lar.
- **Koruyan Test:** (eklenecek — cache_get/put testi.)

---

## DEĞİŞMEZ KURALLAR (her zaman korunmalı)
1. Servise dönen mevcut cevap **key'leri ve URL'leri DEĞİŞMEZ** — yeni çıktılar yalnızca EK alan.
2. Adli araçta **kanıt sessizce değiştirilmez**; düzeltme şeffaf olmalı ve incelemeyi azaltmamalı.
3. Bir güncelleme önceki bir denetimi kapatıyorsa **bilinçli** olmalı, buraya + bekçiye yazılmalı.

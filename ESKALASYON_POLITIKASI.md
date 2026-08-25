# DÜŞÜNME (THINKING) ESKALASYON POLİTİKASI

Bu belge, bir dekont için **yapay zekâ düşünme modunun (extended thinking)** hangi
durumlarda açılacağını tanımlar. Kural **tüm bankalar** için geçerlidir; banka ayrımı yoktur.

Amaç: pahalı düşünme gücünü **her dekonta** değil, yalnızca **gerçekten gereken** dekontlara
harcamak. Böylece isabetin büyük kısmı alınırken maliyet düşük tutulur (~1,2× ≈ her-zaman-açık ~3×).

---

## Akış

**Adım 1 — Her dekont ÖNCE, DÜŞÜNMEDEN yapay zekâ incelemesine girer.**
Bir banka dekontu, özellikle **fotoğraf** ise, hiçbir ayrım gözetmeksizin önce hızlı (düşünmesiz)
AI incelemesinden geçer. Kural motoru + AI bulgular ve kırmızı bayraklar (`celiskiler`) üretir.

**Adım 2 — Bulgulara göre karar ver:**

| Durum | Sonuç |
|---|---|
| **Bulgular "sahte" diyorsa** (deterministik kesin bulgu **veya** AI'ın yüksek-güvenli kırmızı bayrağı hükmü sahteye çekmişse) | **DÜŞÜNME AÇMA.** İş orada biter; dekont sahte işaretlenir. |
| **Bulgular kesin temiz ise** (net dijital PDF, yapısal doğrulama geçti, hiçbir şüphe yok) | **DÜŞÜNME AÇMA.** Zaten karar verildi. |
| **Ortada bir "DURUM" varsa** (ne kesin sahte ne kesin temiz; şüphe var ama kesinleşmedi) | **DÜŞÜNMEYİ TETİKLE** — tek bir düşünmeli tur daha at ve sonucu esas al. |

**Kritik ilke:** Şüphe var *ve* biz zaten **"sahte"** diyorsak → orada kalır, düşünme açılmaz.
Düşünme yalnızca **"sahte diyemedik ama içimiz rahat da değil"** durumunda devreye girer.

---

## "DURUM" (düşünmeyi tetikleyen belirsizlik) tanımı

Aşağıdakilerden **herhangi biri** yeterlidir:

1. **Fotoğraf, image-tavanında (72) takılı ama puan düşüren HİÇBİR güçlü bulgu yok**
   → "temiz görünüyor ama yapısal doğrulanamıyor". *(En önemli tetikleyici — İş Bankası tipi durum.)*
2. **AI hükmü "belirsiz"** ya da **düşük-güvenli (%40–70) bir bayrak** kaldırdı → modelin kendisi emin değil.
3. **Deterministik yumuşak ipucu** var ama kesin eşiğin altında (sınırda tarih farkı, kesinleşmemiş referans şüphesi vb.).
4. **Dijital olması gereken belgenin fotoğrafı/ekran görüntüsü** (e-Dekont / GİB / e-fatura ibaresi + `image_only`).
5. **Bilinmeyen banka / az-örnekli şablon** (`UNKNOWN_BANK_CODE`).
6. **Kara-liste yakın-eşleşmesi** (`KNOWN_FAKE`), başka güçlü bulgu yokken.

---

## Sabit ilkeler

- **TUTAR tetikleyicisi YOKTUR.** Yüksek tutar tek başına düşünmeyi açmaz.
- Düşünme **kesin sahtede de, kesin temizde de** açılmaz — yalnız gri bölgede.
- **Güvenlik sınırı:** eskalasyona/günlük düşünme bütçesine bir tavan konur; bir gün aşırı tetiklenirse
  maliyet patlamaz.

---

## Uygulama (kod)

- `ai_adjudicator.is_thinking_enabled()` — özellik açık mı? **Varsayılan KAPALI**; `DEKONT_THINK_ENABLED=1`
  ile açılır (ayrıca `DEKONT_THINK_BUDGET`, varsayılan 3000 düşünme token'ı).
- `ai_adjudicator.should_escalate_to_thinking(ai_result, findings, extraction, input_kind, text_source)`
  → `(tetikle_mi, sebep)`. Yukarıdaki politikayı birebir uygular.
- `ai_adjudicator.adjudicate(..., thinking_budget=N)` — `N≥1024` iken API çağrısında düşünme açılır
  (`thinking:{type:enabled, budget_tokens:N}`, `max_tokens` bütçe+çıktı olacak şekilde büyütülür).
- `analyze.py` — birinci (düşünmesiz) turdan SONRA, `is_thinking_enabled()` ve `should_escalate_to_thinking()`
  olumluysa ikinci (düşünmeli) tur atılır; sonucu `yapay_zeka_degerlendirmesi`'ni değiştirir
  (`dusunme: {acildi, sebep, butce}` alanı eklenir).
- `self_check` **#46** karar mantığını kilitler. Canlı düşünme davranışı, bayrak açıldıktan sonra
  gerçek dekontlarla doğrulanır ve gri-bant eşikleri ölçüme göre ayarlanır.

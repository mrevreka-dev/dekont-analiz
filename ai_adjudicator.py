"""
YZ Değerlendirici (AI Adjudicator) — kural motorunun ÜSTÜNE oturan akıl-yürütme + düzeltme katmanı.

MANTIK (kullanıcı isteği):
  1) ÖNCE deterministik kural motoru çalışır (alanlar + bulgular üretir).
  2) Bir TAHRİFAT/UYUŞMAZLIK bulgusu VAR ise, YA DA kritik alanlar BOŞ/ŞÜPHELİ ise, dekont bu
     katmana ESKALE edilir.
  3) Bu katman görüntüyü DOĞRUDAN inceler ve:
       - Bulgunun GERÇEK mi yoksa kural/okuma HATASI mı olduğunu değerlendirir (bulgu üzerinde çalışır).
       - Yanlış okunan alanı (ör. IBAN) NETLEŞTİRİP YENİDEN OKUR; BOŞ bırakılan alanları yeniden analiz
         eder (isim yanlış yerden alınmışsa düzeltir).
       - Uzman gibi gerekçeli bir HÜKÜM üretir (benim değerlendirdiğim gibi).
       - Sistemin o BANKA için kod/kural iyileştirmesi gerekiyorsa yapılandırılmış bir TEŞHİS notu üretir
         (geliştiriciye; otomatik kod değiştirmez — adli araçta bu riskli).

GÜVENLİK KORKULUKLARI:
  - Matematiksel KESİN kanıtları (aynı-banka çelişkisi, revizyon-tahrifatı, dijital-PDF'te IBAN mod-97,
    kimlik alan çelişkisi) EZMEZ; onların ÜSTÜNE akıl yürütür. (Fotoğrafta IBAN geçersizliği ise
    genelde OKUMA hatasıdır → yeniden okunur.)
  - Yalnız görüntüde AÇIKÇA okunabilen değeri düzeltir; emin değilse boş bırakır ve banka teyidi önerir.
  - Halüsinasyon yasağı: kanıtsız hüküm vermez. Çıktısı deterministik hükümden AYRI tutulur (denetlenebilir).

BAĞIMSIZLIK: Mevcut API cevap anahtarlarını/URL'lerini DEĞİŞTİRMEZ. Sonuç, rapora EK (additive) bir
alan olarak (`yapay_zeka_degerlendirmesi`) eklenir; yalnızca DEKONT_AI_ADJUDICATOR=1 ve ANTHROPIC_API_KEY
varken çalışır.

Yapılandırma:
  ANTHROPIC_API_KEY        : gerekli.
  DEKONT_AI_ADJUDICATOR    : '1' ise aktif (varsayılan '0' — kapalı, mevcut davranış korunur).
  DEKONT_ADJUDICATOR_MODEL : model adı (varsayılan DEKONT_VISION_MODEL ya da claude-3-5-sonnet-latest).
"""
from __future__ import annotations

import os
import io
import json
import base64
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-5"
# HIZLI (hafif) model: tertemiz dijital PDF'te double-check için — düşük gecikme/maliyet. Fotoğraf ve
# şüpheli belgelerde DEFAULT_MODEL (Sonnet) + vision kullanılır. Env ile değiştirilebilir.
DEFAULT_LIGHT_MODEL = "claude-haiku-5"

# Deterministik olarak KESİN kabul edilen bulgu kodları — YZ bunları EZEMEZ (yalnız açıklar).
_HARD_PROOF_CODES = {
    "SAMEBANK_RAIL_CONTRADICTION", "ID_FIELD_MISMATCH", "REV_AMOUNT_CHANGED", "REV_CONTENT_CHANGED",
    "AMOUNT_MISMATCH", "SELF_TRANSFER", "SEQ_DB_DUPLICATE", "SEQ_CREATION_BACKDATE",
    "PDFIUM_PRODUCED", "BROWSER_RERENDER",
}
# Fotoğraf/OCR'da OKUMA hatası olabilen (YZ'nin yeniden-okuyup düzeltebileceği) bulgular.
_REREAD_CODES = {
    "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH", "ID_CHECKSUM_INVALID",
    "LOW_IMAGE_QUALITY", "CONSISTENCY_FAIL", "VISION_TEXT_TAMPER", "NOT_A_RECEIPT",
}

# Yeniden okunması/doldurulması öncelikli kritik alanlar.
_CRITICAL_FIELDS = ["sender.name", "sender.iban", "receiver.name", "receiver.iban",
                    "amount.value", "transaction.date"]


def is_enabled() -> bool:
    if os.environ.get("DEKONT_AI_ADJUDICATOR", "0") != "1":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def should_adjudicate(findings: list, extraction: dict, input_kind: str = "pdf") -> tuple[bool, list]:
    """Bu dekont YZ değerlendiricisine gitmeli mi? (tetik, nedenler) döndürür.
    Tetikler: (a) high/critical bir bulgu var; (b) kritik alan(lar) boş; (c) yeniden-okunabilir bulgu."""
    # HIZ: yalnız GERÇEKTEN gerekince eskale et (temiz dekontta YZ'ye gidilmez → hızlı ve ucuz).
    reasons = []
    sev = {(f.get("code")): f.get("severity") for f in (findings or [])}
    if any(s in ("high", "critical") for s in sev.values()):
        reasons.append("Yüksek/kritik önem taşıyan bir bulgu var.")
    # BİLİNMEYEN BANKA (gönderici IBAN kodu listede yok): DERİN YZ incelemesi ŞART — tüm alanlar + işlem
    # kanalı (FAST/HAVALE/EFT) okutulur ki banka listeye doğru bilgiyle eklenebilsin (kullanıcı kuralı).
    if any(f.get("code") == "UNKNOWN_BANK_CODE" for f in (findings or [])):
        reasons.append("Bilinmeyen banka: gönderici IBAN kodu listede yok → derin inceleme + kanal tespiti.")
    # GÖRÜNTÜ/FOTOĞRAF DEKONT: görsel tahrifat (font uyuşmazlığı, yapıştırma, hizalama, montaj) YALNIZ
    # görüntüden görülür; kural motoru rasterize metinde bunu göremez. Bu yüzden fotoğraf/görüntü
    # dekontlar KURAL 'TEMİZ' dese bile HER ZAMAN YZ görsel incelemesine eskale edilir (kullanıcı kuralı).
    if input_kind == "image":
        reasons.append("Görüntü/fotoğraf dekont: görsel tahrifat (font/hizalama/montaj) incelemesi şart.")
    # KRİTİK alanlar boşsa YZ görüntüden okuyup DOLDURUR (kullanıcı kuralı: alıcı adı, alıcı IBAN,
    # tutar, işlem no, referans no ekrana ASLA boş gelmemeli). Bu alanlardan HERHANGİ biri boşsa eskale.
    _core = [f for f in ("sender.name", "sender.iban", "receiver.name", "receiver.iban", "amount.value")
             if not _get(extraction or {}, f)]
    if _core:
        reasons.append("Kritik alan boş: " + ", ".join(_core))
    # İŞLEM TANIMLAYICISI: işlem/doküman no, referans no ve sıra/sorgu no'nun HEPSİ birden boşsa
    # (belgede hiçbir işlem numarası okunamamış) → YZ görüntüden okusun.
    _ids = [f for f in ("transaction.document_no", "transaction.ref_no", "transaction.sequence_number")
            if _get(extraction or {}, f)]
    if not _ids:
        reasons.append("İşlem/referans numarası okunamadı (işlem no, referans no, sıra no boş).")
    # KULLANICI KURALI (DOUBLE-CHECK): skor %100 / dekont tertemiz olsa BİLE her dekont YZ'ye gider.
    # YZ ikinci bir göz olarak teyit eder; kural motorunun kaçırabileceği görsel/bağlamsal tahrifatı
    # yakalar. GÜVENLİK: YZ ASLA çelişki yaratmaz — düzeltilmiş deterministik veri OTORİTERDİR (bkz.
    # 7.96 hüküm kapısı + rail_codes_to_remove + has_definitive_eft_fee). YZ yalnız KANITLA (gorsel_tahrifat
    # ≥50 / somut forgery bulgusu) skoru düşürebilir; kanıtsız 'sahte' hükmü nihai skoru/kararı DÜŞÜRMEZ,
    # yalnız 'belirsiz'e çevrilip uzlaştırma notuyla gösterilir. Bu yüzden double-check güvenlidir.
    if not reasons:
        reasons.append("Rutin çift-kontrol (double-check): skor yüksek olsa da YZ teyidi (kullanıcı kuralı).")
    return (True, reasons)


def _img_b64(pil_img, max_dim: int = 1568):   # Anthropic optimal ~1568px; daha büyüğü hız kazandırmaz
    from PIL import Image
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        s = max_dim / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_SCHEMA_HINT = (
    '{\n'
    '  "verdict": "gerçek | şüpheli | sahte | belirsiz",\n'
    '  "confidence": 0-100,\n'
    '  "reasoning_tr": "uzman gerekçesi (kısa paragraf, kanıta dayalı)",\n'
    '  "corrected_fields": { "receiver.iban": "TR...", "receiver.name": "...", "sender.name": "...", '
    '"amount.value": 63500.00, "transaction.document_no": "...", "transaction.ref_no": "..." },  // GÖRÜNTÜDE '
    'AÇIKÇA okuduğun ve kuralın YANLIŞ/BOŞ verdiği alanlar. ALICI ADI, ALICI IBAN, TUTAR, İŞLEM NO ve '
    'REFERANS/SORGU NO alanları ekrana boş gelmemeli — bu alanlar boşsa GÖRÜNTÜDEN OKU ve DOLDUR; '
    'gerçekten okunamıyorsa KOYMA (asla uydurma)\n'
    '  "finding_reviews": [ {"code":"IBAN_INVALID","gercek_mi":false,"aciklama":"foto okuma hatası; doğru IBAN ..."} ],\n'
    '  "gorsel_tahrifat": [ {"alan":"tutar (yazıyla)","aciklama":"yazıyla yazılan tutar belgenin '
    'genel yazı tipinden farklı bir fontta/kalınlıkta — sonradan yapıştırılmış görünüyor","guven":85} ], '
    '// GÖRÜNTÜDE font/kalınlık/hizalama uyuşmazlığı gördüğün alanlar; yoksa boş bırak\n'
    '  "islem_kanali": {"kanal":"EFT | FAST | HAVALE | belirsiz","aninda_gecer":true,'
    '"kanit":"ücret kalemi/başlık/IBAN kodu kanıtı"},  // İşlem hangi kanaldan gitti? EFT=anında GEÇMEZ '
    '(aninda_gecer=false, RİSKLİ); FAST/HAVALE=anında geçer (true). Ücret kalemine bak: "GEÇ EFT/EFT '
    'TUTARI/ÜCRETİ"→EFT; "FAST Ücreti/TUTARI"→FAST; "Havale Ücreti" veya gönderici=alıcı IBAN aynı banka→HAVALE\n'
    '  "verify_suggestion": "banka teyidi için ne sorgulanmalı (sorgu/işlem no vb.)",\n'
    '  "improvement_notes": [ {"bank":"denizbank","field":"receiver.name","problem":"...",'
    '"suggestion":"...","label_hint":"Alıcı Adı Soyadı"} ]  // label_hint: bu alanın belgede YANINDA '
    'durduğu ETİKET METNİ (sistemin ÖĞRENİP sonraki dekontlarda otomatik uygulaması için)\n'
    '}'
)


def _deterministic_facts(extraction: dict, findings: list, bank_key: str) -> str:
    """BANKA/KANAL GERÇEKLERİNİ IBAN kodundan + mod-97'den KESİN hesaplar ve YZ'ye 'değiştirilemez gerçek'
    olarak sunar. Amaç: YZ'nin banka/kanal/aynı-banka hakkında UYDURMA çıkarım yapmasını KÖKTEN engellemek
    (ör. tek alıcı IBAN'ını ihraççıya atfedip 'iki IBAN da aynı bankada, EFT çelişkisi' demesi). Kural motoru
    bu gerçekleri zaten KESİN biliyor; YZ'nin işi kuralın GÖREMEDİĞİ görsel tahrifat/font/tutar/tarihtir."""
    try:
        import banks as _b
    except Exception:
        return ""
    _s = (extraction or {}).get("sender", {}) or {}
    _r = (extraction or {}).get("receiver", {}) or {}
    s_ib = _b.normalize_iban(_s.get("iban") or "")
    r_ib = _b.normalize_iban(_r.get("iban") or "")
    issuer_lbl = (extraction or {}).get("bank") or _s.get("bank") or _s.get("bank_stated") or ""

    def _bank_of(ib):
        if ib and _b.iban_valid(ib) is True:
            return _b.bank_from_iban(ib), _b.iban_bank_code(ib)
        return "", ""
    s_bank, s_code = _bank_of(s_ib)
    r_bank, r_code = _bank_of(r_ib)
    lines = ["--- DETERMİNİSTİK GERÇEKLER (KESİN — DEĞİŞTİRME, AKSİNİ İDDİA ETME) ---"]
    if issuer_lbl:
        lines.append(f"• Belge ihraççısı (üst başlık/logo) = GÖNDERİCİNİN bankası: {issuer_lbl}.")
    if s_ib:
        lines.append(f"• Gönderici IBAN: {s_ib} → banka kodu {s_code or '?'} = {s_bank or 'belirsiz'}"
                     + (" (mod-97 GEÇERLİ)." if s_bank else "."))
    else:
        lines.append("• Gönderici IBAN belgede YAZILI DEĞİL (gönderici müşteri no ile tanımlı). Bu NORMALDİR.")
    if r_ib:
        lines.append(f"• Alıcı IBAN: {r_ib} → banka kodu {r_code or '?'} = {r_bank or 'belirsiz'}"
                     + (" (mod-97 GEÇERLİ)." if r_bank else "."))
    # Aynı-banka mı? YALNIZ iki geçerli IBAN varken belirlenebilir.
    if s_bank and r_bank:
        if s_code == r_code:
            lines.append(f"• İki taraf da AYNI banka ({s_bank}, kod {s_code}) → banka-içi HAVALE beklenir.")
        else:
            lines.append(f"• Taraf bankaları FARKLI ({s_bank} ≠ {r_bank}) → işlem BANKALARARASIDIR → "
                         "EFT/FAST NORMALDİR, çelişki DEĞİLDİR.")
    else:
        # Tek IBAN durumunda ihraççı (gönderici) bankası ile alıcı IBAN bankasını KIYASLA
        if issuer_lbl and r_bank:
            lines.append(f"• Gönderici bankası ({issuer_lbl}) ile alıcı IBAN bankası ({r_bank}) FARKLI ise işlem "
                         "BANKALARARASIDIR (EFT/FAST NORMAL). Elde TEK IBAN olduğundan 'iki IBAN da aynı bankada' "
                         "DİYEMEZSİN — aynı-banka çelişkisi İDDİA ETME.")
        else:
            lines.append("• Elde tek/eksik IBAN var → 'aynı banka' ya da 'aynı-banka EFT çelişkisi' İDDİA ETME.")
    _rail = next((f.get("code") for f in (findings or [])
                  if f.get("code") in ("RAIL_IS_EFT", "RAIL_IS_FAST", "RAIL_IS_HAVALE")), "")
    _rmap = {"RAIL_IS_EFT": "EFT", "RAIL_IS_FAST": "FAST", "RAIL_IS_HAVALE": "HAVALE"}
    if _rail:
        lines.append(f"• İşlem kanalı (kural motoru): {_rmap[_rail]}.")
    lines.append("Bu gerçekler IBAN banka kodundan ve mod-97'den KESİN belirlendi. Banka adı, banka kodu, "
                 "aynı-banka/farklı-banka ve kanal hakkında BUNLARA AYKIRI bir çıkarım YAPMA (ör. IBAN kodunu "
                 "yanlış okuyup başka banka deme; tek IBAN'ı iki tarafa atfetme). SENİN GÖREVİN kuralın "
                 "GÖREMEDİĞİ şeydir: GÖRSEL TAHRİFAT (font/kalınlık/yapıştırma), tutar aritmetiği, tarih "
                 "tutarlılığı. Banka/kanal FAKTLARINI yeniden türetme.")
    return "\n".join(lines)


def _build_prompt(extraction: dict, findings: list, bank_ctx: str, input_kind: str,
                  text_source: str, facts: str = "") -> str:
    fields_json = json.dumps(extraction or {}, ensure_ascii=False)[:1800]
    finds = [{"code": f.get("code"), "severity": f.get("severity"), "tr": (f.get("tr") or "")[:300]}
             for f in (findings or []) if f.get("severity") in ("high", "critical", "medium") or f.get("weight", 0) > 0]
    finds_json = json.dumps(finds, ensure_ascii=False)[:1400]
    kind_note = ("Bu bir FOTOĞRAF/GÖRÜNTÜdür — IBAN/kimlik gibi rakam-alanlarda kuralın 'geçersiz' demesi "
                 "genelde OKUMA hatasıdır; görüntüden NETLEŞTİRİP doğru değeri OKU."
                 if (input_kind == "image" or text_source in ("ocr", "vision"))
                 else "Bu DİJİTAL bir PDF'tir — metin güvenilirdir; IBAN mod-97/aynı-banka gibi kesin bulguları EZME.")
    return (
        "Sen bir BANKA DEKONTU ADLİ DEĞERLENDİRİCİSİsin. Önce deterministik kural motoru çalıştı ve "
        "aşağıdaki ALANLARI ve BULGULARI üretti. Şimdi GÖRSELİ (varsa) DOĞRUDAN incele ve şu işleri yap:\n"
        "1) Her BULGUYU değerlendir: gerçek bir tahrifat/uyuşmazlık mı, yoksa kuralın/OCR'ın HATASI mı? "
        "Gerekçelendir.\n"
        "2) TÜM alanları görüntüyle doğrula; boş/yanlış okunanı yeniden oku ve corrected_fields'a DOĞRUsunu "
        "yaz (gönderici+alıcı IBAN, isimler, tutar, işlem/referans no, tarih, TC). Değer görüntüde varsa boş "
        "bırakma. BANKA ADI↔IBAN: gönderici ve alıcı için YAZAN banka adını IBAN'ın banka koduyla (TR+kontrol "
        "sonrası 5 hane) kıyasla; farklıysa (ör. yazan Garanti ama IBAN 00067=Yapı Kredi) finding_reviews'a "
        "'RECEIVER_BANK_MISMATCH'/'SENDER_BANK_MISMATCH' yaz. Kodlar: 00010 Ziraat,00015 Vakıf,00046 Akbank,"
        "00062 Garanti,00064 İş Bankası,00067 Yapı Kredi,00111 QNB,00134 Denizbank,00157 Enpara,00205 Kuveyt Türk.\n"
        "   IBAN BANKA KODU KESİN KURALI (ÇOK ÖNEMLİ — sık yapılan hatayı önle): Banka kodu, IBAN'daki 'TR' + 2 "
        "kontrol hanesinden SONRAKİ 5 HANEDİR (5.–9. haneler). Örnek: 'TR65 0001 0002 3962 6085 9650 01' → ilk "
        "grup '0001', ama BANKA KODU İLK 5 HANE = '00010' = ZİRAAT'tır — Kuveyt Türk DEĞİLDİR. '0001'e bakıp "
        "banka tahmin ETME; TAM 5 haneyi al. İHRAÇÇI (üstteki logo/başlık, ör. 'KuveytTürk') = GÖNDERİCİNİN "
        "bankasıdır. Çoğu dekontta SADECE ALICI IBAN'ı yazılıdır (gönderici müşteri no ile tanımlanır, IBAN'ı "
        "yazmaz) → görünen TEK IBAN'ı otomatik OLARAK gönderici/ihraççı bankasına AİT SANMA; o IBAN genelde "
        "ALICI'nındır. AYNI-BANKA çelişkisi iddiası için İKİ TARAFIN da IBAN'ı olmalı ve HER İKİSİNİN banka kodu "
        "AYNI olmalı; elinde TEK IBAN varsa 'aynı banka' DİYEMEZSİN. İhraççı (gönderici) bankası ile alıcı "
        "IBAN'ının bankası FARKLIYSA işlem BANKALARARASIDIR → EFT/FAST NORMALDİR, çelişki DEĞİLDİR.\n"
        "3) GÖRSEL/YAZI TAHRİFAT DENETİMİ — EN ÖNEMLİ, ZORUNLU. Gerçek dekontta TÜM metin TEK yazı tipinde "
        "basılır. Her kritik alanı (tutar rakam+YAZIYLA, gönderici/alıcı IBAN, işlem/referans no, isimler, "
        "tarih/TC) belgenin geneliyle TEK TEK kıyasla. Bir alanın fontu/kalınlığı/hizası farklıysa ya da "
        "kenarları keskin/kaymış görünüyorsa → SONRADAN YAPIŞTIRILMIŞ; gorsel_tahrifat'a AYRI yaz (alan+neden+"
        "güven). ÇOK ÖNEMLİ: DEĞER tutarlılığı (75.000 = YetmişBeşBin) FONT tutarlılığı DEĞİLDİR — DEĞERE değil "
        "HARF BİÇİMİNE bak; belge geneli ince/monospace iken yazıyla tutar KALIN/oransal (Arial Bold gibi) ise "
        "değer eşleşse bile TAHRİFATtır, yüksek güvenle yaz. Tutarsızlık yoksa [] bırak ama denetimi ATLAMA.\n"
        "4) KANITA DAYALI HÜKÜM + güven yüzdesi. HÜKÜM KURALI: SAHTE = somut çelişki/tahrifat var (banka "
        "adı≠IBAN kodu, aynı-banka ama EFT/FAST, farklı-banka ama havale, numara tekrarı, font/yapıştırma, "
        "tutar aritmetiği tutmuyor, dijitalde IBAN mod-97 hatası). GERÇEK = hiçbir somut çelişki/tahrifat YOK "
        "ve gerçeklik sinyalleri VAR (IBAN'lar geçerli+bankalarla uyumlu, aritmetik doğru, ETTN/belge no var, "
        "format banka normuna uygun). ŞÜPHELİ = SADECE gerçek ama kesinleşmemiş bir belirsizlik/zayıf tahrifat "
        "izi olduğunda. ÇOK ÖNEMLİ (temkinli ol ama abartma): standart alan MASKELEME (ör. 'FE**** ŞA****') ve "
        "DÜZELTİLMİŞ OCR HATALARI ŞÜPHE NEDENİ DEĞİLDİR → bunlar varken ve başka somut sorun yoksa hüküm GERÇEK "
        "olmalı, sırf 'tam doğrulayamadım' diye ŞÜPHELİ deme. Yine de kesin 'gerçek' iddiası yerine reasoning'de "
        "kısa bir 'kesin teyit için banka kaydı gerekir' notu bırak. reasoning_tr EN FAZLA 2 cümle, sade.\n"
        "5) Bu bankanın çıkarımında SİSTEMİK bir sorun görürsen (kod/kural iyileştirmesi), "
        "improvement_notes'a banka+alan+sorun+öneri olarak yaz.\n"
        "6) İŞLEM KANALI (KRİTİK, TÜM BANKALAR) — bu dekont EFT mi, FAST mı, HAVALE mi? 'islem_kanali'na yaz. "
        "KURAL: EFT işleminde para alıcı hesabına ANINDA GEÇMEZ (saatli/toplu işlenir, geri çağrılabilir) → "
        "aninda_gecer=false, RİSKLİDİR. FAST ve HAVALE anında + kesin geçer → aninda_gecer=true. Kanalı ÜCRET "
        "KALEMİNDEN belirle: 'GEÇ EFT / GECEFT / EFT TUTARI / EFT ÜCRETİ / EFT BSMV' → EFT; 'FAST Ücreti / "
        "GİDEN FAST TUTARI / FAST BSMV' → FAST; 'Havale Ücreti' ya da gönderici ve alıcı IBAN AYNI bankaya "
        "aitse → HAVALE. Başlıktaki genel şablon ('EFT BANKALAR ARASI HESABA HAVALE' gibi) yanıltıcıdır; "
        "asıl kanıt ÜCRET KALEMİdir. Bu belge sahte olmasa BİLE EFT ise anlık teslimatta risklidir.\n\n"
        "OCR HATASI ≠ TAHRİFAT (ÇOK ÖNEMLİ): Kuralın çıkardığı bir değer görüntüdekiyle uyuşmuyorsa (ör. "
        "IBAN'da bir rakam yanlış, tarih/müşteri no/TCKN yanlış okunmuş, ya da fazladan/karışık bir alan var), "
        "bu neredeyse her zaman OCR/okuma hatasıdır — SAHTECİLİK DEĞİLDİR. Böyle durumda doğru değeri "
        "corrected_fields'a yaz ve belgeyi bu yüzden 'şüpheli/sahte' SAYMA, güveni düşürme. Yalnızca (a) GÖRSEL "
        "tahrifat (font/kalınlık/yapıştırma), (b) MANTIKSAL çelişki (banka adı≠IBAN kodu, aynı-banka ama EFT/FAST, "
        "farklı-banka ama havale, numara tekrarı, tutar aritmetiği tutmaması, kimlik/mod-97 çelişkisi) gerçek "
        "sahtecilik kanıtıdır. Okuma hatalarını sessizce DÜZELT, gerçek çelişkileri BULGU yap.\n"
        "KESİN KANITLARI EZME: aynı-banka çelişkisi, revizyon-tahrifatı, kimlik alan çelişkisi, "
        "dijital-PDF'te IBAN mod-97 hatası MATEMATİKSEL kesinliktir — bunları yalnız AÇIKLA, çürütme. "
        "Yalnız GÖRÜNTÜDE açıkça okunabilen değeri düzelt; emin değilsen alanı KOYMA ve banka teyidi öner. "
        "ASLA uydurma.\n\n"
        f"BELGE TÜRÜ NOTU: {kind_note}\n\n"
        + (f"{facts}\n\n" if facts else "")
        + f"--- BANKA BAĞLAMI ---\n{bank_ctx}\n\n"
        f"--- KURALIN ÇIKARDIĞI ALANLAR (JSON) ---\n{fields_json}\n\n"
        f"--- KURALIN BULGULARI (JSON) ---\n{finds_json}\n\n"
        "SADECE şu şemada GEÇERLİ JSON döndür (başka açıklama yazma):\n" + _SCHEMA_HINT
    )


def adjudicate(extraction: dict, findings: list, bank_key: str = "", pil_image=None,
               input_kind: str = "pdf", text_source: str = "digital", timeout: float = 45.0,
               light: bool = False) -> dict | None:
    """YZ değerlendiricisini çalıştırır. Dönen dict rapora EK alan olarak konur; hata/kapalıysa None.
    light=True (tertemiz dijital PDF double-check): HIZLI model (Haiku) + vision YOK + daha küçük çıktı —
    gecikme/maliyet düşer; kanıta dayalı tam inceleme fotoğraf/şüpheli belgelerde (light=False) yapılır."""
    if not is_enabled():
        return None
    api_key = os.environ["ANTHROPIC_API_KEY"]
    if light:
        # Hafif teyit: önce açık env (ADJUDICATOR_MODEL) varsa ona saygı; yoksa hızlı model.
        model = (os.environ.get("DEKONT_ADJUDICATOR_LIGHT_MODEL")
                 or os.environ.get("DEKONT_ADJUDICATOR_MODEL") or DEFAULT_LIGHT_MODEL)
        pil_image = None                      # hafif yolda vision gönderilmez (dijital PDF zaten metin)
    else:
        model = os.environ.get("DEKONT_ADJUDICATOR_MODEL") or os.environ.get("DEKONT_VISION_MODEL") or DEFAULT_MODEL
    try:
        import bank_knowledge as _bk
        bank_ctx = _bk.context_for(bank_key)
    except Exception:
        bank_ctx = ""
    # Referans parmak izi (gerçek PDF korpusundan) — YZ 'gerçek şablon' bağlamıyla kıyaslasın
    try:
        import reference_profiles as _rp
        _rctx = _rp.context_for(bank_key)
        if _rctx:
            bank_ctx = (bank_ctx + "\n\n" + _rctx) if bank_ctx else _rctx
    except Exception:
        pass
    try:
        _facts = _deterministic_facts(extraction, findings, bank_key)
    except Exception:
        _facts = ""
    prompt = _build_prompt(extraction, findings, bank_ctx, input_kind, text_source, facts=_facts)

    content = []
    if pil_image is not None:
        try:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": _img_b64(pil_image)}})
        except Exception:
            pass
    content.append({"type": "text", "text": prompt})

    # ÖNEMLİ: Sonnet 5 varsayılan "düşünme (thinking)" modunda tüm token'ları düşünmeye harcayıp METİN
    # ÜRETMEDEN kesiliyordu (stop_reason=max_tokens, content=['thinking']) → boş yanıt + YÜKSEK MALİYET
    # (her çağrı 4096 düşünme token'ı). Düşünmeyi KAPATIYORUZ: model doğrudan JSON üretir → hızlı, ucuz,
    # dolu yanıt. 2048 token forensic JSON için yeterli.
    def _call(_model, _max_tokens):
        _body = {"model": _model, "max_tokens": _max_tokens, "thinking": {"type": "disabled"},
                 "messages": [{"role": "user", "content": content}]}
        _req = urllib.request.Request(API_URL, data=json.dumps(_body).encode("utf-8"), method="POST")
        _req.add_header("x-api-key", api_key)
        _req.add_header("anthropic-version", "2023-06-01")
        _req.add_header("content-type", "application/json")
        with urllib.request.urlopen(_req, timeout=timeout) as _resp:
            return json.loads(_resp.read().decode("utf-8"))

    try:
        payload = _call(model, 900 if light else 1400)
    except urllib.error.HTTPError as e:
        try:
            _b = e.read().decode("utf-8")[:400]
        except Exception:
            _b = ""
        print(f"[ai_adjudicator] HTTP {e.code} model={model}: {_b}", flush=True)
        # YEDEK: hafif model geçersiz/erişilemezse (ör. model adı yanlış) double-check KAYBOLMASIN →
        # tam model (Sonnet) ile bir kez yeniden dene (metin-tabanlı; vision zaten gönderilmemişti).
        if light and model != DEFAULT_MODEL:
            try:
                print(f"[ai_adjudicator] hafif model başarısız → {DEFAULT_MODEL} ile yeniden deneniyor", flush=True)
                payload = _call(DEFAULT_MODEL, 1400)
            except Exception as e2:
                print(f"[ai_adjudicator] yedek de başarısız: {type(e2).__name__}: {e2}", flush=True)
                return None
        else:
            return None
    except Exception as e:
        print(f"[ai_adjudicator] error model={model}: {type(e).__name__}: {e}", flush=True)
        return None

    try:
        parts = payload.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except Exception:
        return None
    if not text:
        try:
            _types = [p.get("type") for p in (payload.get("content") or [])]
            print(f"[adjudicator] BOŞ metin. stop_reason={payload.get('stop_reason')} "
                  f"content_types={_types} usage={payload.get('usage')}", flush=True)
        except Exception:
            pass
    obj = _parse_json(text)
    if not obj:
        print(f"[adjudicator] JSON parse edilemedi; ham yanıt başı: {text[:200]!r}", flush=True)
        return None
    _san = _sanitize(obj)
    try:
        _gt = _san.get("gorsel_tahrifat") or []
        _rz = (_san.get("reasoning_tr") or "")[:160]
        print(f"[adjudicator] model={model} verdict={_san.get('verdict')} conf={_san.get('confidence')} "
              f"gorsel_tahrifat={len(_gt)} corrected={list((_san.get('corrected_fields') or {}).keys())} "
              f"reasoning={_rz!r}", flush=True)
    except Exception:
        pass
    return _san


def _salvage_json(frag: str) -> dict | None:
    """max_tokens ile KESİLMİŞ JSON'u kurtarır: ilk '{'ten başlar, sondan kırparak açık tırnak/parantezleri
    dengeler ve en uzun AYRIŞTIRILABİLİR ön-eki bulur. Böylece verdict/confidence/gorsel_tahrifat KURTARILIR."""
    frag = (frag or "").strip()
    i = frag.find("{")
    if i < 0:
        return None
    frag = frag[i:]
    try:
        o = json.loads(frag)
        if isinstance(o, dict):
            return o
    except Exception:
        pass
    for end in range(len(frag), 1, -1):
        s = frag[:end].rstrip().rstrip(",")
        if not s.endswith(("}", "]", '"')) and not s[-1:].isdigit() and s[-1:] not in ("e", "l"):
            continue  # yalnız bir değerin bittiği konumlarda dene (hız)
        cand = s
        if cand.count('"') % 2 == 1:
            cand += '"'
        _sq = cand.count("[") - cand.count("]")
        _cu = cand.count("{") - cand.count("}")
        if _sq > 0:
            cand += "]" * _sq
        if _cu > 0:
            cand += "}" * _cu
        try:
            o = json.loads(cand)
            if isinstance(o, dict) and o.get("verdict"):
                return o
        except Exception:
            continue
    return None


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    a = text.find("{")
    b = text.rfind("}")
    if a < 0:
        return None
    if b > a:
        try:
            o = json.loads(text[a:b + 1])
            if isinstance(o, dict):
                return o
        except Exception:
            pass
    # KURTARMA: yanıt max_tokens'a takılıp KESİLMİŞSE (geçerli JSON kapanmamış), açık tırnak/parantezleri
    # dengeleyip artan şekilde en uzun geçerli ön-eki ayrıştırmayı dene — verdict/gorsel_tahrifat kurtarılır.
    salvaged = _salvage_json(text[a:])
    if salvaged is not None:
        print("[adjudicator] KESİK yanıt kurtarıldı (partial JSON).", flush=True)
        return salvaged
    try:
        o = json.loads(text[a:b + 1])
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def _sanitize(obj: dict) -> dict:
    """Model çıktısını güvenli/temiz hâle getirir (tip ve alan kontrolü)."""
    out = {}
    v = str(obj.get("verdict", "belirsiz")).strip().lower()
    out["verdict"] = v if v in ("gerçek", "gercek", "şüpheli", "supheli", "sahte", "belirsiz") else "belirsiz"
    try:
        out["confidence"] = max(0, min(100, int(obj.get("confidence") or 0)))
    except Exception:
        out["confidence"] = 0
    out["reasoning_tr"] = str(obj.get("reasoning_tr") or "")[:2000]
    cf = obj.get("corrected_fields") or {}
    out["corrected_fields"] = {str(k): str(vv)[:120] for k, vv in cf.items()} if isinstance(cf, dict) else {}
    fr = obj.get("finding_reviews") or []
    out["finding_reviews"] = [
        {"code": str(x.get("code", "")), "gercek_mi": bool(x.get("gercek_mi", True)),
         "aciklama": str(x.get("aciklama", ""))[:500]}
        for x in fr if isinstance(x, dict)][:20]
    out["verify_suggestion"] = str(obj.get("verify_suggestion") or "")[:500]
    # İŞLEM KANALI (EFT/FAST/HAVALE) — EFT anında geçmez (riskli). YZ'nin belirlediği kanal korunur.
    ik = obj.get("islem_kanali")
    if isinstance(ik, dict):
        _k = str(ik.get("kanal", "belirsiz")).strip().upper()
        _k = _k if _k in ("EFT", "FAST", "HAVALE") else "belirsiz"
        _ag = ik.get("aninda_gecer")
        out["islem_kanali"] = {
            "kanal": _k,
            "aninda_gecer": (False if _k == "EFT" else (True if _k in ("FAST", "HAVALE") else (bool(_ag) if isinstance(_ag, bool) else None))),
            "kanit": str(ik.get("kanit", ""))[:300],
        }
    # GÖRSEL TAHRİFAT: YZ'nin görüntüden tespit ettiği font/hizalama/montaj uyuşmazlıkları
    # (ör. yazıyla tutar belgenin genel fontundan farklı → yapıştırılmış). Bulguya dönüştürülür.
    gt = obj.get("gorsel_tahrifat") or []
    out["gorsel_tahrifat"] = [
        {"alan": str(x.get("alan", ""))[:80], "aciklama": str(x.get("aciklama", ""))[:400],
         "guven": max(0, min(100, int(x.get("guven") or 0))) if str(x.get("guven") or "").strip().isdigit() else 0}
        for x in gt if isinstance(x, dict)][:10]
    im = obj.get("improvement_notes") or []
    out["improvement_notes"] = [
        {"bank": str(x.get("bank", "")), "field": str(x.get("field", "")),
         "problem": str(x.get("problem", ""))[:300], "suggestion": str(x.get("suggestion", ""))[:300],
         "label_hint": str(x.get("label_hint", ""))[:60]}
        for x in im if isinstance(x, dict)][:20]
    return out


def learn_from(adjudication: dict, bank_key: str) -> list:
    """YZ'nin ürettiği improvement_notes'lardan MAKİNE-UYGULANABİLİR ipuçlarını (bank+field+label_hint)
    kalıcı store'a yazar → 'öğren' adımı. Döndürür: öğrenilen (field,label) listesi.
    Güvenli: yalnız hem alanı DÜZELTMİŞ (corrected_fields) hem label_hint vermiş notlar öğrenilir."""
    if not adjudication:
        return []
    learned = []
    try:
        import store as _store
    except Exception:
        return []
    corrected = set((adjudication.get("corrected_fields") or {}).keys())
    for note in (adjudication.get("improvement_notes") or []):
        field = (note.get("field") or "").strip()
        label = (note.get("label_hint") or "").strip()
        # Banka anahtarı OTORİTER olarak çağırandan (registry bank_key) gelir; YZ'nin serbest
        # metin 'bank' değeri yalnız yedektir (tutarsız olabilir).
        bank = (bank_key or note.get("bank") or "").strip().lower()
        # Yalnız YZ'nin GERÇEKTEN doğru okuyup düzelttiği alan için, ve bir etiket ipucu varsa öğren.
        if field and label and (field in corrected) and bank:
            try:
                if _store.record_field_hint(bank, field, label):
                    learned.append((field, label))
            except Exception:
                pass
    return learned


def apply_corrections(extraction: dict, adjudication: dict, hard_proof_codes=None) -> dict:
    """YZ'nin GÖRÜNTÜDEN düzelttiği alanları çıkarım dict'ine EK olarak işler. Deterministik KESİN
    bulguların dokunduğu alanlar korunur (YZ ezmesin). Yeni bir dict döndürür; orijinali bozmaz.
    Doğrulama: yalnız GEÇERLİ (ör. IBAN mod-97) düzeltmeler uygulanır."""
    import copy
    import banks as _b
    ex = copy.deepcopy(extraction or {})
    corr = (adjudication or {}).get("corrected_fields") or {}
    applied = {}
    for dotted, val in corr.items():
        if not val:
            continue
        # IBAN düzeltmesi yalnız mod-97 GEÇERLİYSE uygulanır (YZ yanlış IBAN uydurmasın)
        if dotted.endswith(".iban"):
            n = _b.normalize_iban(val)
            # YZ yanlış IBAN uydurmasın: yalnız mod-97 KESİN GEÇERLİ (True) düzeltme uygulanır.
            if _b.iban_valid(n) is not True:
                continue
            val = n
        # Sayısal alanlar (tutar/ücret/toplam) YZ'den metin gelebilir → float'a çevir; olmuyorsa atla.
        if dotted in ("amount.value", "amount.fee", "amount.total"):
            try:
                s = str(val).replace(" ", "").replace("TL", "").replace("TRY", "")
                if "," in s and "." in s:
                    s = s.replace(".", "").replace(",", ".")
                elif "," in s:
                    s = s.replace(",", ".")
                val = float(s)
            except Exception:
                continue
        parts = dotted.split(".")
        cur = ex
        ok = True
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                ok = False
                break
            cur = cur[p]
        # AI OTORİTESİ (kullanıcı kuralı): OCR'ın okuduğu TÜM alanlar AI ile yeniden doğrulanır; AI'ın
        # GÖRÜNTÜDEN okuduğu değer alan BOŞ da olsa, DOLU ama YANLIŞ da olsa YAZILIR. (IBAN yukarıda mod-97
        # ile doğrulandı; tutarlar float'a çevrildi → uydurma engellenir.) 'bank_stated' KORUNUR: dekonttaki
        # YAZILI banka adıdır, banka-adı↔IBAN-kodu çelişki kontrolü buna dayanır, AI ezmemeli.
        if ok and parts[-1] != "bank_stated" and cur.get(parts[-1]) != val:
            cur[parts[-1]] = val
            applied[dotted] = val
    ex["_ai_applied_corrections"] = applied
    return ex

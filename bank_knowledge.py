"""
Banka-davranış bilgi tabanı / Per-bank receipt-behavior knowledge base.

AMAÇ: Her incelediğimiz dekonttan öğrendiğimiz BANKA-ÖZEL davranışları (dekont düzeni,
ücret tarifesi, kanal-etiketleme alışkanlığı, kimlik alanları, bilinen sahtecilik tell'leri,
sorgulanabilir tanımlayıcılar) tek yerde biriktirir. Böylece:
  1) 'Bir bankada bulduğumuzu her bankaya aynen uygulama' ilkesi yapıya gömülür — her banka
     kendi bağlamında değerlendirilir.
  2) YZ Değerlendirici (ai_adjudicator.py) bu bilgiyi BAĞLAM olarak alır ve bir dekontu
     'bu bankanın bilinen davranışı ışığında' yorumlar.

Bu dosya KURAL motoru DEĞİLDİR (skor üretmez); yalnızca insan+YZ için yapılandırılmış bilgidir.
Yeni bir banka/dekont öğrenildikçe buraya EKLENİR. `notes_md()` okunur markdown üretir.

KAYNAK: Bu notların çoğu gerçek dekont örneklerinden (bu projede incelenen) çıkarılmıştır;
kesin tarife/limit rakamları zamanla değişebilir — 'as_of' alanına bakın ve şüphede banka
teyidini esas alın.
"""
from __future__ import annotations

# =====================================================================
#  KALICI METODOLOJİ İLKESİ (kullanıcı kuralı — bir daha yazmaya gerek yok):
#  Bir dekont YALNIZCA KENDİ BANKASININ dekontlarıyla karşılaştırılır. A bankasının dekontu
#  A bankasının diğer dekontları/normuyla; B bankası B ile değerlendirilir. Bankalar ARASI
#  kıyas yapılmaz. Kanal (EFT/FAST/HAVALE), dekontun KENDİ fatura etiketinden (EFT/FAST TUTARI/
#  ÜCRETİ) belirlenir; başka bir bankanın alışkanlığı gerekçe yapılmaz. Tüm denetimler ve
#  geliştirmeler banka bazlıdır ve bu bankanın altında (bank_knowledge / reference_profiles /
#  bank_corpus) tutulur.
# =====================================================================
METHODOLOGY = (
    "Her dekont yalnız kendi bankasının dekontlarıyla/normuyla karşılaştırılır; bankalar arası "
    "kıyas yapılmaz. Kanal, dekontun kendi fatura etiketinden belirlenir. Tüm kurallar banka bazlıdır."
)

# Türkiye FAST işlem-başına üst limiti (tarihe göre değişir; kullanıcı 2026-08 için bildirdi).
FAST_LIMIT_TL = 100_000
FAST_LIMIT_AS_OF = "2026-08"

# Düşük ücretlerde FAST/EFT/HAVALE tarifeleri neredeyse aynıdır → ücretle kanal ayırt edilemez.
FEE_RAIL_INDISTINGUISHABLE_BELOW_TL = 12.0

# Genel kanal (rail) mantığı — bankadan bağımsız temel kurallar.
GENERAL_RAIL_LOGIC = [
    "HAVALE = aynı banka içindeki iki hesap arası transfer (banka-içi).",
    "EFT = FARKLI bankalar arası transfer; kesim saatleri vardır, geç saatte ertesi iş gününe valörlenir ('geç EFT').",
    "FAST = FARKLI bankalar arası ANLIK 7/24 transfer; işlem-başına üst limiti vardır (bkz. FAST_LIMIT_TL).",
    f"Gönderici ve alıcı IBAN AYNI banka kodundaysa işlem HAVALE'dir; 'bankalararası/EFT/FAST' iddiası ÇELİŞKİDİR (sahtecilik).",
    f"Tutar o tarihteki FAST limitini AŞIYORSA işlem FAST OLAMAZ → EFT'dir (mantıksal dışlama).",
    f"Ücret {FEE_RAIL_INDISTINGUISHABLE_BELOW_TL:g} TL'nin altındaysa FAST/EFT/HAVALE ücretle AYIRT EDİLEMEZ.",
    "Başlık/işlem-türü etiketi bankaya göre yanıltıcı olabilir (aşağıdaki banka-özel notlara bak); "
    "kesin kanal için ÜCRET KALEMİ etiketi + IBAN banka kodları + işlem/sorgu numarasıyla banka teyidi esastır.",
]

# ---------------------------------------------------------------------------
#  BANKA-ÖZEL BİLGİ
#  Her kayıt: key -> {label, layout, rail_labeling, fees, identity, identifiers, tells, notes}
# ---------------------------------------------------------------------------
BANK_KNOWLEDGE = {
    "akbank": {
        "label": "Akbank T.A.Ş.",
        "layout": "İki sütun: GÖNDERİCİ BİLGİLERİ | ALICI BİLGİLERİ. 'Etiket : Değer'. Şube '7777 - "
                  "AKBANK DİREKT MOBİL CEP'. Alt bilgi: Mersis 0015001526400497, Vergi No 0150015264.",
        "rail_labeling": "BAŞLIK 'EFT BANKALAR ARASI HESABA HAVALE' hem EFT hem FAST için kullanılan "
                         "GENEL bir şablondur; tek başına EFT/FAST ayırt ETMEZ. Ücret kaleminde "
                         "'GEC EFT/GECEFT/EFT' geçerse KESİN EFT. Banka-İÇİ transfer ise farklı başlık "
                         "taşır: 'ÖDEME EMİRLERİ GİRİŞİ' (+ 'HVL ÜCRETİ' kalemi).",
        "fees": {"eft_fast_interbank": "~16,76 TL (KOMİSYON 15,96 + BSMV 0,80)",
                 "havale_intrabank": "~8,38 TL (HVL ÜCRETİ 7,98 + BSMV 0,40)",
                 "as_of": "2026-08 (gerçek dekont örnekleri)"},
        "identity": "Bireysel işlemde 'VKN/Vergi Dairesi' alanı ile 'İşlemi Yapan TCKN' AYNI kişinin "
                    "TCKN'sini birebir taşır. Farklıysa ya da biri kontrol basamağını geçemiyorsa "
                    "SAHTECİLİK.",
        "identifiers": ["Alt satır 'müşteriNo / sıraNo /' (ör. '48283362 / 301931 /')",
                        "QR karekod: dekont doğrulama kodu (14 hane, 'O…' formatı)",
                        "'Referans' alanı gerçek dekontlarda da BOŞ olabilir — tek başına şüphe değil."],
        "tells": ["Aynı-banka IBAN'lar + 'EFT BANKALAR ARASI' başlığı = SAMEBANK_RAIL_CONTRADICTION.",
                  "VKN alanı ≠ İşlemi Yapan TCKN, ya da biri checksum-geçersiz = ID_FIELD_MISMATCH.",
                  "Türü değiştirip ücreti unutmak: EFT etiketi ama 8,38 (havale) ücreti — dikkat."],
        "notes": "Saat 21:42 gibi geç saatte 'GEC EFT' = kesim sonrası EFT (normal). QR/sıra no ile "
                 "banka teyidi kanalı kesinleştirir.",
    },
    "yapikredi": {
        "label": "Yapı ve Kredi Bankası",
        "layout": "'Bilgi Dekontu' / 'e-Dekont'. İki sütun. Alanlar 'ETİKET : değer'. Web yapikredi.com.tr, "
                  "Mersis 0937002089200741.",
        "rail_labeling": "ÇOK ÖNEMLİ: 'DEKONT TİPİ' alanına FAST işleminde bile çoğu zaman 'EFT' yazar "
                         "(genel etiketleme). GERÇEK kanal BAŞLIKTAN/TUTAR kaleminden gelir: 'FAST "
                         "GÖNDERİMİ' ya da 'GİDEN/GELEN FAST TUTARI' varsa işlem FAST'tir. Açıklama "
                         "satırı da 'ELEKTRONİK FON TRANSFERİ (EFT) ÜCRETİ - FAST/' gibi ikisini karıştırır. "
                         "'HESAPTAN HESABA HAVALE' ise banka-içi havaledir.",
        "fees": {"fast_eft_interbank": "~16,76 TL (KOMİSYON 15,96 + VERGİ 0,80)",
                 "havale": "düşük tarife (~8,3x)", "as_of": "2026-08"},
        "identity": "TCKN alanı genelde maskeli (***********).",
        "identifiers": ["'SORGU NO' = FAST işlem sorgu/teyit numarası (banka doğrulaması için KRİTİK).",
                        "'İŞLEM REF', 'SIRA NO/ID', 'BELGE NUMARASI' ek referanslar."],
        "tells": ["Kanal için 'DEKONT TİPİ'ye GÜVENME; başlık 'FAST GÖNDERİMİ' + 'GİDEN FAST TUTARI' esas alınır."],
        "notes": "Sistemde YKB dalı 'FAST GÖNDERİMİ/GİDEN FAST TUTARI' görürse türü FAST'a çeker ve "
                 "SORGU NO'yu sequence olarak yakalar.",
    },
    "deniz": {
        "label": "DenizBank",
        "layout": "İki sütun (Müşteri Bilgisi | İşlem Bilgisi) ve alanlar İKİ-NOKTASIZ: 'Adı Soyadı "
                  "SÜHEYL ŞEN   İşlem Türü Giden FAST'. Kanal '9300 - MobilDeniz'. Genel çıkarıcı bu "
                  "kolonsuz düzende ad/tutar/tür kaçırır → banka-özel dal gerekir.",
        "rail_labeling": "'İşlem Türü' alanı NET yazar: 'Giden FAST' → FAST. EFT/Havale benzer.",
        "fees": {"fast_interbank": "~16,76 TL (Masraf 15,96 + BSMV 0,80)",
                 "note": "Özet satır 'Masraf 16,76 TL' toplamdır; 'Masraf : 15,96' DETAY satırıdır "
                         "(ikisini karıştırma).", "as_of": "2026-08"},
        "identity": "'VKN / TCKN' maskeli (ör. '803063****/1258807****').",
        "identifiers": ["'FAST Sorgu Numarası' UZUN (18 hane, ör. 612949293097369600) = teyit no.",
                        "'Referans Bilgisi : 19082026 - 9300 - 18030687'."],
        "tells": ["Alıcı bankası 'Alıcı Banka 0046-AKBANK...' baştaki kod IBAN'la tutarlı olmalı."],
        "notes": "Denizbank İHRAÇÇI (gönderen); alıcı bankası alıcı IBAN'ından gelir — Deniz'i alıcı "
                 "bankası SANMA (yanlış RECEIVER_BANK_MISMATCH önlenir). IBAN kodu 00134.",
    },
    "alternatif": {
        "label": "Alternatifbank (ABank)",
        "layout": "'Dekont'. SAYIN/MÜŞTERİ ADI (gönderen), 'IBAN NUMARASI' (gönderen), İŞLEM TÜRÜ, alt "
                  "blokta Alıcı Banka/Alıcı Adı/Alıcı IBAN, Tutar/İşlem Ücreti. Web alternatifbank.com.tr, "
                  "Mersis 0060003154500048. NOT: Aktifbank'tan (Aktif Yatırım Bankası) FARKLI kurumdur.",
        "rail_labeling": "'İŞLEM TÜRÜ' net: 'Giden FAST Ödemesi' → FAST.",
        "fees": {"fast": "örnekte 0,00 (küçük tutar)", "as_of": "2026-08"},
        "identity": "'VKN/TCKN' maskeli (ör. /3008926****).",
        "identifiers": ["'FAST Sorgu Numarası' basar (ör. 12985127) = teyit no."],
        "tells": [],
        "notes": "IBAN kodu 00124. İHRAÇÇI gönderen; alıcı bankası alıcı IBAN'ından.",
    },
    "ziraat": {
        "label": "T.C. Ziraat Bankası",
        "layout": "Ziraat Mobil / Süper Şube. 'Hesaptan Hesaba Havale' / FAST / EFT dekontları.",
        "rail_labeling": "İşlem türü başlıkta/etikette. 'Hesaptan FAST', 'Hesaptan EFT', 'Hesaba Havale'.",
        "fees": {"as_of": "2026-08", "note": "Tarife dekont üstünden okunur."},
        "identity": "Boş 'VERGİ KİMLİK NO' alanına adres rakamlarının sızmasına dikkat (banka dalında "
                    "temizlenir).",
        "identifiers": [],
        "tells": ["Şube 'SAYIN' bleed; İşlem Türü 'Dekont' yerine gerçek türü göstermeli (düzeltildi)."],
        "notes": "IBAN kodları 00010/00160/00209.",
    },
    "garanti": {
        "label": "Garanti BBVA",
        "layout": "'HESAPTAN HESABA' vb. Skia üreticisi (kabul edilir).",
        "rail_labeling": "İşlem türü etikette.",
        "fees": {"as_of": "2026-08"},
        "identity": "",
        "identifiers": ["'SIRA NO' gömülü tam zaman damgası taşır (YYYY-MM-DD-HH.MM.SS) = işlemin ANI; "
                        "'DÜZENLENME TARİHİ' = belge oluşturma anı. Oluşturma işlemden SONRA olması "
                        "NORMAL; ÖNCE olması geriye-tarihleme (sahtecilik)."],
        "tells": ["SIRA NO ↔ DÜZENLENME zaman tutarlılığı (check_seq_vs_creation)."],
        "notes": "IBAN kodu 00062.",
    },
    "isbank": {
        "label": "Türkiye İş Bankası",
        "layout": "e-Dekont, 'Doküman Numarası'. isbank.com.",
        "rail_labeling": "İş Bankası FAST'i 'DEKONT/EFT' tipiyle basabilir — kanalı ücret/başlıktan doğrula.",
        "fees": {"fast": "~16,76", "havale": "~8,38", "as_of": "2026-08"},
        "identity": "",
        "identifiers": [],
        "tells": [],
        "notes": "IBAN kodu 00064.",
    },
    "enpara": {
        "label": "Enpara Bank",
        "layout": "'ALICI ÜNVANI' / 'MÜŞTERİ ÜNVANI' iki blok. Fiş No = YYYYAAGG + sayaç.",
        "rail_labeling": "KANAL = FATURA ETİKETİ. Enpara işlemi 'EFT (FAST)' / 'GİDEN FAST EFT' diye "
                         "gösterse de, tutar/ücreti 'EFT TUTARI' ve 'EFT ÜCRETİ' diye FATURALAR → işlem "
                         "EFT'dir. '(FAST)' teslim rayıdır (FAST = anlık EFT altyapısı), kanalı değiştirmez. "
                         "Yalnız 'FAST Ücreti / FAST TUTARI' faturalaması varsa FAST olur.",
        "fees": {"as_of": "2026-08"},
        "identity": "",
        "identifiers": ["Fiş No gömülü tarih (YYYYAAGG) işlem tarihiyle uyumlu olmalı (dijital PDF'te).",
                        "SORGU NO = işlem teyit numarası."],
        "tells": ["Alıcı/gönderici IBAN karışması geçmişte sorundu (ocr_recover ile düzeltildi).",
                  "Alıcı adı 'ALICI ÜNVANI' + açıklamadaki '<ad>, Bireysel Ödeme' ile teyit edilir (yedek)."],
        "notes": "IBAN kodları 00157/00111 (QNB markası). '(FAST)' etiketi işlemi FAST yapmaz; fatura EFT ise EFT. "
                 "BANKA-İÇİ NORM (13 gerçek dekont): interbank Enpara işlemlerinin TAMAMI EFT; aynı banka kodu "
                 "(00157→00157) HAVALE. Fiş No ilk 8 hane = işlem tarihi (YYYYAAGG). GERÇEK üretici 'iText/Ibtech'; "
                 "SAHTELER pdfium/Skia (tarayıcıdan 'yazdır→PDF' ile yeniden üretilmiş) + fiş-tarih çelişkisi.",
    },
    "qnb": {"label": "QNB Bank A.Ş.", "layout": "", "rail_labeling": "", "fees": {"as_of": "2026-08"},
            "identity": "", "identifiers": [], "tells": [], "notes": "IBAN kodu 00111. Fiş No YYYYAAGG."},
    "teb": {"label": "Türk Ekonomi Bankası (TEB)", "layout": "'Bankalararası Para Transfer Dekontu' "
            "(CEPTETEB). Hesap Sahibi/Alacaklı Adı.", "rail_labeling": "Bankalararası = FAST.",
            "fees": {"as_of": "2026-08"}, "identity": "", "identifiers": ["FAST No."],
            "tells": ["TEB'i alıcı bankası sanma → RECEIVER_BANK_MISMATCH FP (düzeltildi)."],
            "notes": "IBAN kodu 00032."},
    "getir": {"label": "GetirFinans (Fibabanka)", "layout": "E-Dekont. ALAN MÜŞTERİ (alıcı), GÖNDEREN "
              "MÜŞTERİ, ALICI IBAN NO, '(-)' borç satırı IBAN'ı = gönderen.", "rail_labeling": "HAVALE/FAST.",
              "fees": {"as_of": "2026-08"}, "identity": "", "identifiers": [],
              "tells": ["Alıcı/gönderen IBAN ayrımı: ALICI IBAN NO=alıcı, (-) satırı=gönderen."],
              "notes": "IBAN kodu 00103 (Fibabanka)."},
    "halk": {"label": "Türkiye Halk Bankası", "layout": "", "rail_labeling": "", "fees": {"as_of": "2026-08"},
             "identity": "", "identifiers": [], "tells": [], "notes": "IBAN kodu 00012."},
    "vakif": {"label": "VakıfBank", "layout": "'Şubesiz Bankacılık'. İki sütunlu, İKİ-NOKTASIZ "
              "'İŞLEM BİLGİLERİ' bloğu: İŞLEM TÜRÜ (ör. 'FAST Giden Anlık Ödeme'), SORGU NO, İŞLEM "
              "TUTARI, MASRAF TUTARI, İŞLEM NO. Web vakifbank.com.tr.",
              "rail_labeling": "'FAST Giden Anlık Ödeme' → FAST. Kolonsuz yakalanır.",
              "fees": {"fast": "16,76 TL örneği", "as_of": "2026-08"},
              "identity": "", "identifiers": ["SORGU NO = FAST teyit no; İŞLEM NO 16 hane."],
              "tells": ["GERÇEK VakıfBank dekontlarında MASRAF TUTARI DAİMA 'TL' ile yazılır "
                        "(ör. '16,76 TL'). Masrafta 'TL' YOKSA gerçek şablondan sapmadır → "
                        "AMOUNT_CURRENCY_INCONSISTENT (masraf sonradan eklenmiş/değiştirilmiş olabilir)."],
              "notes": "IBAN kodları 00015/00210. Gönderen IBAN dekontta bazen yer almaz."},
    "ptt": {"label": "PTT (PttBank)", "layout": "", "rail_labeling": "", "fees": {"as_of": "2026-08"},
            "identity": "", "identifiers": [], "tells": [], "notes": "IBAN kodu 00807."},
    "kuveyt": {"label": "Kuveyt Türk Katılım",
               "layout": "'IBAN'a Para Transferi (Giden)' e-Dekont. İki blok: sol (Şube/Müşteri No/TCKN/"
                         "İşlem Ref/Düzenleyen) — sağ (Belge No/Belge Tarihi/ETTN/Senaryo/Tip). Alt blok: "
                         "Gönderen Kişi, Alıcı, Gönderilen IBAN (=alıcı IBAN), Alıcı Banka, İşlem Yeri, Açıklama, Tutar.",
               "rail_labeling": "KANAL = Açıklama'daki '(FAST)'/'(EFT)'. 'Senaryo/Tip: DEKONT/EFT' GENEL bir "
                                "e-dekont etiketidir, tek başına EFT demek DEĞİLDİR. Açıklama '(FAST)' ise FAST.",
               "fees": {"as_of": "2026-08"},
               "identity": "TCKN maskeli (26*******62).",
               "identifiers": ["Belge No 'BZG...' + İşlem Ref 'AMNAK-B-YYYYMMDD...' + ETTN (UUID)."],
               "tells": ["Gönderen IBAN dekontta BASILMAZ (yalnız 'Gönderilen IBAN' = alıcı) — eksiklik sahtecilik değil."],
               "notes": "IBAN kodu 00205. Katılım bankası. Web kuveytturk.com.tr, Mersis 0600002681400074."},
    "ing": {"label": "ING Bank A.Ş.", "layout": "", "rail_labeling": "", "fees": {"as_of": "2026-08"},
            "identity": "", "identifiers": [], "tells": [], "notes": "IBAN kodu 00099."},
    "fiba": {"label": "Fibabanka A.Ş.", "layout": "", "rail_labeling": "", "fees": {"as_of": "2026-08"},
             "identity": "", "identifiers": [], "tells": [], "notes": "IBAN kodu 00103."},
}


def get(bank_key: str) -> dict | None:
    """Banka anahtarına göre bilgi kaydını döndürür (yoksa None)."""
    return BANK_KNOWLEDGE.get((bank_key or "").strip().lower())


def context_for(bank_key: str) -> str:
    """YZ değerlendiriciye verilecek KISA, düz-metin banka bağlamı üretir (genel kurallar +
    varsa banka-özel notlar). Bilgi yoksa yalnız genel kuralları döndürür."""
    lines = ["GENEL KANAL/ÜCRET KURALLARI:"]
    lines += [f"- {r}" for r in GENERAL_RAIL_LOGIC]
    lines.append(f"- FAST işlem-başı üst limit: {FAST_LIMIT_TL:,.0f} TL (geçerlilik {FAST_LIMIT_AS_OF}).")
    k = get(bank_key)
    if k:
        lines.append("")
        lines.append(f"BANKA-ÖZEL NOTLAR — {k.get('label', bank_key)}:")
        if k.get("layout"):
            lines.append(f"- Düzen: {k['layout']}")
        if k.get("rail_labeling"):
            lines.append(f"- Kanal etiketleme: {k['rail_labeling']}")
        if k.get("fees"):
            _f = "; ".join(f"{kk}={vv}" for kk, vv in k["fees"].items())
            lines.append(f"- Ücret tarifesi: {_f}")
        if k.get("identity"):
            lines.append(f"- Kimlik alanları: {k['identity']}")
        for idf in (k.get("identifiers") or []):
            lines.append(f"- Tanımlayıcı: {idf}")
        for t in (k.get("tells") or []):
            lines.append(f"- Sahtecilik tell: {t}")
        if k.get("notes"):
            lines.append(f"- Not: {k['notes']}")
    return "\n".join(lines)


def notes_md() -> str:
    """İnsan-okur markdown bilgi tabanı üretir (BANKA_DAVRANIS_NOTLARI.md için)."""
    out = ["# Banka Davranış Notları (Bilgi Tabanı)",
           "",
           "Bu belge, incelenen gerçek dekontlardan öğrenilen **banka-özel** davranışları biriktirir. "
           "Kural motoru ve YZ Değerlendirici (ai_adjudicator) bunu bağlam olarak kullanır. "
           "Yeni banka/dekont öğrenildikçe `bank_knowledge.py` güncellenir ve bu belge yeniden üretilir.",
           "",
           f"- FAST işlem-başı üst limit: **{FAST_LIMIT_TL:,.0f} TL** (geçerlilik {FAST_LIMIT_AS_OF})",
           f"- Ücretle kanal ayırt-edilemezlik eşiği: **{FEE_RAIL_INDISTINGUISHABLE_BELOW_TL:g} TL** altı",
           "",
           "## Genel kanal/ücret kuralları", ""]
    out += [f"- {r}" for r in GENERAL_RAIL_LOGIC]
    out.append("")
    out.append("## Banka bazında")
    for key, k in BANK_KNOWLEDGE.items():
        out.append("")
        out.append(f"### {k.get('label', key)}  (`{key}`)")
        if k.get("layout"):
            out.append(f"- **Düzen:** {k['layout']}")
        if k.get("rail_labeling"):
            out.append(f"- **Kanal etiketleme:** {k['rail_labeling']}")
        if k.get("fees"):
            out.append("- **Ücret:** " + "; ".join(f"{kk}: {vv}" for kk, vv in k["fees"].items()))
        if k.get("identity"):
            out.append(f"- **Kimlik:** {k['identity']}")
        if k.get("identifiers"):
            out.append("- **Tanımlayıcılar:** " + "; ".join(k["identifiers"]))
        if k.get("tells"):
            out.append("- **Sahtecilik tell'leri:** " + "; ".join(k["tells"]))
        if k.get("notes"):
            out.append(f"- **Not:** {k['notes']}")
    out.append("")
    return "\n".join(out)

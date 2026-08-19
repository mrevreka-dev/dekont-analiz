"""
Türk bankaları: IBAN banka kodları ve isim eşleştirmeleri.
Turkish banks: IBAN bank codes and name matching.
"""
from __future__ import annotations
import re

# IBAN banka kodu -> banka adı.
# Kaynak: TCMB Ödeme Sistemleri Katılımcıları (resmi liste). IBAN'daki 5 haneli kod,
# TCMB'nin 4 haneli EFT kodunun başına '0' eklenmiş halidir (ör. EFT 0064 -> IBAN 00064).
IBAN_BANK_CODES = {
    "00001": "Türkiye Cumhuriyet Merkez Bankası",
    "00004": "İller Bankası",
    "00010": "T.C. Ziraat Bankası",
    "00012": "Türkiye Halk Bankası",
    "00014": "Türkiye Sınai Kalkınma Bankası (TSKB)",
    "00015": "VakıfBank",
    "00016": "Türk Eximbank",
    "00017": "Türkiye Kalkınma ve Yatırım Bankası",
    "00029": "Birleşik Fon Bankası",
    "00032": "Türk Ekonomi Bankası (TEB)",
    "00046": "Akbank",
    "00059": "Şekerbank",
    "00060": "Türk Ticaret Bankası",
    "00062": "Garanti BBVA",
    "00064": "Türkiye İş Bankası",
    "00067": "Yapı ve Kredi Bankası",
    "00091": "Arap Türk Bankası (A&T Bank)",
    "00092": "Citibank",
    "00096": "Turkish Bank",
    "00098": "JPMorgan Chase Bank",
    "00099": "ING Bank",
    "00103": "Fibabanka",
    "00108": "Turkland Bank (T-Bank)",
    "00109": "ICBC Turkey Bank",
    "00111": "QNB Finansbank",
    "00115": "Deutsche Bank",
    "00116": "Pasha Yatırım Bankası",
    "00121": "Standard Chartered Yatırım Bankası",
    "00122": "Société Générale",
    "00123": "HSBC Bank",
    "00124": "Alternatifbank (ABank)",
    "00125": "Burgan Bank",
    "00129": "Bank of America Yatırım Bank",
    "00132": "İstanbul Takas ve Saklama Bankası (Takasbank)",
    "00134": "DenizBank",
    "00135": "Anadolubank",
    "00137": "Rabobank",
    "00138": "Diler Yatırım Bankası",
    "00139": "GSD Yatırım Bankası",
    "00141": "Nurol Yatırım Bankası",
    "00142": "Bankpozitif",
    "00143": "Aktif Yatırım Bankası",
    "00146": "Odea Bank",
    "00147": "MUFG Bank Turkey",
    "00148": "Intesa Sanpaolo",
    "00149": "Bank of China Turkey",
    "00150": "Golden Global Yatırım Bankası",
    "00151": "D Yatırım Bankası",
    "00152": "Destek Yatırım Bankası",
    "00153": "Misyon Yatırım Bankası",
    "00154": "Tera Yatırım Bankası",
    "00155": "Q Yatırım Bankası",
    "00156": "Hedef Yatırım Bankası",
    "00157": "Enpara Bank",
    "00158": "Colendi Bank",
    "00159": "Fups Bank",
    "00160": "Ziraat Dinamik Banka",
    "00161": "Aytemiz Yatırım Bankası",
    "00203": "Albaraka Türk Katılım",
    "00205": "Kuveyt Türk Katılım",
    "00206": "Türkiye Finans Katılım",
    "00209": "Ziraat Katılım",
    "00210": "Vakıf Katılım",
    "00211": "Türkiye Emlak Katılım",
    "00212": "Hayat Finans Katılım",
    "00213": "T.O.M. Katılım Bankası",
    "00214": "Dünya Katılım Bankası",
    "00806": "Merkezi Kayıt Kuruluşu",
    "00807": "PTT (Posta ve Telgraf Teşkilatı)",
}

# Metin içi anahtar kelimeler -> banka adı
NAME_KEYWORDS = [
    (r"garanti", "Garanti BBVA"),
    (r"is ?bank|işbank|isbank\.com|i̇ş bankas|iş bankas", "Türkiye İş Bankası"),
    (r"ziraat", "T.C. Ziraat Bankası"),
    (r"yap[ıi] ?kredi|yapikredi", "Yapı ve Kredi Bankası"),
    (r"akbank", "Akbank"),
    (r"halkbank|halk bankas", "Türkiye Halk Bankası"),
    (r"vak[ıi]fbank|vak[ıi]f bankas", "VakıfBank"),
    (r"denizbank", "DenizBank"),
    (r"finansbank|qnb", "QNB Finansbank"),
    (r"\bteb\b|türk ekonomi", "Türk Ekonomi Bankası (TEB)"),
    (r"enpara", "Enpara Bank"),
    (r"kuveyt türk", "Kuveyt Türk Katılım"),
    (r"albaraka", "Albaraka Türk Katılım"),
    (r"ing bank|\bing\b", "ING Bank"),
    (r"papara", "Papara"),
    (r"ziraat kat", "Ziraat Katılım"),
]


def normalize_iban(raw: str) -> str:
    s = re.sub(r"\s+", "", raw or "").upper()
    return s


def iban_bank_code(iban: str) -> str:
    """IBAN'dan 5 haneli banka kodunu döndürür (ör. 'TR64 0006 4...' -> '00064')."""
    s = normalize_iban(iban)
    m = re.match(r"TR\d{2}(\d{5})", s)
    return m.group(1) if m else ""


def iban_valid(iban: str) -> bool | None:
    """Türk IBAN'ının biçim + mod-97 kontrol basamağını doğrular.
    Döner: True (geçerli), False (geçersiz/tahrif), None (TR IBAN kalıbı yok -> kontrol edilemez).
    Kural (ISO 13616): ilk 4 karakter (TRkk) sona alınır, harfler sayıya çevrilir (T=29, R=27),
    97'ye bölümünden kalan 1 olmalıdır."""
    s = normalize_iban(iban)
    if not re.fullmatch(r"TR\d{24}", s):
        return None                                # TR IBAN değil -> bu kontrol uygulanmaz
    rearr = s[4:] + s[:4]
    digits = ""
    for ch in rearr:
        digits += ch if ch.isdigit() else str(ord(ch) - 55)   # A=10 ... T=29, R=27
    try:
        return int(digits) % 97 == 1
    except ValueError:
        return None


# OCR'da sıkça karışan rakam çiftleri (görsel benzerlik). Tek yönlü değil — çift yönlü.
_OCR_CONFUSABLE = {
    "0": ["8", "6"], "8": ["0", "6", "3"], "6": ["8", "5", "0"], "5": ["6", "8"],
    "1": ["7"], "7": ["1"], "3": ["8", "9"], "9": ["4"], "2": ["7"], "4": ["9"],
}


def repair_iban_ocr(iban: str) -> str:
    """OCR'ın YANLIŞ OKUDUĞU bir IBAN'ı (mod-97 tutmuyorsa) görsel-karışan rakamları tek tek
    deneyerek onarır. YALNIZCA sonuç BENZERSİZSE (tam olarak 1 geçerli aday) uygular — birden
    çok ya da hiç geçerli aday varsa TAHMİN YÜRÜTMEZ, orijinali döndürür. Böylece 'TR..218058'
    gibi tek-basamak hatası güvenle 'TR..218056'ya düzelir; sahte IBAN üretme riski olmaz.
    Döner: onarılmış IBAN (benzersizse) ya da girdinin normalize hâli."""
    s = normalize_iban(iban)
    if not re.fullmatch(r"TR\d{24}", s) or iban_valid(s) is True:
        return s                                   # kalıp yok ya da zaten geçerli
    body = list(s)
    found = set()
    # Onarılabilir pozisyonlar: kontrol basamakları (2-3) + HESAP GÖVDESİ (9+).
    # BANKA KODU (4-8) KORUNUR — onu değiştirmek alıcının BANKASINI değiştirir; asla tahmin etme.
    _positions = [2, 3] + list(range(9, len(body)))
    for i in _positions:
        d = body[i]
        for alt in _OCR_CONFUSABLE.get(d, ()):
            cand = body[:]; cand[i] = alt
            cs = "".join(cand)
            if iban_valid(cs) is True:
                found.add(cs)
    return next(iter(found)) if len(found) == 1 else s


def bank_from_iban(iban: str) -> str:
    """IBAN banka kodundan banka adını döndürür; bilinmiyorsa boş."""
    code = iban_bank_code(iban)
    return IBAN_BANK_CODES.get(code, "") if code else ""


def _only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def tckn_valid(s: str) -> bool | None:
    """11 haneli TC Kimlik No sağlaması (resmi algoritma).
    Döner: True/False; girdi 11 rakam değilse None (uygulanamaz — ör. maskeli)."""
    d = _only_digits(s)
    if len(d) != 11:
        return None
    if d[0] == "0":
        return False
    n = [int(c) for c in d]
    odd = n[0] + n[2] + n[4] + n[6] + n[8]     # 1., 3., 5., 7., 9. haneler
    even = n[1] + n[3] + n[5] + n[7]           # 2., 4., 6., 8. haneler
    if (odd * 7 - even) % 10 != n[9]:
        return False
    if sum(n[:10]) % 10 != n[10]:
        return False
    return True


def vkn_valid(s: str) -> bool | None:
    """10 haneli Vergi Kimlik No sağlaması (resmi algoritma).
    Döner: True/False; girdi 10 rakam değilse None."""
    d = _only_digits(s)
    if len(d) != 10:
        return None
    n = [int(c) for c in d]
    total = 0
    for i in range(9):
        tmp = (n[i] + (9 - i)) % 10
        if tmp == 0:
            total += 0
        else:
            v = (tmp * pow(2, 9 - i, 9)) % 9
            total += (9 if v == 0 else v)
    return (10 - (total % 10)) % 10 == n[9]


def id_valid(s: str) -> bool | None:
    """Maskeli değilse TCKN (11) ya da VKN (10) sağlamasını uygular; aksi halde None."""
    if not s or "*" in s:
        return None
    d = _only_digits(s)
    if len(d) == 11:
        return tckn_valid(d)
    if len(d) == 10:
        return vkn_valid(d)
    return None


def bank_label_from_iban(iban: str) -> str:
    """
    IBAN'dan banka etiketi. Bilinen kod -> banka adı. Kod var ama tabloda yoksa
    en azından kodu göster ('Tanımsız kurum (IBAN kodu: 00XYZ)') ki kullanıcı
    bakabilsin. IBAN geçersizse boş.
    """
    code = iban_bank_code(iban)
    if not code:
        return ""
    name = IBAN_BANK_CODES.get(code)
    if name:
        return name
    return f"Tanımsız kurum (IBAN kodu: {code})"


def bank_from_text(text: str) -> str:
    t = (text or "").lower()
    for pat, name in NAME_KEYWORDS:
        if re.search(pat, t):
            return name
    return ""

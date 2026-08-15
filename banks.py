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


def bank_from_iban(iban: str) -> str:
    """IBAN banka kodundan banka adını döndürür; bilinmiyorsa boş."""
    code = iban_bank_code(iban)
    return IBAN_BANK_CODES.get(code, "") if code else ""


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

"""
Türk bankaları: IBAN banka kodları ve isim eşleştirmeleri.
Turkish banks: IBAN bank codes and name matching.
"""
from __future__ import annotations
import re

# 5 haneli IBAN banka kodu -> banka adı
IBAN_BANK_CODES = {
    "00010": "T.C. Ziraat Bankası",
    "00012": "Türkiye Halk Bankası",
    "00015": "VakıfBank",
    "00032": "Türk Ekonomi Bankası (TEB)",
    "00046": "Akbank",
    "00059": "Şekerbank",
    "00062": "Garanti BBVA",
    "00064": "Türkiye İş Bankası",
    "00067": "Yapı ve Kredi Bankası",
    "00099": "ING Bank",
    "00103": "Fibabanka",
    "00111": "QNB Finansbank",
    "00123": "HSBC Bank",
    "00134": "DenizBank",
    "00143": "Odea Bank",
    "00146": "Aktif Yatırım Bankası",
    "00203": "Albaraka Türk Katılım",
    "00205": "Kuveyt Türk Katılım",
    "00206": "Türkiye Finans Katılım",
    "00209": "Ziraat Katılım",
    "00210": "Vakıf Katılım",
    "00211": "Emlak Katılım",
    "00015000": "VakıfBank",
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
    (r"enpara", "QNB Finansbank (Enpara)"),
    (r"kuveyt türk", "Kuveyt Türk Katılım"),
    (r"albaraka", "Albaraka Türk Katılım"),
    (r"ing bank|\bing\b", "ING Bank"),
    (r"papara", "Papara"),
    (r"ziraat kat", "Ziraat Katılım"),
]


def normalize_iban(raw: str) -> str:
    s = re.sub(r"\s+", "", raw or "").upper()
    return s


def bank_from_iban(iban: str) -> str:
    s = normalize_iban(iban)
    m = re.match(r"TR\d{2}(\d{5})", s)
    if m:
        return IBAN_BANK_CODES.get(m.group(1), "")
    return ""


def bank_from_text(text: str) -> str:
    t = (text or "").lower()
    for pat, name in NAME_KEYWORDS:
        if re.search(pat, t):
            return name
    return ""

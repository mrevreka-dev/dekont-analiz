"""
Evrensel (bankadan bağımsız) alan çıkarımı / Universal bank-agnostic extraction.

Belirli bir banka şablonuna bağlı kalmadan, TÜM Türk bankalarının dekontlarındaki
"gönderen / alıcı adı, IBAN, tutar, tarih" bilgilerini çıkarmayı hedefler.
Yöntem:
  - Kapsamlı bir Türk bankacılık ETİKET SÖZLÜĞÜ (her alanın çok sayıda varyasyonu,
    rol etiketli: gönderen / alıcı / nötr).
  - Geometrik (koordinat-tabanlı) etiket-değer eşleştirme (geo.py) birincil yol;
    OCR / kelime yoksa metin-tabanlı etiket ayrıştırma yedek yol.
  - Rol ataması: "Alıcı/Alacaklı/Lehtar/Karşı/Hedef" -> alıcı; "Gönderen/Gönderici/
    Borçlu/Hesap Sahibi" -> gönderen; rolsüz "IBAN/Tutar/Ad Soyad" -> sırayla/konuma göre.

Bilinen 3 format (Garanti, İş Bankası, VakıfBank) extract.py'de özel olarak ele
alınır; bu modül DİĞER tüm bankalar (Ziraat, Akbank, Yapı Kredi, Halkbank, QNB,
Denizbank, TEB, ING, Kuveyt Türk, Albaraka, Enpara, Papara, ...) için devreye girer.
"""
from __future__ import annotations

import re
import geo
import banks

# ---------------------------------------------------------------------------
# ETİKET SÖZLÜĞÜ: (alias listesi, kanonik alan, rol)
#   rol: "s" = gönderen (sender), "r" = alıcı (receiver), "" = nötr/ortak
# Alias'lar aksan-duyarsız eşleşir (geo.norm). Uzun ifadeler önce denenir.
# ---------------------------------------------------------------------------
LABELS: list[tuple[list[str], str, str]] = [
    # --- ALICI (receiver) adı ---
    (["Alıcı Ad Soyad/Unvan", "Alıcı Adı Soyadı/Unvanı", "Alıcı İsim\\Unvan", "Alıcı İsim/Unvan",
      "Alıcı Ad Soyad", "Alıcı Adı Soyadı", "Alıcı Ad/Unvan", "Alıcı Unvan", "Alıcı Ünvanı",
      "Alıcı Adı", "Alıcı İsmi", "Alacaklı Adı Soyadı", "Alacaklı Ad Soyad", "Alacaklı",
      "Lehtar Adı", "Lehtar", "Karşı Taraf", "Karşı Taraf Adı", "Alıcı"], "receiver_name", "r"),
    # --- ALICI IBAN / hesap ---
    (["Alıcı IBAN", "Alıcı Hesap No / IBAN", "Alıcı Hesap No/IBAN", "Alıcı Hesap/IBAN",
      "Alıcı Hesap No", "Alıcı Hesap Numarası", "Alıcı Hesap", "Alacaklı IBAN", "Lehtar IBAN",
      "Karşı IBAN", "Hedef IBAN", "Alıcı IBAN No"], "receiver_iban", "r"),
    # --- ALICI banka ---
    (["Alıcı Banka", "Alıcı Bankası", "Alacaklı Banka", "Karşı Banka", "Hedef Banka",
      "Alıcı Banka Adı"], "receiver_bank", "r"),
    # --- GÖNDEREN (sender) adı ---
    (["Gönderen Ad Soyad/Unvan", "Gönderen Adı Soyadı/Unvanı", "Gönderen Ad Soyad", "Gönderen Adı Soyadı",
      "Gönderen Ad/Unvan", "Gönderen Unvan", "Gönderen Ünvanı", "Gönderen Adı", "Gönderen İsmi",
      "Gönderici Ad Soyad", "Gönderici Adı Soyadı", "Gönderici", "Borçlu Adı Soyadı", "Borçlu",
      "Hesap Sahibi", "Ad Soyad", "Adı Soyadı", "Gönderen"], "sender_name", "s"),
    # --- GÖNDEREN IBAN / hesap ---
    (["Gönderen IBAN", "Gönderen Hesap No / IBAN", "Gönderen Hesap No/IBAN", "Gönderen Hesap/IBAN",
      "Gönderen Hesap No", "Gönderen Hesap Numarası", "Gönderen Hesap", "Gönderici IBAN",
      "Borçlu IBAN", "Hesap Numarası", "Hesap No", "IBAN No", "IBAN"], "sender_iban", "s"),
    # --- Kimlik / şube / müşteri ---
    (["TC Kimlik No", "TC Kimlik Numarası", "TCKN", "T.C. Kimlik No", "Kimlik No"], "sender_tckn", "s"),
    (["Müşteri No", "Müşteri Numarası"], "sender_customer", "s"),
    (["Şube Adı", "Şube", "Şube Kodu"], "sender_branch", "s"),
    # --- Tutar ---
    (["Toplam İşlem Tutarı", "Toplam Tutar", "İşlem Tutarı", "Gönderilen Tutar", "Transfer Tutarı",
      "Havale Tutarı", "EFT Tutarı", "FAST Tutarı", "Ödeme Tutarı", "İşlem Miktarı", "Tutar", "Miktar"],
     "amount", ""),
    (["FAST Ücreti ve Vergi", "İşlem Ücreti", "Masraf Tutarı", "Masraf", "Ücret", "Komisyon",
      "Komisyon Tutarı", "İşlem Masrafı"], "fee", ""),
    # --- Tarih ---
    (["Dekont Tarihi", "İşlem Tarihi", "İşlem Zamanı", "Düzenlenme Tarihi", "İşlem Zam./Valör",
      "Valör Tarihi", "Tarih"], "date", ""),
    # --- Referans ---
    (["Referans Numarası", "Referans No", "Sorgu Numarası", "Sorgu No", "İşlem No", "İşlem Numarası",
      "Dekont No", "Dekont Numarası", "FAST Ref No", "Fiş No", "Sıra No", "Makbuz No"], "ref_no", ""),
    # --- Tür / kanal / açıklama ---
    (["Senaryo/Dekont Tipi", "İşlem Türü", "İşlem Tipi", "Dekont Tipi"], "type", ""),
    (["İşlem Yeri", "İşlem Kanalı", "Kanal"], "channel", ""),
    (["İşlem Açıklaması", "Ödeme Açıklaması", "Açıklama"], "description", ""),
]

# phrase(norm) -> (field, role)  hızlı arama tablosu + tüm alias düz listesi
_PHRASE_MAP: dict[str, tuple[str, str]] = {}
_ALL_PHRASES: list[str] = []
for aliases, field, role in LABELS:
    for a in aliases:
        _PHRASE_MAP[geo.norm(a)] = (field, role)
        _ALL_PHRASES.append(a)

_CURRENCY_RE = re.compile(r"\b(TL|TRY|USD|EUR|GBP)\b", re.I)


def _is_amount_field(v):
    return v is not None


def universal_extract(pdf_bytes: bytes | None, text: str, reading_text: str = "") -> dict:
    """
    Bankadan bağımsız çıkarım. Öncelik geometrik (pdf_bytes varsa), yedek metin.
    Döndürdüğü dict extract.py tarafından Extraction'a eşlenir.
    """
    out = {"sender_name": "", "sender_iban": "", "sender_tckn": "", "sender_customer": "",
           "sender_branch": "", "receiver_name": "", "receiver_iban": "", "receiver_bank": "",
           "amount": None, "amount_currency": "", "fee": None, "date": "", "ref_no": "",
           "type": "", "channel": "", "description": "", "all_ibans": []}

    # --- 1) Geometrik ---
    geo_map = {}
    if pdf_bytes:
        try:
            words = geo.get_words(pdf_bytes)
            geo_map = geo.label_value_map(words, _ALL_PHRASES)
        except Exception:
            geo_map = {}

    # --- 2) Metin (yedek / tamamlayıcı) ---
    txt = reading_text or text or ""
    text_map = _text_label_map(txt)

    # birleştir: geometrik öncelikli, boşları metinden doldur
    combined: dict[str, str] = {}
    for phrase, val in geo_map.items():
        combined.setdefault(geo.norm(phrase), val)
    for nphrase, val in text_map.items():
        combined.setdefault(nphrase, val)

    # --- 3) Kanonik alanlara eşle (rol dikkate alınarak) ---
    for nphrase, val in combined.items():
        fr = _PHRASE_MAP.get(nphrase)
        if not fr or not val:
            continue
        field, role = fr
        if field in ("receiver_name", "sender_name"):
            name = geo.clean_name(val)
            if name and not out[field]:
                out[field] = name
        elif field in ("receiver_iban", "sender_iban"):
            ib = geo.clean_iban(val)
            if ib and not out[field]:
                out[field] = ib
        elif field == "amount":
            a = geo.clean_amount(val)
            if a is not None and out["amount"] is None:
                out["amount"] = a
                cm = _CURRENCY_RE.search(val)
                out["amount_currency"] = cm.group(1).upper() if cm else ""
        elif field == "fee":
            a = geo.clean_amount(val)
            if a is not None and out["fee"] is None:
                out["fee"] = a
        elif field == "receiver_bank":
            if not out["receiver_bank"]:
                out["receiver_bank"] = geo.clean_name(val) or val.strip()
        elif field in ("sender_tckn", "sender_customer", "sender_branch"):
            if not out[field]:
                out[field] = val.strip()
        else:  # date, ref_no, type, channel, description
            if not out.get(field):
                out[field] = val.strip()

    # --- 4) Tüm IBAN'lar (konumsal yedek) ---
    all_ibans = _all_ibans(txt)
    out["all_ibans"] = all_ibans
    # rolsüz IBAN eksikse konuma göre ata: ilk=gönderen, ikinci=alıcı
    if not out["sender_iban"] and all_ibans:
        out["sender_iban"] = all_ibans[0]
    if not out["receiver_iban"] and len(all_ibans) >= 2:
        # gönderenden farklı ilk IBAN
        for ib in all_ibans:
            if ib != out["sender_iban"]:
                out["receiver_iban"] = ib
                break

    # --- 5) Tutar yedeği: en büyük tutar ---
    if out["amount"] is None:
        amts = [geo.clean_amount(m) for m in re.findall(r"-?\d{1,3}(?:\.\d{3})*,\d{2}", txt)]
        amts = [a for a in amts if a is not None]
        if amts:
            out["amount"] = max(amts)
            cm = _CURRENCY_RE.search(txt)
            out["amount_currency"] = cm.group(1).upper() if cm else ""

    # banka tamamla
    if not out["receiver_bank"] and out["receiver_iban"]:
        out["receiver_bank"] = banks.bank_from_iban(out["receiver_iban"])
    return out


def _text_label_map(text: str) -> dict:
    """Metinden 'LABEL : VALUE' (aksan-duyarsız) çıkarır. {norm_phrase: value}."""
    res = {}
    if not text:
        return res
    ntext = geo.norm(text)
    # uzun ifadeler önce
    for phrase in sorted(_ALL_PHRASES, key=lambda p: -len(p)):
        nlab = geo.norm(phrase)
        if nlab in res:
            continue
        m = re.search(rf"(?<![a-z0-9]){re.escape(nlab)}\s*[:：]\s*(.+)", ntext)
        if m:
            # değeri orijinal metinden aynı konumdan al (norm uzunluk korur)
            val = text[m.start(1):m.end(1)]
            val = re.split(r"\s{2,}|\n", val)[0].strip()
            if val and len(val) > 1:
                res[nlab] = val
    return res


_IBAN_RE = re.compile(r"\bTR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}\b", re.I)


def _all_ibans(text: str) -> list:
    seen, out = set(), []
    for m in _IBAN_RE.finditer(text or ""):
        ib = re.sub(r"\s+", "", m.group(0)).upper()
        if ib not in seen:
            seen.add(ib); out.append(ib)
    return out

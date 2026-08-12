"""
Alan çıkarımı / Field extraction.

Dekont metninden (dijital metin veya OCR) yapılandırılmış bilgi çıkarır:
gönderici, alıcı, tutar, işlem bilgileri, banka.

Yaklaşım: etiket-tabanlı ayrıştırma (LABEL : VALUE) + IBAN/tutar/tarih/TCKN
desen tanıma. Türk bankalarının farklı formatlarına dayanıklı olacak şekilde
çok sayıda etiket eş anlamlısı tanımlanmıştır.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

import pdfplumber
import io

import banks

# ----------------------- Desen tanıyıcılar -----------------------
IBAN_RE = re.compile(r"\bTR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}\b", re.I)
IBAN_LOOSE_RE = re.compile(r"\bTR\s?\d{2}[\d ]{20,30}\b", re.I)
# Para tutarı — hem TÜRK (1.234.567,89) hem ULUSLARARASI/US (1,234,567.89) formatını yakalar.
# Bir tutar sayılması için ya binlik ayraç (3'lü gruplar) ya da ondalık bulunmalıdır;
# böylece açıklamadaki uzun sayı dizileri (ör. referans 35665290220) tutar sanılmaz.
# Not: öncesinde/sonrasında başka rakam-ayraç olmamalı; böylece tarih (12.08.2026) ve
# sürüm (3.3.1) parçaları tutar sanılmaz.
AMOUNT_RE = re.compile(
    r"(?<![\d.,])(?:-?\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|-?\d+[.,]\d{1,2})(?![.,]?\d)")
CURRENCY_RE = re.compile(r"\b(TL|TRY|USD|EUR|GBP)\b", re.I)
DATE_RE = re.compile(r"\b\d{2}[./]\d{2}[./]\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?")
TCKN_RE = re.compile(r"\b\d{2,11}\*{2,}\b|\b\d{11}\b")


def _parse_money_token(tok: str) -> float | None:
    """Tek bir para jetonunu (TR veya US biçimi) float'a çevirir.
    Kural: iki ayraç varsa EN SAĞDAKİ ondalıktır; tek ayraç varsa son grup 3 haneyse
    binlik, değilse ondalıktır. Böylece '100,000.00', '100.000,00', '100,000',
    '100.000', '100,00', '100.00' hepsi doğru çözülür."""
    t = (tok or "").replace(" ", "").strip()
    neg = t.startswith("-")
    t = t.lstrip("+-")
    if not re.search(r"\d", t):
        return None
    has_dot, has_com = "." in t, "," in t
    if has_dot and has_com:
        if t.rfind(",") > t.rfind("."):      # virgül daha sağda -> TR (virgül ondalık)
            t = t.replace(".", "").replace(",", ".")
        else:                                 # nokta daha sağda -> US (nokta ondalık)
            t = t.replace(",", "")
    elif has_com:
        parts = t.split(",")
        t = t.replace(",", "") if (len(parts) > 2 or len(parts[-1]) == 3) else t.replace(",", ".")
    elif has_dot:
        parts = t.split(".")
        if len(parts) > 2 or len(parts[-1]) == 3:
            t = t.replace(".", "")            # binlik (ör. 100.000 -> 100000)
        # aksi halde nokta zaten ondalık (ör. 100.00)
    try:
        v = float(t)
        return -v if neg else v
    except ValueError:
        return None


def parse_amount(s: str) -> float | None:
    m = AMOUNT_RE.search(s or "")
    if not m:
        return None
    return _parse_money_token(m.group(0))


@dataclass
class Party:
    name: str = ""
    iban: str = ""
    account_no: str = ""
    customer_no: str = ""
    tckn: str = ""
    branch: str = ""
    bank: str = ""


@dataclass
class Amount:
    value: float | None = None
    currency: str = ""
    text: str = ""
    fee: float | None = None
    total: float | None = None


@dataclass
class Transaction:
    date: str = ""
    value_date: str = ""
    ref_no: str = ""
    document_no: str = ""
    ettn: str = ""
    receipt_no: str = ""
    type: str = ""
    channel: str = ""
    description: str = ""
    sequence_number: str = ""     # banka bazlı sayısal işlem/sorgu numarası (sıra analizi için)


@dataclass
class Extraction:
    bank: str = ""
    doc_kind: str = ""          # ör. "e-Dekont / EFT", "HESAPTAN FAST"
    sender: Party = field(default_factory=Party)
    receiver: Party = field(default_factory=Party)
    amount: Amount = field(default_factory=Amount)
    transaction: Transaction = field(default_factory=Transaction)
    text_source: str = ""       # "digital" | "ocr" | "none"
    raw_text: str = ""
    all_ibans: list = field(default_factory=list)
    all_amounts: list = field(default_factory=list)
    confidence: float = 0.0     # çıkarım güveni (0-1)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["raw_text"] = self.raw_text[:4000]
        return d


def merge_extractions(primary: "Extraction", others: list) -> "Extraction":
    """Birden çok OCR varyantından çıkan alanları birleştirir (boşları doldurur).
    Kötü fotoğraflarda farklı varyantlar farklı alanları okuyabildiği için sonuç iyileşir."""
    def take_name(a, b):
        # daha uzun ve geçerli görünen ismi tercih et
        if not a: return b
        if not b: return a
        return a if len(a) >= len(b) else b
    for o in others:
        if not o:
            continue
        s, r = primary.sender, primary.receiver
        s.name = take_name(s.name, o.sender.name) if not s.name else s.name
        for fld in ("iban", "tckn", "customer_no", "branch", "bank"):
            if not getattr(s, fld) and getattr(o.sender, fld):
                setattr(s, fld, getattr(o.sender, fld))
        r.name = r.name or o.receiver.name
        for fld in ("iban", "bank"):
            if not getattr(r, fld) and getattr(o.receiver, fld):
                setattr(r, fld, getattr(o.receiver, fld))
        if primary.amount.value is None and o.amount.value is not None:
            primary.amount.value = o.amount.value
            primary.amount.currency = primary.amount.currency or o.amount.currency
        for fld in ("fee", "total"):
            if getattr(primary.amount, fld) is None and getattr(o.amount, fld) is not None:
                setattr(primary.amount, fld, getattr(o.amount, fld))
        for fld in ("date", "ref_no", "document_no", "ettn", "type", "channel", "description"):
            if not getattr(primary.transaction, fld) and getattr(o.transaction, fld):
                setattr(primary.transaction, fld, getattr(o.transaction, fld))
        if not primary.bank and o.bank:
            primary.bank = o.bank
        primary.all_ibans = primary.all_ibans or o.all_ibans
    primary.confidence = _confidence(primary)
    return primary


# Dekont içeriği göstergeleri (bir görselin dekont olup olmadığını anlamak için)
_RECEIPT_KEYWORDS = [
    "dekont", "işlem", "islem", "tutar", "iban", "gönderen", "gonderen", "alıcı", "alici",
    "alacaklı", "havale", "eft", "fast", "referans", "banka", "hesap", "komisyon", "masraf",
    "bsmv", "valör", "valor", "sorgu", "bankası", "bankasi", "tl", "try", "ödeme", "odeme",
]


def ocr_recover(ex: "Extraction", text: str) -> None:
    """OCR ile bozulmuş metinden ek bilgi kurtarır (banka kodu, IBAN, sıra no).
    Bulanık fotoğraflarda etiketler bozulsa bile banka kodu/rakam dizileri okunabilir."""
    if not text:
        return
    # Banka kodu: '... Banka 0157 ...' -> 0157 (4 hane) -> IBAN kodu 00157
    if not ex.receiver.bank:
        for m in re.finditer(r"[Bb]ank\w{0,3}\W{0,6}(\d{4})\b", text):
            name = banks.IBAN_BANK_CODES.get("0" + m.group(1))
            if name:
                ex.receiver.bank = name
                break
    # IBAN kurtarma: TR + 24 haneli dizi (OCR harf karışıklığı toleranslı), gruplu
    if not ex.receiver.iban or not ex.sender.iban:
        trans = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5", "B": "8",
                               "Z": "2", "Q": "0", "D": "0", "G": "6", "g": "9"})
        found = []
        for m in re.finditer(r"[T7][Rr][ ]?([\dOoIlSBZQDGg][ \dOoIlSBZQDGg]{24,34})", text):
            digits = re.sub(r"[^0-9]", "", m.group(1).translate(trans))
            if len(digits) >= 24:
                cand = "TR" + digits[:24]
                if cand not in found:
                    found.append(cand)
        if found:
            if not ex.receiver.iban:
                ex.receiver.iban = found[0]
            if not ex.sender.iban and len(found) > 1:
                ex.sender.iban = found[1]
    # IBAN'dan banka tamamla
    if not ex.receiver.bank and ex.receiver.iban:
        ex.receiver.bank = banks.bank_label_from_iban(ex.receiver.iban)


def derive_sequence_number(ex: "Extraction") -> str:
    """Banka bazlı sayısal işlem/sorgu/referans numarasını (sıra analizi için) çıkarır.
    En uzun bitişik rakam dizisini (>= 6 hane) tercih eder."""
    cands = [ex.transaction.document_no, ex.transaction.ref_no]
    best = ""
    for c in cands:
        for run in re.findall(r"\d{6,}", c or ""):
            if len(run) > len(best):
                best = run
    return best


def receipt_content_score(text: str, ex: "Extraction") -> float:
    """0-1: metnin/çıkarımın bir banka dekontu olma olasılığı."""
    if not text:
        text = ""
    low = _norm_tr(text)
    kw = sum(1 for k in _RECEIPT_KEYWORDS if k in low)
    score = 0.0
    score += min(kw, 6) / 6 * 0.5          # anahtar kelimeler
    if ex.all_ibans:
        score += 0.2
    if ex.amount.value is not None:
        score += 0.15
    if ex.bank:
        score += 0.1
    if ex.transaction.date:
        score += 0.05
    return round(min(score, 1.0), 2)


def extract_text_digital(pdf_bytes: bytes, layout: bool = True) -> str:
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                if layout:
                    t = page.extract_text(layout=True) or ""
                else:
                    t = page.extract_text() or ""
                parts.append(t)
            return "\n".join(parts)
    except Exception:
        return ""


# Türkçe -> ASCII (UZUNLUK KORUYAN: her karakter tek karaktere) — OCR toleransı için
_TR_MAP = str.maketrans({
    "İ": "i", "I": "i", "ı": "i", "Ş": "s", "ş": "s", "Ğ": "g", "ğ": "g",
    "Ü": "u", "ü": "u", "Ö": "o", "ö": "o", "Ç": "c", "ç": "c",
    "Â": "a", "â": "a", "Î": "i", "î": "i", "Û": "u", "û": "u",
})


def _norm_tr(s: str) -> str:
    """Diyakritikleri ASCII'ye katlar; uzunluk korunur (pozisyon eşlemesi için)."""
    return (s or "").translate(_TR_MAP).lower()


def _find_label(text: str, labels: list[str]) -> str:
    """
    'LABEL : VALUE' kalıbından değeri çeker. Aksan-duyarsız eşleşir; böylece hem
    düzgün Türkçe hem OCR ile bozulmuş (İ->i, ş->s) metinde çalışır. Değer,
    orijinal metinden (aksanlar korunarak) alınır — normalize uzunluk-koruyandır.
    """
    ntext = _norm_tr(text)
    for lab in labels:
        nlab = _norm_tr(lab)
        m = re.search(rf"{nlab}\s*[:：]\s*(.+)", ntext, re.I)
        if m:
            # değeri ORİJİNAL metinden aynı konumdan al (uzunluk korunduğu için span aynı)
            val = text[m.start(1):m.end(1)].strip()
            val = re.split(r"\s{2,}", val)[0].strip()
            if val:
                return val
    return ""


def _clean_name(s: str) -> str:
    s = re.sub(r"\s{2,}", " ", s or "").strip()
    s = re.sub(r"[:：]+$", "", s).strip()
    return s


def _after_label(text: str, label: str, stops: list[str]) -> str:
    """'LABEL: DEĞER  STOP:...' kalıbında, etiketten sonraki değeri BİR SONRAKİ etikete
    (stops) ya da satır sonuna kadar döndürür. Böylece 'GÖNDEREN: Özgür İnci AÇIKLAMA:...'
    -> 'Özgür İnci' ve 'ALICI ÜNVANI: Mezher Kaya ALICI IBAN:...' -> 'Mezher Kaya'."""
    m = re.search(re.escape(label) + r"\s*[:：]\s*(.+)", text)
    if not m:
        return ""
    val = m.group(1)
    # satır sonunda kes
    val = val.splitlines()[0]
    # bir sonraki etikette kes — etiket önceki kelimeye BİTİŞİK de olabilir
    # (ör. "Tolga ŞengülALICI IBAN"), bu yüzden kelime sınırı aranmaz; düz alt-dizi yeter.
    cut = len(val)
    for st in stops:
        mm = re.search(re.escape(st), val)
        if mm and mm.start() < cut:
            cut = mm.start()
    return val[:cut].strip(" :：-,")


def extract_fields(text: str, reading_text: str = "", pdf_bytes: bytes | None = None) -> Extraction:
    ex = Extraction()
    ex.raw_text = text or reading_text
    if (not text or not text.strip()) and (not reading_text or not reading_text.strip()):
        return ex

    lines = [l for l in (text or "").splitlines()]
    joined = "\n".join(lines)
    rjoined = reading_text or joined

    # --- Tüm IBAN ve tutarlar ---
    ex.all_ibans = [banks.normalize_iban(m.group(0)) for m in IBAN_RE.finditer(joined)]
    if not ex.all_ibans:
        ex.all_ibans = [banks.normalize_iban(m.group(0)) for m in IBAN_LOOSE_RE.finditer(joined)
                        if len(re.sub(r"\D", "", m.group(0))) >= 24]
    # tekilleştir
    seen = set(); uniq = []
    for ib in ex.all_ibans:
        if ib not in seen and len(ib) >= 20:
            seen.add(ib); uniq.append(ib)
    ex.all_ibans = uniq
    ex.all_amounts = [m.group(0).strip() for m in AMOUNT_RE.finditer(joined)]

    # --- Format / banka tespiti (gönderici öncelikli; alıcı banka adını yakalamamak için) ---
    # Format tespiti bankanın KENDİ imzasına (başlık/footer/özel etiket) sabitlenir;
    # karşı-taraf banka adının metinde geçmesi tetiklememeli.
    low = joined.lower()
    up = joined.upper()
    is_vakif = ("vakifbank.com" in low or "vakıflar bankası" in low
                or ("VAKIFBANK" in up and "İŞLEM BİLGİLERİ" in up))
    is_isbank = ("isbank.com" in low or "ETTN" in joined
                 or ("e-dekont" in low and "doküman numarası" in low))
    is_garanti = ("HESAPTAN" in up or "garantibbva" in low)
    # Enpara/QNB FAST dekontu: net satır-içi etiketler ("ALICI ÜNVANI", "EFT TUTARI", ...)
    is_enpara = ("enpara" in low
                 or ("ALICI ÜNVANI" in up and "EFT TUTARI" in up)
                 or ("MÜŞTERİ ÜNVANI" in up and "GIDEN FAST" in up))

    if is_vakif:
        ex.bank = "VakıfBank"
    elif is_isbank:
        ex.bank = "Türkiye İş Bankası"
    elif is_garanti:
        ex.bank = "Garanti BBVA"
    elif is_enpara:
        ex.bank = "Enpara.com (QNB)"

    # =============================================================
    #  İŞ BANKASI e-Dekont formatı
    # =============================================================
    if is_isbank:
        ex.doc_kind = "e-Dekont"
        ex.transaction.type = _find_label(joined, ["Senaryo/Dekont Tipi", "İşlem Türü"])
        ex.transaction.channel = _find_label(joined, ["İşlem Yeri"])
        ex.transaction.document_no = _find_label(joined, ["Doküman Numarası", "e-Dekont Belge No"])
        ex.transaction.ettn = _find_label(joined, ["ETTN"])
        ex.transaction.ref_no = _find_label(joined, ["Referans Numarası", "Sorgu Numarası"])
        ex.transaction.date = _find_label(joined, ["Dekont Tarihi", "İşlem Zam", "İşlem Zam\\./Valör"])
        ex.transaction.description = _find_label(joined, ["Açıklama"])
        # Gönderici: sol üstteki isim (Müşteri No satırından önce) — ilk büyük harf satırı
        ex.sender.customer_no = _find_label(joined, ["Müşteri No"])
        ex.sender.tckn = _find_label(joined, ["TCKN", "TC Kimlik"])
        ex.sender.iban = _first_iban_after(joined, ["IBAN"]) or (ex.all_ibans[0] if ex.all_ibans else "")
        ex.sender.name = _isbank_sender_name(lines)
        # Alıcı
        ex.receiver.name = _clean_name(_find_label(joined, ["Alıcı Isim.?Unvan", "Alıcı İsim.?Unvan", "Alıcı Ad"]))
        ex.receiver.iban = banks.normalize_iban(_find_label(joined, ["Alıcı IBAN"]))
        ex.receiver.bank = _find_label(joined, ["Alıcı Banka"])
        # Tutar
        amt_txt = _find_label(joined, ["İşlem Tutarı"])
        ex.amount.value = parse_amount(amt_txt)
        ex.amount.text = amt_txt
        cm = CURRENCY_RE.search(amt_txt or "")
        ex.amount.currency = (cm.group(1).upper() if cm else "TRY")
        ex.amount.fee = parse_amount(_find_label(joined, ["FAST Ücreti ve Vergi", "Ücret"]))
        ex.amount.total = parse_amount(_find_label(joined, ["Toplam İşlem Tutarı"]))

    # =============================================================
    #  GARANTİ BBVA "HESAPTAN FAST" formatı
    # =============================================================
    elif is_garanti:
        ex.doc_kind = _detect_garanti_kind(joined)
        ex.sender.branch = _find_label(joined, ["ŞUBE ADI", "SUBE ADI"])
        ex.sender.customer_no = _find_label(joined, ["MÜŞTERİ NUMARASI", "MUSTERI NUMARASI"])
        ex.sender.account_no = _find_label(joined, ["HESAP NUMARASI"])
        ex.sender.tckn = _find_label(joined, ["TC KİMLİK NO", "TC KIMLIK NO"])
        ex.transaction.date = _find_label(joined, ["DÜZENLENME TARİHİ", "İŞLEM TARİHİ", "ISLEM TARIHI"])
        ex.transaction.channel = _find_label(joined, ["İŞLEM YERİ", "ISLEM YERI"])
        ex.transaction.ref_no = _find_label(joined, ["FAST REF NO", "REF NO"])
        # Gönderici adı: "SAYIN" sonrası satır
        ex.sender.name = _garanti_sender_name(lines)
        ex.sender.iban = _first_iban_after(joined, ["IBAN"]) or (ex.all_ibans[0] if ex.all_ibans else "")
        # Alıcı
        ex.receiver.name = _clean_name(_find_label(joined, ["ALACAKLI"]))
        ex.receiver.iban = banks.normalize_iban(_find_label(joined, ["ALACAKLI IBAN"]))
        # Tutar: "TUTAR : - 50,00 TL"
        amt_txt = _find_label(joined, ["TUTAR"])
        ex.amount.value = parse_amount(amt_txt)
        ex.amount.text = amt_txt
        cm = CURRENCY_RE.search(amt_txt or "")
        ex.amount.currency = (cm.group(1).upper() if cm else "TL")
        ex.amount.fee = parse_amount(_find_label(joined, ["MASRAF"]))
        ex.amount.total = parse_amount(_find_label(joined, ["KOMİSYON TOPLAMI", "KOMISYON TOPLAMI"]))

    # =============================================================
    #  VAKIFBANK formatı (çok sütunlu; okuma-sırası metni ile)
    # =============================================================
    elif is_vakif:
        ex.doc_kind = "İşlem Dekontu"
        ex.sender.bank = "VakıfBank"
        # Öncelik: geometrik (koordinat-tabanlı) çıkarım
        geo_ok = False
        if pdf_bytes:
            try:
                import geo
                g = geo.vakif_fields(pdf_bytes)
                if g:
                    geo_ok = True
                    ex.sender.name = g["sender_name"]
                    ex.receiver.name = g["receiver_name"]
                    ex.sender.iban = g["sender_iban"]
                    ex.receiver.iban = g["receiver_iban"]
                    ex.receiver.bank = g["receiver_bank"]
                    if g["amount"] is not None:
                        ex.amount.value = g["amount"]; ex.amount.currency = "TL"
                    ex.amount.fee = g["fee"]
                    ex.transaction.date = g["date"]
                    ex.transaction.ref_no = g["ref_no"]
                    ex.transaction.document_no = g["document_no"]
                    ex.transaction.type = g["type"]
            except Exception:
                geo_ok = False
        if geo_ok:
            # geometrik çıkarım tamam; IBAN'lardan banka tamamla ve bitir
            if not ex.receiver.bank and ex.receiver.iban:
                ex.receiver.bank = banks.bank_label_from_iban(ex.receiver.iban)
            if not ex.sender.bank and ex.sender.iban:
                ex.sender.bank = banks.bank_label_from_iban(ex.sender.iban)
            ex.confidence = _confidence(ex)
            return ex
        # ---- Yedek: metin-tabanlı ayrıştırma ----
        rt = rjoined
        ex.transaction.type = _find_label(rt, ["İŞLEM TÜRÜ", "ISLEM TURU"])
        dm = re.search(r"İŞLEM TARİHİ\s*[:：]?\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2}:\d{2})?)", rt)
        ex.transaction.date = dm.group(1) if dm else (DATE_RE.search(rt).group(0) if DATE_RE.search(rt) else "")
        ex.transaction.ref_no = _find_label(rt, ["SORGU NO"])
        im = re.search(r"İŞLEM NO\s*[:：]?\s*(\d{6,})", rt)
        ex.transaction.document_no = im.group(1) if im else ""
        ex.transaction.description = _find_label(rt, ["İŞLEM AÇIKLAMASI"])
        # Tutar
        am = re.search(r"İŞLEM TUTARI\s*[:：]?\s*([\d.]+,\d{2})\s*(TL|TRY)?", rt)
        if am:
            ex.amount.value = parse_amount(am.group(1))
            ex.amount.text = am.group(0).replace("İŞLEM TUTARI", "").strip(" :")
            ex.amount.currency = (am.group(2) or "TL").upper()
        fm = re.search(r"MASRAF TUTARI\s*[:：]?\s*([\d.]+,\d{2})", rt)
        if fm:
            ex.amount.fee = parse_amount(fm.group(1))
        # Alıcı banka
        ex.receiver.bank = _clean_name(_find_label(rt, ["ALICI BANKA"]))
        # İsimler (VakıfBank etiketleri satır kaydırmalı olabilir)
        s_name, r_name = _vakif_names(rt)
        ex.sender.name = s_name
        ex.receiver.name = r_name
        # IBAN'lar
        if ex.all_ibans:
            ex.receiver.iban = ex.all_ibans[0]
            if len(ex.all_ibans) > 1:
                ex.sender.iban = ex.all_ibans[1]
        ex.sender.bank = "VakıfBank"

    # =============================================================
    #  ENPARA.com / QNB FAST dekontu (net satır-içi etiketler)
    # =============================================================
    elif is_enpara:
        ex.doc_kind = _detect_garanti_kind(joined)  # GIDEN FAST EFT / HAVALE ...
        rt = rjoined or joined
        _STOPS = ["ALICI IBAN", "ALICI ÜNVANI", "MÜŞTERİ ÜNVANI", "MUSTERI UNVANI",
                  "GÖNDEREN", "GONDEREN", "AÇIKLAMA", "ACIKLAMA", "IBAN", "KATILIMCI",
                  "EFT TUTARI", "EFT ÜCRETİ", "SORGU NO", "SIRA NO", "FİŞ NO", "FIS NO", "B/A"]
        # Alıcı
        ex.receiver.name = _clean_name(_after_label(rt, "ALICI ÜNVANI", _STOPS)
                                       or _after_label(rt, "ALICI UNVANI", _STOPS))
        ex.receiver.iban = banks.normalize_iban(_after_label(rt, "ALICI IBAN", _STOPS))
        # Gönderen (MÜŞTERİ ÜNVANI daha güvenilir; yoksa GÖNDEREN)
        ex.sender.name = _clean_name(_after_label(rt, "MÜŞTERİ ÜNVANI", _STOPS)
                                     or _after_label(rt, "MUSTERI UNVANI", _STOPS)
                                     or _after_label(rt, "GÖNDEREN", _STOPS)
                                     or _after_label(rt, "GONDEREN", _STOPS))
        # Gönderen IBAN: "MÜŞTERİ ÜNVANI ... IBAN : TR..." satırından ya da tüm IBAN'lardan
        s_iban = _after_label(rt, "MÜŞTERİ ÜNVANI", ["AÇIKLAMA", "GÖNDEREN"])
        sm = IBAN_RE.search(s_iban) or (IBAN_RE.search(_find_label(rt, ["IBAN"]) or ""))
        ex.sender.iban = banks.normalize_iban(sm.group(0)) if sm else ""
        # IBAN'lar eksikse konumsal ata (alıcı=Halkbank IBAN, gönderen=Enpara/QNB IBAN)
        for ib in ex.all_ibans:
            if ib != ex.receiver.iban and not ex.sender.iban:
                ex.sender.iban = ib
        # Tutar: "EFT TUTARI : 100,000.0 TL"  (etiketten sonraki İLK para jetonu)
        eft = _after_label(rt, "EFT TUTARI", ["EFT ÜCRETİ", "EFT UCRETI", "SORGU NO"])
        ex.amount.value = parse_amount(eft)
        ex.amount.text = eft
        cm = CURRENCY_RE.search(eft or "")
        ex.amount.currency = (cm.group(1).upper() if cm else "TL")
        fee_txt = _after_label(rt, "EFT ÜCRETİ(BSMV DAHİL)", ["SORGU NO"]) \
            or _after_label(rt, "EFT ÜCRETİ", ["SORGU NO"])
        _fee = parse_amount(fee_txt)
        if _fee is None and re.match(r"\s*0(\s|$|TL)", fee_txt or ""):
            _fee = 0.0                        # "0 TL" gibi ücretsiz durum
        ex.amount.fee = _fee
        # TCKN, açıklama, referans, sıra/fiş no
        ex.sender.tckn = _find_label(rt, ["TC kimlik numarası", "TC Kimlik"])
        ex.transaction.description = _after_label(rt, "AÇIKLAMA", ["SIRA NO", "FİŞ NO"]) \
            or _after_label(rt, "ACIKLAMA", ["SIRA NO", "FIS NO"])
        ex.transaction.channel = _find_label(rt, ["İşlem yeri", "İşlem Yeri"])
        sorgu = re.search(r"SORGU NO\s*[:：]?\s*(\d{4,})", rt) or re.search(r"sorgu no[:：]?\s*(\d{4,})", low)
        ex.transaction.ref_no = sorgu.group(1) if sorgu else ""
        fis = re.search(r"Fi[şs]\s*No\s*[:：]?\s*(\d{6,})", rt, re.I)
        ex.transaction.document_no = fis.group(1) if fis else ""
        sira = re.search(r"S[ıi]ra\s*No\s*[:：]?\s*([\d\-]{6,})", rt, re.I)
        ex.transaction.receipt_no = sira.group(1) if sira else ""
        ex.sender.bank = "Enpara.com (QNB)"

    # =============================================================
    #  EVRENSEL / diğer tüm bankalar (Ziraat, Akbank, Yapı Kredi, ...)
    # =============================================================
    else:
        ex.doc_kind = "Dekont"
        import universal
        u = universal.universal_extract(pdf_bytes, joined, rjoined)
        ex.sender.name = u["sender_name"]
        ex.sender.iban = u["sender_iban"]
        ex.sender.tckn = u["sender_tckn"]
        ex.sender.customer_no = u["sender_customer"]
        ex.sender.branch = u["sender_branch"]
        ex.receiver.name = u["receiver_name"]
        ex.receiver.iban = u["receiver_iban"]
        ex.receiver.bank = u["receiver_bank"]
        if u["amount"] is not None:
            ex.amount.value = u["amount"]
            ex.amount.currency = u["amount_currency"] or "TL"
        ex.amount.fee = u["fee"]
        ex.transaction.date = u["date"]
        ex.transaction.ref_no = u["ref_no"]
        ex.transaction.type = u["type"]
        ex.transaction.channel = u["channel"]
        ex.transaction.description = u["description"]
        ex.all_ibans = u["all_ibans"] or ex.all_ibans
        if not ex.bank:
            ex.bank = banks.bank_from_iban(ex.sender.iban) or banks.bank_from_text(joined) or u["receiver_bank"]

    # --- Tarih/saat normalizasyonu (tüm bankalar) ---
    # Bazı dekontlarda tarih+saat tek satırda "İşlem tarihi ve saati :12.08.2026 21:26"
    # biçiminde yer alır; geometrik çıkarım saati düşürüp başta ':' bırakabilir.
    # Bunu onarır: baştaki çöp temizlenir, saat de yakalanır.
    ex.transaction.date = _normalize_datetime(ex.transaction.date, rjoined or joined)

    # --- IBAN'lardan banka tamamlama ---
    if not ex.receiver.bank and ex.receiver.iban:
        ex.receiver.bank = banks.bank_label_from_iban(ex.receiver.iban)
    if not ex.sender.bank and ex.sender.iban:
        ex.sender.bank = banks.bank_label_from_iban(ex.sender.iban)
    if not ex.bank:
        ex.bank = ex.sender.bank or banks.bank_from_iban(ex.sender.iban) or ex.receiver.bank

    # --- Alıcı/gönderici IBAN ayrımı (eksikse) ---
    if not ex.receiver.iban and len(ex.all_ibans) >= 2:
        ex.receiver.iban = ex.all_ibans[1]
    if not ex.sender.iban and ex.all_ibans:
        ex.sender.iban = ex.all_ibans[0]

    ex.confidence = _confidence(ex)
    return ex


_IBAN_OCR_TRANS = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1", "S": "5",
                                 "B": "8", "Z": "2", "Q": "0", "D": "0", "g": "9"})


def _ocr_fix_iban(s: str) -> str:
    """OCR karışıklıklarını (O->0, I->1, S->5, B->8) düzeltip geçerli IBAN'a çevirir."""
    body = re.sub(r"\s", "", s[2:]).translate(_IBAN_OCR_TRANS)
    cand = "TR" + body
    m = re.match(r"TR\d{24}", cand)
    return m.group(0) if m else ""


def _first_iban_after(text: str, labels: list[str]) -> str:
    # "Alıcı IBAN" gibi karşı-taraf etiketlerini gönderici IBAN'ı ile karıştırma
    for lab in labels:
        pat = rf"(?<!Al[ıi]c[ıi] )(?<!Alacakl[ıi] ){lab}\s*[:：]?\s*(TR[\dOoIlSBZQDg][\dOoIlSBZQDg ]{{20,34}})"
        for m in re.finditer(pat, text):
            raw = m.group(1)
            if re.match(r"TR[\d ]{20,32}$", raw):
                return banks.normalize_iban(raw)
            fixed = _ocr_fix_iban(raw)   # OCR toleranslı (O->0, I->1, S->5, B->8)
            if fixed:
                return fixed
    return ""


_NAME_RE = re.compile(r"^[A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ\s\.]{3,}$")


def _left_col(raw: str) -> str:
    """Layout satırının sol sütununu (ilk 2+ boşluğa kadar) döndürür."""
    return re.split(r"\s{2,}", raw.strip())[0].strip()


def _leading_caps(s: str) -> str:
    """Satır başındaki ardışık BÜYÜK-HARF isim kelimelerini döndürür (OCR için)."""
    toks = s.strip().split()
    out = []
    for t in toks:
        # noktalama temizle
        tw = re.sub(r"[^\wÇĞİÖŞÜçğıöşü]", "", t)
        if len(tw) >= 2 and tw == tw.upper() and re.match(r"^[A-ZÇĞİÖŞÜ]+$", tw):
            out.append(tw)
        else:
            break
    return " ".join(out)


def _isbank_sender_name(lines: list[str]) -> str:
    # İş Bankası: sol üstte, "Müşteri No" satırından önceki BÜYÜK HARF isim
    for i, l in enumerate(lines):
        if re.search(r"Müşteri No|Musteri No", l, re.I):
            for j in range(i - 1, max(-1, i - 6), -1):
                cand = _left_col(lines[j])
                if _NAME_RE.match(cand) and "DEKONT" not in cand.upper():
                    return cand
                # OCR: satırın başındaki büyük-harf kelimeler (etiketten önce)
                lc = _leading_caps(lines[j])
                if len(lc.split()) >= 2 and "DEKONT" not in lc.upper() and "TURKIYE" not in lc.upper():
                    return lc
    for l in lines[:12]:
        cand = _left_col(l)
        if _NAME_RE.match(cand) and "DEKONT" not in cand.upper():
            return cand
        lc = _leading_caps(l)
        if len(lc.split()) >= 2 and not any(k in lc.upper() for k in ("DEKONT", "TURKIYE", "BANKASI", "BANKACILIK")):
            return lc
    return ""


def _garanti_sender_name(lines: list[str]) -> str:
    # Garanti: "SAYIN" etiketinin sağ sütununda ya da bir alt satırın sağ sütununda isim
    for i, l in enumerate(lines):
        if re.search(r"\bSAYIN\b", l):
            after = _clean_name(l.split("SAYIN", 1)[1])
            if _NAME_RE.match(after):
                return after
            # bir sonraki satırların SAĞ sütunu (isim, adresten önce)
            for j in range(i + 1, min(len(lines), i + 3)):
                cols = re.split(r"\s{2,}", lines[j].strip())
                right = cols[-1].strip() if len(cols) > 1 else cols[0].strip()
                if _NAME_RE.match(right):
                    return right
    return ""


_VK_LABELS = r"(ALICI AD SOYAD/UNVAN|ALICI AD SOYAD|GÖNDEREN AD SOYAD ?/?|GONDEREN AD SOYAD ?/?|GONDEREN AD|GÖNDEREN AD|ALICI HESAP NO|GONDEREN HESAP NO|İŞLEM TUTARI|MASRAF TUTARI|İŞLEM NO|SORGU NO|ALICI BANKA|UNVAN|/)"


_VK_STOP = {"SOYAD", "UNVAN", "ALICI", "GONDEREN", "GÖNDEREN", "SOYAD/UNVAN", "AD", "HESAP", "NO", "TL", "TRY", "BANKA"}


def _vk_strip(s: str) -> str:
    s = re.sub(_VK_LABELS, " ", s)
    s = re.sub(r"TR\d[\d ]+", " ", s)
    s = re.sub(r"[\d.]+,\d{2}", " ", s)     # tutarlar
    s = re.sub(r"\b(TL|TRY|USD|EUR)\b", " ", s)
    s = re.sub(r"\d{3,}", " ", s)
    s = _clean_name(s)
    # tamamen durak-kelimelerden oluşuyorsa geçersiz say
    toks = [t for t in s.split() if t.upper() not in _VK_STOP]
    if not toks:
        return ""
    return " ".join(s.split())


def _vakif_names(text: str):
    """VakıfBank çok sütunlu düzeninden (gönderici, alıcı) isimlerini çıkarır."""
    sender = receiver = ""
    # 1) Aynı satırda 'X ALICI AD SOYAD/UNVAN Y' kalıbı
    m = re.search(r"(.*?)ALICI AD SOYAD/UNVAN(.*)", text)
    if m:
        left = _vk_strip(m.group(1).splitlines()[-1] if m.group(1) else "")
        right = _vk_strip(m.group(2).splitlines()[0] if m.group(2) else "")
        if _NAME_RE.match(right.upper()) and len(right) >= 4:
            receiver = right
        if _NAME_RE.match(left.upper()) and len(left) >= 4:
            sender = left
    # 2) GÖNDEREN etiketinden sonra gelen isim
    if not sender:
        gm = re.search(r"GÖNDEREN AD SOYAD ?/?|GONDEREN AD SOYAD ?/?|GONDEREN AD|GÖNDEREN AD", text)
        if gm:
            tail = text[gm.end():]
            for ln in tail.splitlines():
                cand = _vk_strip(ln)
                if _NAME_RE.match(cand.upper()) and len(cand) >= 5:
                    sender = cand
                    break
    return sender, receiver


def _vakif_value(text: str, labels: list[str]) -> str:
    """
    VakıfBank çok sütunlu düzende değer, etiketle aynı satırda ya da bir önceki/
    sonraki satırda olabilir. Etiketi bul, aynı satırdaki diğer metni ya da
    komşu satırı isim olarak dene.
    """
    lines = text.splitlines()
    for i, l in enumerate(lines):
        for lab in labels:
            if lab in l:
                rest = l.replace(lab, " ").strip()
                # aynı satırda başka etiket kalıntısını temizle
                rest = re.sub(r"(İŞLEM TUTARI|MASRAF TUTARI|GONDEREN HESAP NO|ALICI HESAP NO|SORGU NO|İŞLEM NO).*", "", rest).strip()
                cand = _clean_name(rest)
                if _NAME_RE.match(cand.upper()) and len(cand) >= 4:
                    return cand
                # komşu satırlar
                for j in (i - 1, i + 1, i + 2):
                    if 0 <= j < len(lines):
                        c2 = _clean_name(lines[j])
                        c2 = re.sub(r"(İŞLEM|MASRAF|SORGU|IBAN|HESAP|TR\d).*", "", c2).strip()
                        if _NAME_RE.match(c2.upper()) and len(c2) >= 5 and "UNVAN" not in c2.upper():
                            return c2
    return ""


def _detect_garanti_kind(text: str) -> str:
    if "HESAPTAN FAST" in text:
        return "HESAPTAN FAST"
    if "HESAPTAN EFT" in text:
        return "HESAPTAN EFT"
    if "HAVALE" in text:
        return "HAVALE"
    return "Dekont"


# İşlem tarih/saatini taşıyan olası etiketler (en özgüllü/uzun önce).
_DT_LABELS = [
    "İşlem Tarihi ve Saati", "İşlem Tarih ve Saati", "İşlem Tarihi/Saati",
    "Tarih ve Saat", "Tarih/Saat", "İşlem Zamanı", "Dekont Tarihi",
    "Düzenlenme Tarihi", "Valör Tarihi", "İşlem Tarihi", "Tarih",
]


def _normalize_datetime(current: str, text: str) -> str:
    """İşlem tarih/saatini temizler ve tamamlar.

    - Baştaki ':' / boşluk gibi çöpü temizler (DATE_RE ile net tarih ayıklar).
    - Tarih var ama saat yoksa, metinde aynı tarihi izleyen saati (HH:MM[:SS]) ekler.
    - Hiç tarih yoksa, tarih+saat taşıyan bir etiketten ya da metindeki ilk tarihten türetir.
    Mevcut doğru (tarih+saat) değeri asla bozmaz.
    """
    cur = (current or "").strip()
    m = DATE_RE.search(cur)
    cur_clean = m.group(0) if m else ""
    if cur_clean and ":" in cur_clean:
        return cur_clean                      # zaten tarih+saat var; yalnızca çöp temizlendi

    # Tarih+saat içeren bir etiket bul (daha zengin bilgi)
    for lab in _DT_LABELS:
        v = _find_label(text, [lab])
        if v:
            mm = DATE_RE.search(v)
            if mm and ":" in mm.group(0):
                return mm.group(0)

    # Mevcut tarih var ama saat yok: metinde bu tarihi izleyen saati ekle
    if cur_clean:
        mm = re.search(re.escape(cur_clean) + r"\s+(\d{2}:\d{2}(?::\d{2})?)", text or "")
        return cur_clean + " " + mm.group(1) if mm else cur_clean

    # Hiç tarih yok: etiketten (yalnızca tarih) ya da metindeki ilk tarihten
    for lab in _DT_LABELS:
        v = _find_label(text, [lab])
        if v:
            mm = DATE_RE.search(v)
            if mm:
                return mm.group(0)
    m2 = DATE_RE.search(text or "")
    return m2.group(0) if m2 else cur


def _generic_extract(ex: Extraction, text: str) -> None:
    # En büyük tutarı işlem tutarı kabul et
    amounts = [(parse_amount(a), a) for a in ex.all_amounts]
    amounts = [(v, a) for v, a in amounts if v is not None]
    if amounts:
        amounts.sort(key=lambda x: x[0], reverse=True)
        ex.amount.value = amounts[0][0]
        ex.amount.text = amounts[0][1]
        cm = CURRENCY_RE.search(text)
        ex.amount.currency = cm.group(1).upper() if cm else ""
    # Tarih
    dm = DATE_RE.search(text)
    if dm:
        ex.transaction.date = dm.group(0)
    # IBAN atama
    if ex.all_ibans:
        ex.sender.iban = ex.all_ibans[0]
        if len(ex.all_ibans) > 1:
            ex.receiver.iban = ex.all_ibans[1]
    # Etiketli alıcı/gönderici dene
    ex.receiver.name = _clean_name(_find_label(text, ["Alıcı", "Alacaklı", "ALACAKLI", "Lehtar"]))
    ex.sender.name = _clean_name(_find_label(text, ["Gönderen", "Gönderici", "Borçlu", "Ad Soyad"]))


def _confidence(ex: Extraction) -> float:
    score = 0.0
    if ex.bank: score += 0.15
    if ex.amount.value is not None: score += 0.30
    if ex.sender.iban: score += 0.15
    if ex.receiver.iban: score += 0.15
    if ex.receiver.name: score += 0.15
    if ex.transaction.date: score += 0.10
    return round(min(score, 1.0), 2)

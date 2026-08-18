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
# Tarih + opsiyonel saat. Tarih-saat ayracı boşluk, 'T' ya da TİRE olabilir
# (Ziraat: '13/08/2026-22:37:16').
DATE_RE = re.compile(r"\b\d{2}[./]\d{2}[./]\d{4}(?:[\sT-]+\d{2}:\d{2}(?::\d{2})?)?")
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
            # Gönderen IBAN'ını KÖRLEMESİNE found[1] atama: alıcı zaten found içindeki bir IBAN'a
            # (ör. found[1]) eşitse gönderen = alıcı çakışması doğar. Bunun yerine ALICI'dan FARKLI
            # ilk adayı ata; farklı aday yoksa boş bırak (kopya IBAN 'aynı banka' yanlış-pozitifi üretir).
            if not ex.sender.iban:
                for _ib in found:
                    if _ib != ex.receiver.iban:
                        ex.sender.iban = _ib
                        break
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
    -> 'Özgür İnci' ve 'ALICI ÜNVANI: Mezher Kaya ALICI IBAN:...' -> 'Mezher Kaya'.

    Eşleşme AKSAN/BÜYÜK-KÜÇÜK DUYARSIZDIR (İ/ı, ş/s ... ve PDF kodlama farkları için);
    değer ORİJİNAL metinden alınır (normalize uzunluk-koruyandır, span aynıdır)."""
    ntext = _norm_tr(text)
    m = re.search(re.escape(_norm_tr(label)) + r"\s*[:：][ \t]*", ntext)
    if not m:
        return ""
    start = m.end()
    nl = text.find("\n", start)                       # satır sonunda kes
    end = nl if nl != -1 else len(text)
    nval = ntext[start:end]
    # bir sonraki etikette kes — etiket önceki kelimeye BİTİŞİK de olabilir
    # (ör. "Tolga ŞengülALICI IBAN"), bu yüzden kelime sınırı aranmaz; düz alt-dizi yeter.
    cut = len(nval)
    for st in stops:
        mm = re.search(re.escape(_norm_tr(st)), nval)
        if mm and mm.start() < cut:
            cut = mm.start()
    return text[start:start + cut].strip(" :：-,")


def _label_values(text: str, label: str, stops: list[str]) -> list[str]:
    """Bir etiketin TÜM tekrarları için değer listesi (iki-sütunlu düzenlerde sol/sağ).
    Ör. Akbank'ta 'Adı Soyadı/Unvan' aynı satırda iki kez geçer -> [gönderici, alıcı]."""
    ntext = _norm_tr(text)
    nlab = _norm_tr(label)
    out = []
    for m in re.finditer(re.escape(nlab) + r"\s*[:：][ \t]*", ntext):
        start = m.end()
        nl = text.find("\n", start)
        end = nl if nl != -1 else len(text)
        nval = ntext[start:end]
        cut = len(nval)
        for st in stops:
            mm = re.search(re.escape(_norm_tr(st)), nval)
            if mm and mm.start() < cut:
                cut = mm.start()
        v = text[start:start + cut].strip(" :：-,")
        if v:
            out.append(v)
    return out


def _row_amount(text: str, label: str) -> float | None:
    """Etiketi içeren satırdaki EN BÜYÜK para tutarını döndürür (tablo satırları için)."""
    nlab = _norm_tr(label)
    for ln in (text or "").splitlines():
        if nlab in _norm_tr(ln):
            vals = [_parse_money_token(m.group(0)) for m in AMOUNT_RE.finditer(ln)]
            vals = [v for v in vals if v is not None]
            if vals:
                return max(vals)
    return None


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
    # ÖNEMLİ: Her banka İHRAÇ EDENİN KENDİ imzasıyla (web adresi/footer/kendine özgü etiket)
    # tanınır. Karşı-taraf banka adı (ör. "ALICI BANKA :Vakıflar Bankası") ve genel alanlar
    # (ör. ETTN — tüm e-Dekontlarda var) tetiklememelidir. Aşağıdaki imzalar ÖNCELİK sırasıyla
    # değerlendirilir; yalnızca BİR banka seçilir (karşılıklı dışlayan).
    # İmzalar İHRAÇÇIYA-ÖZGÜ olmalı: web adresi (footer) ve YALNIZCA ihraç edende geçen
    # kanal/şube ifadeleri. Banka TAM ADLARI (ör. "Yapı ve Kredi Bankası A.Ş.") KULLANILMAZ;
    # çünkü bunlar karşı-tarafta (Alan Banka / Alıcı Banka / KATILIMCI) da geçer ve yanlış
    # bankaya yönlendirir.
    _sig_yapikredi = "yapikredi.com" in low
    # ZİRAAT-ÖZEL SAĞLAMLAŞTIRMA: 'İ'.lower() = 'i̇' (i + U+0307 birleşik nokta) olduğundan ham
    # low'da 'ZİRAAT MOBİL'/'ZİRAAT SÜPER ŞUBE' düz 'ziraat...' imzalarına EŞLEŞMİYORDU; tespit
    # yalnız footer 'ziraatbank.com' ile çalışıyordu (footer okunmazsa Ziraat kaçıyordu). _norm_tr
    # aksanı katlar ama birleşik noktayı bırakır; onu da temizleyip kanal/şube imzalarını güvenilir
    # yap. Bu imzalar ihraççı-özgüdür ('ziraat mobil'/'ziraat süper' yalnız Ziraat'ın KENDİ dekontunda
    # geçer; karşı-tarafta yalnız 'ziraat bankası' ADI geçer, kanal adı değil → çakışma yok).
    _zsig = _norm_tr(low).replace("̇", "")
    _sig_ziraat = ("ziraatbank.com" in low
                   or "ziraat mobil" in _zsig
                   or "ziraat super sube" in _zsig or "ziraat super" in _zsig)
    _sig_isbank = ("isbank.com" in low or ("e-dekont" in low and "doküman numarası" in low))
    _sig_vakif = ("vakifbank.com" in low or ("VAKIFBANK" in up and "İŞLEM BİLGİLERİ" in up))
    _sig_garanti = ("garantibbva" in low or ("HESAPTAN" in up and "GARANTİ" in up))
    # ENPARA-ÖZEL: OCR Türkçe karakterleri bozunca (ş->s, Ü->U) düz-metin etiket imzaları
    # tutmuyordu ve belge yanlışlıkla universal dala düşüp gönderen/alıcı IBAN'ını çakıştırıyordu.
    # En sağlam imza web adresidir (enpara.com — yalnız ihraççıda geçer). Etiket imzalarını da
    # aksan/OCR-duyarsız (_norm_tr) karşılaştır.
    _en = _norm_tr(low)
    _sig_enpara = ("enpara.com" in low
                   or "enpara subesi" in _en
                   or ("qnb.com" not in low and (
                       ("alici unvani" in _en and "eft tutari" in _en)
                       or ("musteri unvani" in _en and "giden fast" in _en))))
    _sig_akbank = ("akbank.com" in low or "akbank direkt" in low)
    _sig_ing = ("ing.com.tr" in low or "ing bank anonim" in low)
    _sig_fiba = ("fibabanka.com" in low or "fibabanka" in _norm_tr(low))
    # QNB: Enpara ile AYNI altyapı/format (Ibtech+iText); QNB markası ayrı etiketlensin.
    # QNB'ye ÖZGÜ imza: web adresi + QNB kanal ifadeleri. DİKKAT: 'QNB Bank' salt-metin olarak
    # Enpara belgelerinde tarihsel dipnotta geçebilir ("Enpara'nın QNB Bank A.Ş. ...") — bu yüzden
    # 'qnb bank' tek başına KULLANILMAZ; yalnız qnb.com / QNB Telefon|İnternet Bankacılığı sayılır.
    _nlow = _norm_tr(low)
    _sig_qnb = ("qnb.com" in low or "qnb telefon bankaciligi" in _nlow
                or "qnb internet bankaciligi" in _nlow)
    # Halkbank: web adresi ihracçı-özgüdür. DİKKAT: "Halk Bankası" ADI karşı-tarafta (ALICI BANKA)
    # da geçer; bu yüzden yalnızca 'halkbank.com' imza sayılır.
    _sig_halk = ("halkbank.com" in low)
    # PTT (PttBank): footer web adresi + kuruma özgü kanal/başlık ifadeleri ihracçı-özgüdür.
    # 'PTT'/'Posta ve Telgraf' ADI karşı-tarafta pek geçmez; yine de en güvenli imza web adresidir.
    _sig_ptt = ("pttbank.ptt.gov.tr" in low or "pttbank internet bankaciligi" in _nlow
                or "posta ve telgraf teskilati" in _nlow)
    # Kuveyt Türk Katılım: YALNIZCA footer web adresi ihracçı-özgüdür. "Kuveyt Türk" ADI
    # karşı-tarafta (Alıcı Banka) geçebileceğinden ad tek başına KULLANILMAZ.
    _sig_kuveyt = ("kuveytturk.com" in _nlow)
    # DenizBank: footer web adresi + kuruma-özgü kanal (MobilDeniz) ihracçı-özgü ("DenizBank" adı
    # karşı-tarafta da geçebilir, bu yüzden salt ad kullanılmaz).
    _sig_deniz = ("denizbank.com" in low or "mobildeniz" in _nlow)
    # Ziraat Dinamik Banka: AYRI dijital banka (ZİRAAT DİNAMİK BANKA A.Ş., ziraatdinamik.com.tr,
    # IBAN kodu 00160). Klasik Ziraat'ten ÖNCE değerlendirilir. İmza ihracçıya-özgü olmalı:
    # web adresi ya da KENDİ kanalı ('Ziraat Dinamik Mobil'); salt "Ziraat Dinamik" ADI karşı-
    # tarafta (Alan Banka) geçebileceğinden tek başına kullanılmaz.
    # ZİRAAT DİNAMİK-ÖZEL: aynı 'İ' birleşik-nokta sorunu; 'ziraat dinamik mobil' imzasını da
    # nokta-temizlenmiş (_zsig) biçimde ara ki İ'li metinde web adresi olmadan da tanınsın.
    _sig_ziraatdinamik = ("ziraatdinamik.com" in _nlow or "ziraat dinamik mobil" in _zsig)
    issuer = ("yapikredi" if _sig_yapikredi
              else "ziraatdinamik" if _sig_ziraatdinamik else "ziraat" if _sig_ziraat
              else "isbank" if _sig_isbank else "vakif" if _sig_vakif
              else "akbank" if _sig_akbank else "ing" if _sig_ing
              else "fiba" if _sig_fiba else "qnb" if _sig_qnb else "halk" if _sig_halk
              else "ptt" if _sig_ptt
              else "kuveyt" if _sig_kuveyt else "deniz" if _sig_deniz
              else "garanti" if _sig_garanti else "enpara" if _sig_enpara else "")
    is_yapikredi = issuer == "yapikredi"
    is_ziraat = issuer == "ziraat"
    is_isbank = issuer == "isbank"
    is_vakif = issuer == "vakif"
    is_garanti = issuer == "garanti"
    is_enpara = issuer == "enpara"
    is_akbank = issuer == "akbank"
    is_ing = issuer == "ing"
    is_fiba = issuer == "fiba"
    is_qnb = issuer == "qnb"
    is_halk = issuer == "halk"
    is_ptt = issuer == "ptt"
    is_kuveyt = issuer == "kuveyt"
    is_deniz = issuer == "deniz"
    is_ziraatdinamik = issuer == "ziraatdinamik"

    ex.bank = {"yapikredi": "Yapı ve Kredi Bankası", "ziraat": "T.C. Ziraat Bankası",
               "isbank": "Türkiye İş Bankası", "vakif": "VakıfBank",
               "garanti": "Garanti BBVA", "enpara": "Enpara.com (QNB)",
               "akbank": "Akbank T.A.Ş.", "ing": "ING Bank A.Ş.",
               "fiba": "Fibabanka A.Ş.", "qnb": "QNB Bank A.Ş.",
               "halk": "Türkiye Halk Bankası",
               "ptt": "PTT (PttBank)",
               "kuveyt": "Kuveyt Türk Katılım", "deniz": "DenizBank",
               "ziraatdinamik": "Ziraat Dinamik Banka"}.get(issuer, "")

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
        # Alıcı adı: FAST'ta 'Alıcı Isim\Unvan'; HAVALE (Para Aktarma) formatında iki-sütunlu
        # 'Alıcı Hesap : <ad>' (ad maskeli olabilir: 'YU**** EM**** SO****').
        _ISTOP = ["Alıcı Hesap", "Alıcı IBAN", "Alıcı Banka", "IBAN", "Sorgu", "Aktarılan",
                  "Havale", "Toplam", "Ücret", "İşlem Türü", "Açıklama"]
        ex.receiver.name = _clean_name(
            _find_label(joined, ["Alıcı Isim.?Unvan", "Alıcı İsim.?Unvan", "Alıcı Ad"]))
        if not ex.receiver.name:
            ex.receiver.name = _clean_name(_after_label(joined, "Alıcı Hesap", _ISTOP))
        ex.receiver.iban = banks.normalize_iban(_find_label(joined, ["Alıcı IBAN"]))
        ex.receiver.bank = _find_label(joined, ["Alıcı Banka"])
        # Tutar: FAST 'İşlem Tutarı'; HAVALE 'Aktarılan Tutar'
        amt_txt = _find_label(joined, ["İşlem Tutarı"]) or _find_label(joined, ["Aktarılan Tutar"])
        ex.amount.value = parse_amount(amt_txt)
        ex.amount.text = amt_txt
        cm = CURRENCY_RE.search(amt_txt or "")
        ex.amount.currency = (cm.group(1).upper() if cm else "TRY")
        # Ücret: FAST 'FAST Ücreti ve Vergi'; HAVALE 'Havale Ücreti+Vergi' (vergi dahil -> BSMV eklenmez)
        ex.amount.fee = parse_amount(_find_label(joined, ["FAST Ücreti ve Vergi"])) \
            or parse_amount(_after_label(joined, "Havale Ücreti+Vergi", ["Toplam", "Ücret Tah"])) \
            or parse_amount(_find_label(joined, ["Ücret"]))
        ex.amount.total = parse_amount(_find_label(joined, ["Toplam İşlem Tutarı"])) \
            or parse_amount(_after_label(joined, "Toplam Tutar", ["Ücret Tah", "İşlem Türü", "TRY"]))
        # Havale'de alıcı IBAN etiketsiz: gönderenden farklı (ikinci) IBAN
        if not ex.receiver.iban:
            for ib in ex.all_ibans:
                if ib and ib != ex.sender.iban:
                    ex.receiver.iban = ib
                    break

    # =============================================================
    #  GARANTİ BBVA "HESAPTAN FAST" formatı
    # =============================================================
    elif is_garanti:
        ex.doc_kind = _detect_garanti_kind(joined)
        ex.transaction.type = ex.doc_kind          # İşlem Türü alanı boş kalmasın (HESAPTAN FAST/EFT/HAVALE)
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
        # Alıcı adı: FAST'ta "ALACAKLI : ad"; HAVALE'de "ALACAKLI HESAP : hesapNo ad"
        _rn = _clean_name(_after_label(joined, "ALACAKLI", ["IBAN", "MASRAF", "TUTAR", "SIRA", "BSMV"]))
        if not _rn:
            _alh = _after_label(joined, "ALACAKLI HESAP",
                                ["ALACAKLI IBAN", "IBAN", "MASRAF", "BSMV", "TUTAR", "SIRA"])
            _rn = _clean_name(re.sub(r"^[\d/\s\.]+", "", _alh)) if _alh else ""
        ex.receiver.name = _rn
        ex.receiver.iban = banks.normalize_iban(_find_label(joined, ["ALACAKLI IBAN"]))
        ex.receiver.branch = _clean_name(_after_label(joined, "ALACAKLI ŞUBE",
                                                      ["ALACAKLI HESAP", "ALACAKLI IBAN"]))
        # Tutar: "TUTAR : - 50,00 TL"
        amt_txt = _find_label(joined, ["TUTAR"])
        ex.amount.value = parse_amount(amt_txt)
        ex.amount.text = amt_txt
        cm = CURRENCY_RE.search(amt_txt or "")
        ex.amount.currency = (cm.group(1).upper() if cm else "TL")
        # Ücret: HAVALE'de "MASRAF TOPLAMI" (masraf+BSMV, tüm kesilen ücret) en doğrusu; yoksa
        # FAST "MASRAF : 7,97" / HAVALE "MASRAF TUTARI : 7,98". (MASRAF HESABI bir hesap no'dur.)
        # DİKKAT: "MASRAF TOPLAMI" işlem TOPLAMI DEĞİLDİR (yalnız ücretlerin toplamı) -> total'a yazma.
        # Garanti FAST dekontu ücret toplamını "KOMİSYON TOPLAMI : 8,37 TL" ile basar (MASRAF+BSMV);
        # tek başına "MASRAF : 7,97" eksik kalır. Önce KOMİSYON/MASRAF TOPLAMI, yoksa tekil MASRAF.
        ex.amount.fee = parse_amount(_after_label(joined, "KOMİSYON TOPLAMI", ["YALNIZ", "SIRA", "TUTAR"])) \
            or parse_amount(_after_label(joined, "MASRAF TOPLAMI", ["YALNIZ", "SIRA", "TUTAR"])) \
            or parse_amount(_after_label(joined, "MASRAF TUTARI", ["BSMV", "TUTAR"])) \
            or parse_amount(_after_label(joined, "MASRAF", ["BSMV", "SIRA", "TUTAR", "HESABI"]))
        # NOT: Garanti dekontunda ayrı bir işlem 'TOPLAM'ı yok; TUTAR transfer tutarıdır,
        # 'MASRAF/KOMİSYON TOPLAMI' yalnız ücret toplamıdır -> ex.amount.total'a YAZMA.
        ex.amount.total = None
        # Sıra No (dekont referansı)
        _sira = _find_label(joined, ["SIRA NO"])
        if _sira:
            ex.transaction.receipt_no = re.split(r"\s{2,}|TUTAR", _sira)[0].strip()

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
            # Çok satırlı firma unvanının son satırı komşu alana (MASRAF TUTARI) sızmış olabilir:
            # "MASRAF TUTARI: TİCARET LİMİTED ŞİRKETİ 8,38 TL" -> ismin devamını geri ekle.
            _raw = g.get("_raw", {}) or {}
            _mt = _raw.get("MASRAF TUTARI", "") or ""
            _lead = re.match(r"^\s*(\D+?)\s+\d[\d.]*,\d{2}", _mt)
            if _lead and ex.sender.name and re.search(
                    r"(VE|SANAY|T[İIİ]CARET|L[İIİ]M[İIİ]TED|ŞİRKET|SIRKET|A\.?Ş)",
                    ex.sender.name.upper()):
                ex.sender.name = _clean_name(ex.sender.name + " " + _lead.group(1).strip())
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
    elif is_enpara or is_qnb:
        # Enpara ve QNB AYRI bankalardır; dekont düzenleri (Ibtech+iText) birebir aynı olduğu
        # için ÇIKARIM KODU ortaktır. Banka etiketi/kuralları issuer'a göre ayrışır.
        ex.doc_kind = _detect_garanti_kind(joined)  # GIDEN FAST EFT / HAVALE ...
        rt = rjoined or joined
        _nrt = _norm_tr(rt)
        if "havaleyi alan" in _nrt or "havaleyi gonderen" in _nrt:
            # ---- Enpara HESAPTAN HESABA HAVALE alt-formatı --------------------
            #  Gönderen: 'HAVALEYİ GÖNDEREN HESAP UNVANI:...' (ya da üstteki 'Sayın ...')
            #  Alıcı:    'HAVALEYİ ALAN MUSTERİ UNVANI:...'; IBAN 'HAVALEYİ ALAN HESAP NO:.. IBAN: TR..'
            #  Tutar:    işlem tablosu satırının (hesap IBAN'ı + tutar) sonundaki para jetonu
            _HS = ["HAVALEYİ ALAN", "HAVALEYİ GÖNDEREN", "AÇIKLAMA", "ACIKLAMA",
                   "HESAP NO", "IBAN", "YETKİ", "YETKI", "SIRA NO", "FİŞ NO", "FIS NO"]
            ex.sender.name = _clean_name(
                _after_label(rt, "HAVALEYİ GÖNDEREN HESAP UNVANI", _HS)
                or _after_label(rt, "HAVALEYI GONDEREN HESAP UNVANI", _HS)
                or _after_label(rt, "Sayın", ["TC kimlik", "TC Kimlik", "İşlem", "Şube", "Vergi"]))
            ex.receiver.name = _clean_name(
                _after_label(rt, "HAVALEYİ ALAN MUSTERİ UNVANI", _HS)
                or _after_label(rt, "HAVALEYİ ALAN MÜŞTERİ ÜNVANI", _HS)
                or _after_label(rt, "HAVALEYI ALAN MUSTERI UNVANI", _HS))
            # Alıcı IBAN: 'HAVALEYİ ALAN HESAP NO:.. IBAN: TR..' satırından
            _ral = _after_label(rt, "HAVALEYİ ALAN HESAP NO", ["SIRA NO", "FİŞ NO", "FIS NO"]) \
                or _after_label(rt, "HAVALEYI ALAN HESAP NO", ["SIRA NO", "FIS NO"])
            rm = IBAN_RE.search(_ral or "")
            ex.receiver.iban = banks.normalize_iban(rm.group(0)) if rm else ""
            # Gönderen IBAN: işlem satırındaki (alıcıdan farklı) IBAN
            for ib in ex.all_ibans:
                if ib and ib != ex.receiver.iban:
                    ex.sender.iban = ib
                    break
            # Tutar: IBAN + para jetonu içeren işlem satırı (ilk eşleşen)
            for ln in rt.splitlines():
                if IBAN_RE.search(ln):
                    vals = [_parse_money_token(m.group(0)) for m in AMOUNT_RE.finditer(ln)]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        ex.amount.value = max(vals)
                        ex.amount.text = re.sub(r"\s{2,}", " ", ln.strip())
                        break
            cm = CURRENCY_RE.search(ex.amount.text or rt)
            ex.amount.currency = (cm.group(1).upper() if cm else "TL")
            ex.amount.fee = None
        else:
            # ---- standart EFT/FAST formatı -----------------------------------
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
        # İşlem türü (display): Enpara/QNB'de doc_kind çoğu kez 'Dekont'a düşer ('GİDEN FAST EFT'
        # başlığını _detect_garanti_kind yakalamıyor). Metinden çıkar; İşlem Türü alanı boş kalmasın.
        _lj = _norm_tr(joined).lower()
        if "havaleyi alan" in _lj or "hesaptan hesaba havale" in _lj:
            ex.transaction.type = "Hesaptan Hesaba Havale"
        elif "fast" in _lj:
            ex.transaction.type = "Giden FAST"
        elif "eft" in _lj:
            ex.transaction.type = "Giden EFT"
        else:
            ex.transaction.type = ex.doc_kind or "Dekont"
        # Gönderen/Alıcı IBAN çakışması (vision bitişik müşteri IBAN'ını yanlış okursa gönderen=alıcı
        # olabilir): aynıysa farklı bir IBAN dene, yoksa gönderen IBAN'ı boşalt (yanlış 'aynı banka' üretme).
        if ex.sender.iban and ex.sender.iban == ex.receiver.iban:
            ex.sender.iban = next((ib for ib in ex.all_ibans if ib and ib != ex.receiver.iban), "")
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
        ex.sender.bank = ex.bank or ("QNB Bank A.Ş." if is_qnb else "Enpara.com (QNB)")

    # =============================================================
    #  T.C. ZİRAAT BANKASI (HESAPTAN FAST / EFT / HAVALE)
    # =============================================================
    elif is_ziraat or is_ziraatdinamik:
        # Ziraat ve Ziraat Dinamik AYRI bankalardır; dekont düzeni (BİRİM/HESAP/Gönderen/Alan Banka/
        # Alıcı Hesap/İşlem Tutarı/Fast Sorgu No) neredeyse AYNI olduğundan ÇIKARIM KODU ortaktır.
        # İşlem türü (başlık): Ziraat başlığı 'Hesaptan Hesaba Havale' / 'Hesaptan FAST' /
        # 'Hesaptan EFT' olabilir. _detect_garanti_kind BÜYÜK-HARF 'HAVALE' arıyordu; Ziraat
        # başlığı karışık-harf ('Hesaba Havale') olduğundan yanlışlıkla 'Dekont'a düşüyordu.
        # ZİRAAT-ÖZEL: aksan/büyük-küçük duyarsız algıla (yalnız bu dalda).
        _zt = _norm_tr(joined).lower()
        if "hesaptan fast" in _zt:
            ex.doc_kind = "Hesaptan FAST"
        elif "hesaptan eft" in _zt:
            ex.doc_kind = "Hesaptan EFT"
        elif "hesaba havale" in _zt:
            ex.doc_kind = "Hesaptan Hesaba Havale"
        else:
            ex.doc_kind = _detect_garanti_kind(joined)  # HESAPTAN FAST / EFT / HAVALE
        rt = joined                                  # Ziraat'ta hizalı layout metni daha güvenilir
        # İki Ziraat formatı desteklenir:
        #  (a) HESAPTAN FAST/EFT: 'Gönderen', 'Alıcı', 'Alan Banka', 'İşlem Tutarı'
        #  (b) Hesaptan Hesaba Havale: gönderen adı IBAN satırı sonunda; alıcı 'Alacaklı Adı
        #      Soyadı'/'Alacaklı IBAN', tutar 'Havale Tutarı', ücret 'Komisyon'
        _ZS = ["Alan Banka", "Alacaklı Adı Soyadı", "Alacaklı IBAN", "Alacaklı Hesap",
               "Alacaklı Şube", "Alacaklı Vergi", "Alıcı Hesap", "Alıcı", "Gönderen",
               "İşlem Tutarı", "Havale Tutarı", "Komisyon", "BSMV", "Mesaj Ücreti",
               "Toplam Masraf", "Fast Sorgu No", "Fast Mesaj Kodu", "VALÖR", "İŞLEM YERİ",
               "IBAN", "HESAP NUMARASI", "VERGİ", "SAYIN", "Açıklama"]
        # --- Gönderen adı: 'Gönderen' etiketi ya da GÖNDEREN IBAN satırının sonundaki isim ---
        s_name = _clean_name(_after_label(rt, "Gönderen", _ZS))
        if not s_name:
            iban_line = _after_label(rt, "IBAN", ["HESAP NUMARASI", "VERGİ", "İŞLEM", "Alacaklı"])
            iban_line = IBAN_RE.sub(" ", iban_line or "")
            iban_line = re.sub(r"\bSAYIN\b", " ", iban_line, flags=re.I)
            s_name = _clean_name(iban_line)
        if not s_name:
            # ZİRAAT-ÖZEL: 'Hesaptan Hesaba Havale' formatında 'Gönderen' etiketi YOKTUR; hesap
            # sahibi (=gönderen) sağ sütundaki 'SAYIN' başlığının hemen ALTINDAKİ satırdır.
            # SAYIN'dan sonraki İLK büyük-harf isim satırını al (adres/rakam satırından önce).
            _sm = re.search(r"\bSAYIN\b[ \t]*\n?[ \t]*([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ.\-' ]{2,})", rt, re.I)
            if _sm:
                s_name = _clean_name(_sm.group(1))
        ex.sender.name = s_name
        # --- Alıcı adı: 'Alacaklı Adı Soyadı' (havale) ya da 'Alıcı' (FAST) ---
        ex.receiver.name = _clean_name(_after_label(rt, "Alacaklı Adı Soyadı", _ZS)
                                       or _after_label(rt, "Alacaklı Adı Soyadı", _ZS)
                                       or _after_label(rt, "Alıcı", _ZS))
        # ZİRAAT-ÖZEL: FAST'te 'Alıcı : <ad>' değeri satır sonunda kesilip SOYAD bir alt satıra
        # kayabilir (ör. 'Alıcı : Yasin' \n 'epaydın'). Alıcı adı TEK sözcükse ve hemen sonraki
        # satır bir etiket/rakam DEĞİL, isim-devamı bir sözcükse onu ekle (soyad düşmesin).
        if ex.receiver.name and " " not in ex.receiver.name.strip():
            _cm = re.search(r"Al[ıi]c[ıi]\s*[:：]\s*" + re.escape(ex.receiver.name)
                            + r"[ \t]*\n[ \t]*([A-Za-zÇĞİÖŞÜçğıöşü][A-Za-zÇĞİÖŞÜçğıöşü.\-']{1,30})", rt)
            if _cm:
                _cont = _cm.group(1).strip()
                # stop sözcükleri NORMALİZE (aksan/İ-katlamalı) biçimde; _norm_tr ş->s, İ->i, ı->i ...
                _stop_w = {"islem", "komisyon", "toplam", "alan", "fast", "bsmv", "mesaj", "tutar",
                           "hesap", "valor", "gonderen", "iban", "sube", "aciklama", "sayin",
                           "vergi", "banka", "alici", "alacakli", "havale"}
                if _norm_tr(_cont) not in _stop_w and not _cont[0].isdigit():
                    ex.receiver.name = _clean_name(ex.receiver.name + " " + _cont)
        ex.receiver.bank = _clean_name(_after_label(rt, "Alan Banka", _ZS))
        ex.receiver.branch = _clean_name(_after_label(rt, "Alacaklı Şube", _ZS))
        # IBAN'lar: alıcı 'Alacaklı IBAN'/'Alıcı Hesap', gönderen üstteki 'IBAN :'
        r_ib = IBAN_RE.search(_after_label(rt, "Alacaklı IBAN", _ZS) or "") \
            or IBAN_RE.search(_after_label(rt, "Alıcı Hesap", ["Alıcı", "Alan Banka"]) or "")
        ex.receiver.iban = banks.normalize_iban(r_ib.group(0)) if r_ib else ""
        s_ib = _first_iban_after(rt, ["IBAN"])
        ex.sender.iban = s_ib or (ex.all_ibans[0] if ex.all_ibans else "")
        if ex.sender.iban and ex.sender.iban == ex.receiver.iban:
            for ib in ex.all_ibans:
                if ib != ex.receiver.iban:
                    ex.sender.iban = ib
                    break
        # Tutar: 'İşlem Tutarı' (FAST) ya da 'Havale Tutarı' (havale)
        _amt_txt = _after_label(rt, "İşlem Tutarı", _ZS) or _after_label(rt, "Havale Tutarı", _ZS)
        ex.amount.value = parse_amount(_amt_txt)
        cur = CURRENCY_RE.search(_amt_txt or "")
        ex.amount.currency = (cur.group(1).upper() if cur else "TRY")
        # Ücret: 'Toplam Masraf' (FAST) ya da 'Komisyon' (havale)
        ex.amount.fee = parse_amount(_after_label(rt, "Toplam Masraf", _ZS)) \
            or parse_amount(_after_label(rt, "Komisyon", _ZS))
        # toplam çekilen ("Hesabınızdan 15.640,00 TL ... Çekilmiştir")
        tm = re.search(r"Hesab[ıi]n[ıi]zdan\s+(" + AMOUNT_RE.pattern + r")", rt)
        if tm:
            ex.amount.total = parse_amount(tm.group(1))
        # Zaman / referans / kanal
        ex.transaction.date = _find_label(rt, ["İŞLEM TARİHİ", "ISLEM TARIHI"])
        ex.transaction.value_date = _find_label(rt, ["VALÖR", "VALOR"])
        ex.transaction.ref_no = _find_label(rt, ["Fast Sorgu No", "Sorgu No"])
        ex.transaction.channel = _find_label(rt, ["İŞLEM YERİ", "ISLEM YERI"])
        ex.transaction.type = ex.doc_kind
        # Şube/birim: Ziraat 'ŞUBE KODU/ADI'; Ziraat Dinamik 'BİRİM KODU/ADI'
        ex.sender.branch = _find_label(rt, ["ŞUBE KODU/ADI", "SUBE KODU/ADI", "ŞUBE ADI",
                                            "BİRİM KODU/ADI", "BIRIM KODU/ADI"])
        # ZİRAAT-ÖZEL: sol sütundaki 'ŞUBE KODU/ADI' değeri ile sağ sütunun başlığı 'SAYIN'
        # aynı satıra binince değere 'SAYIN' (ve sonrası) sızabilir; şube değerinden temizle.
        if ex.sender.branch:
            ex.sender.branch = re.sub(r"\s+SAYIN\b.*$", "", ex.sender.branch, flags=re.I).strip()
        ex.sender.account_no = _find_label(rt, ["HESAP NUMARASI"])
        ex.sender.tckn = _find_label(rt, ["VERGİ KİMLİK NO", "TC KİMLİK", "VERGI KIMLIK NO"])
        # ZİRAAT-ÖZEL: bu formatta 'VERGİ KİMLİK NO' çoğu bireysel dekontta BOŞTUR. Alan boşken
        # _find_label bir sonraki satırı ('İŞLEM TARİHİ ...') ya da sağ sütundaki adresin posta/kapı
        # rakamlarını (ör. 'No:111/1 54100' -> '1114154100') bu alana sızdırabilir. Gerçek kimlik
        # SADECE rakam/boşluk/maske(*) içerir; harf/etiket sözcüğü içeriyorsa sızıntıdır -> temizle.
        # Ayrıca maskesiz ama geçersiz sağlama (TCKN/VKN) da adres sızıntısıdır -> temizle.
        # Böylece yanlış 'geçersiz VKN -> sahte' üretilmez.
        if ex.sender.tckn:
            _idraw = ex.sender.tckn.strip()
            if "*" in _idraw:
                # Maskeli kimlik (ör. '14***16', '39*******34'): DOĞRULANAMAZ ama meşrudur; kısa da
                # olsa OLDUĞU GİBİ tut. Yalnız rakam/*/boşluk dışı karakter (harf/etiket sızıntısı) varsa temizle.
                if not re.fullmatch(r"[0-9*\s]+", _idraw):
                    ex.sender.tckn = ""
            else:
                # Maskesiz: gerçek kimlik yalnız 10-11 haneli rakamdır. Harf/etiket sızıntısı
                # ('İŞLEM TARİHİ ...') ya da adres rakamı ('1114154100') sağlama tutmaz -> temizle.
                _d = re.sub(r"\s", "", _idraw)
                if not (_d.isdigit() and len(_d) in (10, 11) and banks.id_valid(_d) is not False):
                    ex.sender.tckn = ""
        ex.sender.bank = "Ziraat Dinamik Banka" if is_ziraatdinamik else "T.C. Ziraat Bankası"

    # =============================================================
    #  YAPI VE KREDİ BANKASI (FAST GÖNDERİMİ / EFT e-Dekont)
    # =============================================================
    elif is_yapikredi:
        ex.doc_kind = "e-Dekont"
        rt = joined                                  # hizalı layout metni
        _YS = ["GÖNDEREN ADI", "ALICI ADI", "ALACAKLI ADI", "ALICI BANKA", "ALICI ŞUBE",
               "ALICI HESAP", "ALACAKLI HESAP", "ALICI TCKN", "GÖNDEREN HESAP", "HESAP NUMARASI",
               "İŞLEM REF", "SIRA NO", "PERSONEL", "BELGE", "VALÖR", "DEKONT TİPİ", "SENARYO",
               "ETTN", "GİDEN FAST TUTARI", "ISLEM TUTARI", "İŞLEM TUTARI", "VERGİ", "VERGI",
               "KOMİSYON", "NET TUTAR", "DÖVİZ", "DÖVIZ", "TOPLAM TAHSILAT", "ÖDEMENİN",
               "MESAJ TÜRÜ", "SORGU NO", "KOLAY ADRES", "AÇIKLAMA", "MÜŞTERİ NO", "IBAN",
               "TCKN", "VD", "VKN"]

        def _yk_amt(lbl):
            raw = _after_label(rt, lbl, _YS)
            mm = re.search(r"-?\d[\d.,]*", raw or "")
            v = _parse_money_token(mm.group(0)) if mm else None
            return abs(v) if v is not None else None

        # Taraflar. Alıcı adı: 'ALICI ADI' (FAST) ya da 'ALACAKLI ADI' (HESAPTAN HESABA HAVALE).
        # Bu formatta ALICI ADI maskeli olabilir: 'BA***** AŞ*****' -> olduğu gibi al (eksik değil).
        ex.sender.name = _clean_name(_after_label(rt, "GÖNDEREN ADI", _YS))
        if not ex.sender.name:
            # e-Dekont HAVALE-BORÇ: hesap sahibi adı altta 'AD SOYAD Ticari Unvan:' önünde
            _sm = re.search(r"([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ.\- ]{3,}?)\s+Ticari Unvan", rt)
            ex.sender.name = _clean_name(_sm.group(1)) if _sm else ""
        ex.receiver.name = _clean_name(_after_label(rt, "ALACAKLI ADI", _YS)
                                       or _after_label(rt, "ALICI ADI", _YS))
        ex.receiver.bank = _clean_name(_after_label(rt, "ALICI BANKA", _YS))
        # IBAN'lar. Alıcı: 'ALICI HESAP' (FAST) ya da 'ALACAKLI HESAP :.../IBAN:TR..' (havale)
        r_ib = IBAN_RE.search(_after_label(rt, "ALACAKLI HESAP", _YS) or "") \
            or IBAN_RE.search(_after_label(rt, "ALICI HESAP", _YS) or "")
        ex.receiver.iban = banks.normalize_iban(r_ib.group(0)) if r_ib else ""
        s_ib = IBAN_RE.search(_after_label(rt, "GÖNDEREN HESAP NO", _YS) or "") \
            or IBAN_RE.search(_find_label(rt, ["IBAN NO", "IBAN"]) or "")
        ex.sender.iban = banks.normalize_iban(s_ib.group(0)) if s_ib else \
            (ex.all_ibans[0] if ex.all_ibans else "")
        if ex.sender.iban == ex.receiver.iban:
            for ib in ex.all_ibans:
                if ib != ex.receiver.iban:
                    ex.sender.iban = ib
                    break
        # Tutar ve masraf kalemleri (negatif/ondalık). Etiket iki formatta farklı:
        #  FAST: 'GİDEN FAST TUTARI', 'KOMİSYON', 'VERGİ', 'TOPLAM TAHSILAT TUTARI'
        #  HAVALE-BORÇ: 'ISLEM TUTARI', 'KOMİSYON TUTARI', 'VERGI/FON', 'NET TUTAR'
        ex.amount.value = _yk_amt("GİDEN FAST TUTARI") or _yk_amt("ISLEM TUTARI") \
            or _yk_amt("İŞLEM TUTARI")
        ex.amount.currency = (_after_label(rt, "DÖVİZ CİNSİ", _YS) or "TL").split()[0] if \
            _after_label(rt, "DÖVİZ CİNSİ", _YS) else "TL"
        _kom = _yk_amt("KOMİSYON TUTARI") or _yk_amt("KOMİSYON")
        _ver = _yk_amt("VERGI/FON") or _yk_amt("VERGİ/FON") or _yk_amt("VERGİ") or _yk_amt("VERGI")
        if _kom is not None or _ver is not None:
            ex.amount.fee = round((_kom or 0) + (_ver or 0), 2)
        ex.amount.total = _yk_amt("TOPLAM TAHSILAT TUTARI") or _yk_amt("NET TUTAR")
        # Zaman / referans / kanal / kimlik
        ex.transaction.date = _find_label(rt, ["İŞLEM TARİHİ", "ISLEM TARIHI"])
        ex.transaction.value_date = _find_label(rt, ["VALÖR", "VALOR"])
        ex.transaction.ref_no = _find_label(rt, ["İŞLEM REF", "ISLEM REF"])
        ex.transaction.document_no = _find_label(rt, ["BELGE NUMARASI"])
        ex.transaction.ettn = _find_label(rt, ["ETTN"])
        ex.transaction.type = _find_label(rt, ["DEKONT TİPİ", "MESAJ TÜRÜ"])
        ex.transaction.channel = _clean_name(_after_label(rt, "ÖDEMENİN KAYNAĞI", _YS))
        ex.sender.customer_no = _find_label(rt, ["MÜŞTERİ NO"])
        ex.sender.bank = "Yapı ve Kredi Bankası"

    # =============================================================
    #  AKBANK (EFT/HAVALE dekontu — iki sütunlu GÖNDERİCİ | ALICI)
    # =============================================================
    elif is_akbank:
        ex.doc_kind = _detect_garanti_kind(joined)
        rt = joined
        _AS = ["Adı Soyadı", "Adres", "Alacaklı", "Borçlu", "Müşteri No", "Karşı Şube",
               "VKN", "Vergi", "Hesap No", "TUTAR", "İşlem"]
        # İki sütunlu 'Adı Soyadı/Unvan' -> [gönderici, alıcı]
        names = _label_values(rt, "Adı Soyadı/Unvan", _AS) or _label_values(rt, "Adı Soyadı", _AS)
        if names:
            ex.sender.name = _clean_name(names[0])
            if len(names) > 1:
                ex.receiver.name = _clean_name(names[1])
        # IBAN'lar: alıcı 'Alacaklı Hesap No :TR..', gönderen serbest TR.. (kendi satırında)
        r_ib = IBAN_RE.search(_after_label(rt, "Alacaklı Hesap No", _AS) or "")
        ex.receiver.iban = banks.normalize_iban(r_ib.group(0)) if r_ib else ""
        for ib in ex.all_ibans:
            if ib != ex.receiver.iban:
                ex.sender.iban = ib
                break
        # Tutarlar: TUTAR BİLGİLERİ borç/alacak defterinden.
        #  - Transfer principal ana satırda (MEVDUAT/ŞCH), ücretler ayrı satırlarda
        #    (HVL/HAVALE/EFT ÜCRETİ + KOMİSYON + BSMV) — tutarları satırın sağ sütununda.
        #  - Havale formatında ücret satırının etiketi 'HVL ÜCRETİ-MBL' olabilir.
        _kom = _row_amount(rt, "KOMİSYON") or 0
        _bsmv = _row_amount(rt, "BSMV") or 0
        _hvl = (_row_amount(rt, "HVL ÜCRETİ") or _row_amount(rt, "HAVALE ÜCRETİ")
                or _row_amount(rt, "EFT ÜCRETİ") or _row_amount(rt, "İŞLEM ÜCRETİ") or 0)
        _fee = round(_kom + _bsmv + _hvl, 2)
        ex.amount.fee = _fee if _fee else None
        ex.amount.total = _row_amount(rt, "TOPLAM")
        # Transfer tutarı: ŞCH satırı; yoksa TOPLAM − ücretler (masraf çıkarılmış net tutar).
        _transfer = _row_amount(rt, "ŞCH")
        if _transfer is None and ex.amount.total is not None:
            _transfer = round(ex.amount.total - _fee, 2)
        ex.amount.value = _transfer
        ex.amount.currency = "TL"
        # Zaman / referans / kimlik
        ex.transaction.date = _find_label(rt, ["İşlem Tarihi/Saati", "İşlem Tarihi"])
        ex.transaction.value_date = _find_label(rt, ["Valör"])
        ex.transaction.ref_no = _after_label(rt, "Referans", ["İşlemi", "Valör"])
        ex.sender.branch = _find_label(rt, ["Düzenleyen Şube"])
        ex.sender.customer_no = _find_label(rt, ["Müşteri No"])
        ex.sender.tckn = _find_label(rt, ["İşlemi Yapan TCKN"])
        ex.sender.account_no = _find_label(rt, ["Borçlu Hesap No"])
        ex.sender.bank = "Akbank T.A.Ş."

    # =============================================================
    #  ING BANK (FAST/EFT dekontu — alıcı bilgisi açıklamaya gömülü)
    # =============================================================
    elif is_ing:
        ex.doc_kind = _detect_garanti_kind(joined)
        rt = joined
        # Gönderen (hesap sahibi): "Sayın KAYRA TANRIKULU" (aynı satır) ya da
        # "KULLANILAN HESAP :TANRIKULU KAYRA". Satır atlamamak için [^\n]+ kullanılır.
        sm = re.search(r"Say[ıi]n\s+([^\n]+)", rt)
        _sraw = re.split(r"\s{2,}", sm.group(1).strip())[0] if sm else ""
        ex.sender.name = _clean_name(_sraw) or \
            _clean_name(_after_label(rt, "KULLANILAN HESAP", ["FAST", "TOPLAM", "Açıklama"]))
        # Tutar (US biçimi: 10,000.00)
        ex.amount.value = parse_amount(_find_label(rt, ["FAST TUTARI", "İşlem Tutarı", "Tutar"]))
        ex.amount.total = parse_amount(_find_label(rt, ["TOPLAM", "Toplam"]))
        ex.amount.currency = "TL"
        # Açıklamaya gömülü alıcı: "... Sorgu No:XXXX <IBAN> <Alıcı Banka A.Ş.> <Alıcı Ad Soyad>"
        acik = ""
        for ln in rt.splitlines():
            if "sorgu no" in _norm_tr(ln) and IBAN_RE.search(ln):
                acik = ln
                break
        if not acik:
            acik = _after_label(rt, "Açıklama", []) or ""
        sq = re.search(r"[Ss]orgu\s*No\s*[:：]?\s*(\d{6,})", acik)
        ex.transaction.ref_no = sq.group(1) if sq else _find_label(rt, ["Sorgu No"])
        rib = IBAN_RE.search(acik)
        if rib:
            ex.receiver.iban = banks.normalize_iban(rib.group(0))
            tail = acik[rib.end():].strip()
            mb = re.match(r"(.+?A\.?\s?Ş\.?)\s+(.+)", tail)
            if mb:
                ex.receiver.bank = _clean_name(mb.group(1))
                ex.receiver.name = _clean_name(mb.group(2))
            else:
                ex.receiver.name = _clean_name(tail)
        # Zaman: işlem tarihi (içerik) — 'İşlem Tarihi :13/08/2026'
        ex.transaction.date = _find_label(rt, ["İşlem Tarihi"])
        ex.transaction.document_no = _find_label(rt, ["Dekont No"])
        ex.sender.tckn = _find_label(rt, ["Vergi No"])
        ex.sender.bank = "ING Bank A.Ş."

    # =============================================================
    #  FİBABANKA E-Dekont (iki-sütun etiket/değer; alıcı açıklamaya gömülü)
    # =============================================================
    elif is_fiba:
        ex.doc_kind = "E-Dekont"
        rt = rjoined or joined                # okuma-sırası metni bu formatta daha temiz
        low_rt = rt.lower()
        # Tarih + saat -> tek işlem zamanı
        _d = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", rt)
        _tm = re.search(r"Saat\s*[:：]\s*(\d{2}:\d{2}(?::\d{2})?)", rt, re.I)
        ex.transaction.date = ((_d.group(1) if _d else "")
                               + ((" " + _tm.group(1)) if _tm else "")).strip()
        # Gönderen adı: 'FULL NAME' etiketinin altındaki değer (aynı satırda tarih olabilir)
        m = re.search(r"FULL NAME[^\n]*\n\s*(?:\d{2}/\d{2}/\d{4}\s+)?([^\n]+)", rt)
        ex.sender.name = _clean_name(m.group(1)) if m else ""
        # Hesap no + valör (aynı değer satırında yan yana)
        m = re.search(r"ACCOUNT NUMBER[^\n]*\n\s*(\d{4,})", rt)
        ex.sender.account_no = m.group(1) if m else ""
        m = re.search(r"VALUE DATE[^\n]*\n\s*(?:\d+\s+)?(\d{2}/\d{2}/\d{4})", rt)
        ex.transaction.value_date = m.group(1) if m else ""
        # Şube (BRANCH etiketinin altındaki değer; araya VERGİ NO etiketi girebilir)
        m = re.search(r"BRANCH\s*\n(?:[^\n]*TAX NUMBER[^\n]*\n)?\s*([^\n]+)", rt)
        ex.sender.branch = _clean_name(m.group(1)) if m else ""
        ex.transaction.channel = ex.sender.branch
        # Dekont no (00100-639758365) + referans
        m = re.search(r"\b(\d{4,6}-\d{6,})\b", rt)
        ex.transaction.document_no = m.group(1) if m else ""
        ex.transaction.receipt_no = ex.transaction.document_no
        m = re.search(r"Referans[ıiİI]?\s*[:：]\s*(\d{6,})", rt, re.I)
        ex.transaction.ref_no = m.group(1) if m else ""
        # Tutar: '(-)TRY 10,000.00'
        am = re.search(r"\(-\)\s*(TRY|TL|USD|EUR|GBP)\s*([0-9][0-9.,]*)", rt, re.I) \
            or re.search(r"\b(TRY|TL|USD|EUR|GBP)\s*([0-9][0-9.,]*)", rt)
        if am:
            ex.amount.currency = am.group(1).upper()
            ex.amount.value = _parse_money_token(am.group(2))
            ex.amount.text = am.group(0).strip()
        # Alıcı adı: 'ALICI: <ad> -' ('ALICI IBAN' değil)
        m = re.search(r"ALICI\s*[:：]\s*(?!IBAN)([^\-\n]+?)\s*-", rt, re.I)
        ex.receiver.name = _clean_name(m.group(1)) if m else ""
        # Alıcı banka: 'BANKAADI:<banka> -'
        m = re.search(r"BANKAADI\s*[:：]\s*([^\-\n]+)", rt, re.I)
        ex.receiver.bank = _clean_name(m.group(1)) if m else ""
        # Alıcı IBAN: 'ALICI IBAN:TR...' (araya \n girebilir)
        m = re.search(r"ALICI\s*IBAN\s*[:：]\s*(TR[0-9 ]{20,34})", rt, re.I)
        if m:
            ex.receiver.iban = banks.normalize_iban(m.group(1))
        elif ex.all_ibans:
            ex.receiver.iban = ex.all_ibans[0]
        # Gönderen IBAN: TR83 ... (Fibabanka); son 2 hane ayrı satıra düşmüş olabilir ->
        # Türk IBAN'ının son haneleri hesap numarasıdır; hesap no ile tamamla.
        sm = re.search(r"TR\d{2}(?:[ ]?\d{4}){5}(?:[ ]?\d{2})?", rt)
        if sm:
            cand = re.sub(r"\s", "", sm.group(0)).upper()
            if len(cand) < 26 and ex.sender.account_no:
                need = 26 - len(cand)
                acc = ex.sender.account_no
                if need <= len(acc) and cand.endswith(acc[:-need] if len(acc) > need else acc):
                    cand = cand + acc[-need:]
            if len(cand) == 26 and cand != banks.normalize_iban(ex.receiver.iban):
                ex.sender.iban = banks.normalize_iban(cand)
        ex.transaction.type = "GİDEN FAST" if "giden fast" in low_rt else \
            ("GELEN FAST" if "gelen fast" in low_rt else "")
        ex.sender.bank = "Fibabanka A.Ş."

    # =============================================================
    #  HALKBANK (İnternet/Mobil Şube dekontu — tek sütun, net etiketli)
    # =============================================================
    elif is_halk:
        ex.doc_kind = _detect_garanti_kind(joined) or "Dekont"
        rt = joined
        ex.sender.customer_no = _find_label(rt, ["MÜŞTERİ NO"])
        ex.sender.tckn = _find_label(rt, ["TCKN/VKN/YKN", "TCKN"])
        # İşlem tarihi: "15/08/2026 - 01:06" -> tire ayracını boşluğa çevir
        _dt = _after_label(rt, "İŞLEM TARİHİ", ["VALÖR", "BELGE", "MÜŞTERİ"])
        ex.transaction.date = re.sub(r"\s*-\s*", " ", _dt).strip() if _dt else ""
        ex.transaction.value_date = _after_label(rt, "VALÖR TARİHİ", ["BELGE", "IBAN", "ETTN"])
        ex.transaction.channel = _after_label(rt, "İŞLEM KANALI", ["İŞLEM TÜRÜ", "GÖNDEREN"])
        ex.transaction.type = _after_label(rt, "İŞLEM TÜRÜ", ["GÖNDEREN", "AÇIKLAMA"])
        # Gönderen / Alıcı (etiketli, tek satır)
        ex.sender.name = _clean_name(_after_label(rt, "GÖNDEREN", ["IBAN", "ALICI"]))
        ex.sender.iban = banks.normalize_iban(_after_label(rt, "GÖNDEREN IBAN", ["ALICI"]))
        if not ex.sender.iban:
            ex.sender.iban = banks.normalize_iban(_after_label(rt, "IBAN", ["BELGE", "ETTN", "GÖNDEREN"]))
        ex.receiver.name = _clean_name(_after_label(rt, "ALICI", ["IBAN", "BANKA", "ÖDEME"]))
        ex.receiver.iban = banks.normalize_iban(_after_label(rt, "ALICI IBAN", ["ALICI BANKA", "ÖDEME", "BANKA"]))
        ex.receiver.bank = _clean_name(_after_label(rt, "ALICI BANKA", ["ÖDEME", "SORGU", "AÇIKLAMA"]))
        ex.transaction.description = _after_label(rt, "ÖDEME AMACI", ["SORGU", "İŞLEM TUTARI"])
        ex.transaction.ref_no = _find_label(rt, ["SORGU NO"])
        # BİMREF-SERİSIRANO: M-2026-08-15-01.06.10.075702  (dekont referansı)
        _sr = re.search(r"SER[İIİ]?S[İIİ]?RANO\s*[:：]?\s*([A-Z0-9\.\-]{10,})", rt, re.I)
        ex.transaction.receipt_no = _sr.group(1) if _sr else ""
        _bn = _after_label(rt, "BELGE NO", ["ETTN", "İŞLEM KANALI"])
        ex.transaction.document_no = _bn if (_bn and _bn not in ("-", "—")) else ""
        # Tutar / ücret / toplam (US biçimi: 75,000.00). Ücret = FAST ÜCRETİ + BSMV (toplam kesinti)
        def _hmon(pat):
            m = re.search(pat + r"\s*[:：]?\s*(-?[\d.,]+)", rt)
            return _parse_money_token(m.group(1)) if m else None
        ex.amount.value = _hmon(r"İŞLEM TUTARI\s*\(TL\)")
        _uc = _hmon(r"FAST ÜCRET[İIİ]\s*\(TL\)") or _hmon(r"ÜCRET[İIİ]?\s*\(TL\)")
        _bsmv = _hmon(r"BSMV\s*\(TL\)")
        ex.amount.fee = round((_uc or 0) + (_bsmv or 0), 2) if (_uc is not None or _bsmv is not None) else None
        ex.amount.total = _hmon(r"TOPLAM\s*\(TL\s*\)")
        ex.amount.currency = "TL"
        ex.sender.bank = "Türkiye Halk Bankası"

    # =============================================================
    #  PTT (PttBank) — FAST / Havale dekontu (tek sütun, etiketli)
    # =============================================================
    #  Örnek alanlar:
    #    MÜŞTERİ NUMARASI : ...        SAYIN
    #    HESAP NUMARASI  : 16025964    ALLAHVERDİ BARIŞ   (isim sağ sütunda, hesap no'dan sonra)
    #    IBAN : TR04...                (gönderici IBAN — tam)
    #    Alıcı Iban : TR39******2091   (maskeli)
    #    Alıcı Adı  : al****** ku***** (maskeli)
    #    Alan Banka : 67-YAPI VE KREDİ BANKASI A.Ş.
    #    İşlem Türü : Fast   /  İşlem Masrafı : 87,50 TL  /  Tutar : 100.000,00 TL
    #    İşlem Sıra No : 504910  /  Hesabınızdan 100.087,50 TL ... Çekilmiştir.
    elif is_ptt:
        rt = joined
        _PS = ["SAYIN", "HESAP NUMARASI", "IBAN", "TC KİMLİK", "TC KIMLIK", "İŞLEM TARİHİ",
               "İŞLEM YERİ", "Açıklama", "İşlem Türü", "Alıcı Iban", "Alıcı İban", "Alıcı Adı",
               "Alan Banka", "Alan Şube", "Alıcı İl", "Fast Tipi", "Fast Tarihi", "Ödeme Türü",
               "İşlem Masrafı", "Tutar", "İşlem Sıra No", "Hesabınızdan", "PttBank"]
        ex.transaction.type = _find_label(rt, ["İşlem Türü"])
        ex.doc_kind = (ex.transaction.type or "Dekont")
        ex.transaction.channel = _find_label(rt, ["İŞLEM YERİ", "ISLEM YERI"])
        ex.transaction.description = _after_label(rt, "Açıklama", ["İşlem Türü", "Alıcı", "Alan"])
        # İşlem tarihi: "17/08/2026 -21:18" -> tire ayracını boşluğa çevir
        _dt = _after_label(rt, "İŞLEM TARİHİ", ["İŞLEM YERİ", "ISLEM YERI", "Açıklama"])
        ex.transaction.date = re.sub(r"\s*-\s*", " ", _dt).strip() if _dt else ""
        ex.transaction.ref_no = _find_label(rt, ["İşlem Sıra No", "Islem Sira No"])
        # Gönderici. İsim sağ sütunda 'HESAP NUMARASI : <hesapNo> <AD SOYAD>' satırında;
        # 'SAYIN' etiketinden sonra da gelebilir. Hesap no rakam, isim büyük-harf sözcükler.
        _cn = _find_label(rt, ["MÜŞTERİ NUMARASI", "MUSTERI NUMARASI"])
        _cnm = re.match(r"\s*(\d+)", _cn or "")   # 'SAYIN' sağ sütun kalıntısını at, sadece rakam
        ex.sender.customer_no = _cnm.group(1) if _cnm else _cn
        _hesap = _after_label(rt, "HESAP NUMARASI", ["IBAN", "TC KİMLİK", "TC KIMLIK", "İŞLEM"])
        _hm = re.match(r"\s*(\d+)\s+(.+)", _hesap or "")
        if _hm:
            ex.sender.account_no = _hm.group(1)
            ex.sender.name = _clean_name(_hm.group(2))
        else:
            ex.sender.account_no = re.sub(r"\D", "", _hesap or "")
        if not ex.sender.name:
            # 'SAYIN' sonrası büyük-harf isim (sağ sütun iki satıra bölünmüş olabilir)
            _sm = re.search(r"SAYIN\s+([A-ZÇĞİÖŞÜ][A-ZÇĞİÖŞÜ ]{2,})", _norm_tr(rt).upper())
            if _sm:
                ex.sender.name = _clean_name(rt[_sm.start(1):_sm.end(1)])
        ex.sender.tckn = _find_label(rt, ["TC KİMLİK NO", "TC KIMLIK NO"])
        ex.sender.iban = _first_iban_after(rt, ["IBAN"]) or (ex.all_ibans[0] if ex.all_ibans else "")
        ex.sender.bank = "PTT (PttBank)"
        # Alıcı (maskeli IBAN/ad — olduğu gibi tutulur; mod-97 uygulanmaz, FP üretmez)
        ex.receiver.name = _clean_name(_after_label(rt, "Alıcı Adı", ["Alan Banka", "Alan Şube",
                                                                       "Alıcı İl", "Fast"]))
        _rib = _after_label(rt, "Alıcı Iban", ["Alıcı Adı", "Alan Banka"]) \
            or _after_label(rt, "Alıcı İban", ["Alıcı Adı", "Alan Banka"])
        ex.receiver.iban = (_rib or "").strip()
        ex.receiver.bank = _clean_name(_after_label(rt, "Alan Banka", ["Alan Şube", "Alıcı İl",
                                                                       "Fast Tipi", "Fast"]))
        ex.receiver.branch = _clean_name(_after_label(rt, "Alan Şube", ["Alıcı İl", "Fast"]))
        # Tutar / masraf / toplam
        ex.amount.value = parse_amount(_find_label(rt, ["Tutar"]))
        ex.amount.fee = parse_amount(_find_label(rt, ["İşlem Masrafı", "Islem Masrafi"]))
        _tm = re.search(r"Hesab[ıi]n[ıi]zdan\s+(" + AMOUNT_RE.pattern + r")", rt)
        if _tm:
            ex.amount.total = parse_amount(_tm.group(1))
        ex.amount.currency = "TL"

    # =============================================================
    #  KUVEYT TÜRK KATILIM — e-Dekont (IBAN'a Para Transferi / Giden)
    # =============================================================
    #  İki-bloklu: solda gönderen/müşteri, sağda belge bilgisi. Alıcı IBAN 'Gönderilen IBAN'
    #  etiketiyle verilir (gönderilen = alıcıya). Gönderenin kendi IBAN'ı çoğu dekontta yer almaz.
    elif is_kuveyt:
        rt = joined
        ex.doc_kind = "e-Dekont"
        _KS = ["Gönderen Kişi", "Gönderen", "Alıcı Banka", "Alıcı", "Gönderilen IBAN",
               "İşlem Yeri", "Açıklama", "Tutar", "Belge No", "Belge Tarihi", "ETTN",
               "Senaryo", "Müşteri No", "TCKN", "İşlem Ref", "Düzenleyen", "Şube Adı"]
        ex.transaction.type = _find_label(rt, ["Senaryo/Tip", "Senaryo"])
        ex.transaction.document_no = _find_label(rt, ["Belge No"])
        ex.transaction.ettn = _find_label(rt, ["ETTN"])
        ex.transaction.ref_no = _find_label(rt, ["İşlem Ref"])
        ex.transaction.date = _find_label(rt, ["Belge Tarihi"])
        ex.transaction.channel = _clean_name(_after_label(rt, "İşlem Yeri", _KS)) \
            or _find_label(rt, ["Düzenleyen"])
        ex.transaction.description = _after_label(rt, "Açıklama", ["Tutar", "Kuveyt", "Müşteri İmza"])
        ex.sender.customer_no = _find_label(rt, ["Müşteri No"])
        ex.sender.tckn = _find_label(rt, ["TCKN"])
        ex.sender.name = _clean_name(_after_label(rt, "Gönderen Kişi", _KS)) \
            or _clean_name(_after_label(rt, "Gönderen", _KS))
        ex.sender.bank = "Kuveyt Türk Katılım"
        # Alıcı adı 'Alıcı :' (Alıcı Banka'dan ÖNCE gelir). Alıcı IBAN = 'Gönderilen IBAN'.
        ex.receiver.name = _clean_name(_after_label(rt, "Alıcı", ["Gönderilen IBAN", "Alıcı Banka",
                                                                   "IBAN", "İşlem Yeri"]))
        ex.receiver.bank = _clean_name(_after_label(rt, "Alıcı Banka", ["İşlem Yeri", "Açıklama", "Tutar"]))
        _rib = IBAN_RE.search(_after_label(rt, "Gönderilen IBAN", ["Alıcı Banka", "İşlem Yeri"]) or "")
        ex.receiver.iban = banks.normalize_iban(_rib.group(0)) if _rib else ""
        ex.amount.value = parse_amount(_find_label(rt, ["Tutar"]))
        ex.amount.currency = "TL"

    # =============================================================
    #  DENİZBANK — Dekont (Giden FAST / EFT)
    # =============================================================
    #  Müşteri Bilgisi (gönderen: Adı Soyadı, VKN/TOKEN, IBAN) + İşlem Açıklaması
    #  (Alıcı Banka, Alıcı IBAN, Alıcı Adı Soyadı, Tutar, Masraf, İşlem Yapılan Kanal).
    elif is_deniz:
        rt = joined
        ex.doc_kind = _find_label(rt, ["İşlem Türü"]) or "FAST"
        ex.transaction.type = _find_label(rt, ["İşlem Türü"])
        ex.transaction.date = _find_label(rt, ["İşlem Tarihi"])
        ex.transaction.value_date = _find_label(rt, ["Valör Tarihi", "Valör"])
        ex.transaction.channel = _clean_name(_after_label(rt, "İşlem Yapılan Kanal",
                                                          ["Açıklama", "Referans", "FAST", "Ödeme"]))
        ex.transaction.ref_no = _find_label(rt, ["FAST Sorgu Numarası", "FAST Sorgu No"])
        ex.sender.name = _clean_name(_after_label(rt, "Adı Soyadı", ["VKN", "TOKEN", "IBAN", "İşlem"]))
        ex.sender.tckn = _find_label(rt, ["VKN / TOKEN", "VKN/TOKEN", "VKN"])
        ex.sender.bank = "DenizBank"
        # Alıcı
        ex.receiver.name = _clean_name(_after_label(rt, "Alıcı Adı Soyadı",
                                                    ["Tutar", "Masraf", "Ödeme", "İşlem", "Referans"]))
        ex.receiver.bank = _clean_name(_after_label(rt, "Alıcı Banka",
                                                    ["Kolay Adres", "Alıcı IBAN", "Alıcı Adı", "Alıcı"]))
        _rib = IBAN_RE.search(_after_label(rt, "Alıcı IBAN", ["Alıcı Adı", "Tutar", "Masraf"]) or "")
        ex.receiver.iban = banks.normalize_iban(_rib.group(0)) if _rib else ""
        # Gönderen IBAN: alıcıdan FARKLI ilk IBAN (Müşteri Bilgisi'ndeki IBAN, kod 00134)
        for ib in ex.all_ibans:
            if ib and ib != ex.receiver.iban:
                ex.sender.iban = ib
                break
        # Tutar / masraf. 'Masraf : 8,37' (toplam) ilk geçtiği için _find_label onu döndürür.
        ex.amount.value = parse_amount(_find_label(rt, ["Tutar"]))
        ex.amount.fee = parse_amount(_find_label(rt, ["Masraf"]))
        ex.amount.currency = "TL"

    # =============================================================
    #  EVRENSEL / diğer tüm bankalar
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
    # Gönderen IBAN'ı yalnızca ALICI'nınkinden FARKLI bir IBAN varsa ata; tek IBAN alıcıya
    # aitse gönderene yazma (ör. ING'de yalnızca alıcı IBAN'ı görünür).
    if not ex.sender.iban:
        for ib in ex.all_ibans:
            if ib != ex.receiver.iban:
                ex.sender.iban = ib
                break

    # ENPARA/QNB-ÖZEL SON GÜVENCE: bu formatta gönderenin BİTİŞİK müşteri IBAN'ı OCR'da harf
    # sızıntısıyla ('TR0...' -> 'TRO...') bozulunca yakalanamaz; kod ilk 'IBAN:' (=ALICI IBAN'ı)
    # gönderene atayabilir. Ara-guard alıcı IBAN'ı O AN henüz boşsa atlanabildiğinden, TÜM çözümleme
    # bittikten SONRA (return öncesi) son kez denetle: gönderen == alıcı ise farklı bir IBAN dene,
    # yoksa gönderen IBAN'ını BOŞALT — kopya IBAN 'aynı banka' yanlış-pozitifi üretir.
    if (is_enpara or is_qnb) and ex.sender.iban and ex.sender.iban == ex.receiver.iban:
        ex.sender.iban = next((ib for ib in ex.all_ibans if ib and ib != ex.receiver.iban), "")

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

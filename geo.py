"""
Geometrik (koordinat-tabanlı) alan çıkarımı / Geometry-based field extraction.

Düz metin yerine kelimelerin sayfadaki (x, y) konumlarını kullanır. Çok sütunlu
ve satır-kaydırmalı dekont düzenlerinde (özellikle VakıfBank) etiketleri ve
değerleri konumlarına göre eşleştirir. Yöntem: her "değer" kelimesini, aynı
tarafta bulunan en yakın "etiket" bloğuna dikey mesafeye göre atar.
"""
from __future__ import annotations

import io
import re
import pdfplumber

# Türkçe -> ASCII (uzunluk koruyan), eşleştirme için
_TR = str.maketrans({"İ":"i","I":"i","ı":"i","Ş":"s","ş":"s","Ğ":"g","ğ":"g",
                     "Ü":"u","ü":"u","Ö":"o","ö":"o","Ç":"c","ç":"c","Â":"a","â":"a"})
def norm(s): return (s or "").translate(_TR).lower()


def get_words(pdf_bytes: bytes):
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            p = pdf.pages[0]
            # Açık tolerans: pdfplumber sürümleri arası kelime birleştirme farkını sabitler
            return p.extract_words(use_text_flow=False, keep_blank_chars=False,
                                   x_tolerance=3.0, y_tolerance=3.0)
    except Exception:
        return []


def lines_from_words(words, ytol=3):
    """Kelimeleri satırlara gruplar (top'a göre)."""
    rows = {}
    for w in words:
        key = round(w["top"] / ytol) * ytol
        rows.setdefault(key, []).append(w)
    out = []
    for k in sorted(rows):
        ws = sorted(rows[k], key=lambda w: w["x0"])
        out.append({"top": k, "words": ws, "text": " ".join(w["text"] for w in ws)})
    return out


def _order_reading(ws, ytol=6):
    """Kelimeleri görsel satırlara (ytol) gruplayıp okuma sırasına (üst->alt, sol->sağ) dizer."""
    if not ws:
        return ""
    ws = sorted(ws, key=lambda w: w["top"])
    lines = [[ws[0]]]
    for w in ws[1:]:
        if abs(w["top"] - lines[-1][-1]["top"]) <= ytol:
            lines[-1].append(w)
        else:
            lines.append([w])
    parts = []
    for ln in lines:
        for w in sorted(ln, key=lambda w: w["x0"]):
            parts.append(w["text"])
    return " ".join(parts).strip()


def _cluster_x(values, gap=40):
    """x0 değerlerini sütunlara kümeler; her kümenin merkezini döndürür."""
    if not values:
        return []
    vs = sorted(values)
    cols = [[vs[0]]]
    for v in vs[1:]:
        if v - cols[-1][-1] <= gap:
            cols[-1].append(v)
        else:
            cols.append([v])
    return [sum(c) / len(c) for c in cols]


def label_value_map(words, label_phrases):
    """
    Verilen etiket ifadeleri için {etiket: değer} döndürür.

    Algoritma:
      1. Etiket ifadelerini sayfadaki kelime dizilerinde bul (aksan-duyarsız),
         her bulunan etiketin konumunu (x0, top) kaydet.
      2. Etiket olmayan (değer) kelimeleri topla.
      3. Etiketleri sol/sağ sütuna göre ayır; değerleri de sütuna göre ayır.
      4. Her değer kelimesini, AYNI tarafta (sol etiket -> orta değer sütunu,
         sağ etiket -> sağ değer sütunu) en yakın etikete dikey mesafeyle ata.
      5. Etikete atanan değer kelimelerini konuma göre birleştir.
    """
    if not words:
        return {}
    lines = lines_from_words(words)

    # --- 1) Etiket bul (en uzun ifade önce; kısa alias'lar kalanı kapar) ---
    found = []   # {phrase, x0, x1, top, wordset(ids)}
    used_ids = set()
    label_phrases = sorted(label_phrases, key=lambda p: -len(norm(p).split()))
    for phrase in label_phrases:
        toks = norm(phrase).split()
        n = len(toks)
        for ln in lines:
            ws = ln["words"]
            for i in range(len(ws) - n + 1):
                cand = [norm(ws[i + j]["text"]) for j in range(n)]
                if cand == toks:
                    ids = tuple(id(ws[i + j]) for j in range(n))
                    if any(x in used_ids for x in ids):
                        continue
                    x0 = ws[i]["x0"]; x1 = ws[i + n - 1]["x1"]; top = ln["top"]
                    found.append({"phrase": phrase, "x0": x0, "x1": x1, "top": top})
                    for x in ids:
                        used_ids.add(x)
                    break

    if not found:
        return {}

    # etiket kelimelerini işaretle (değerlerden çıkarmak için)
    label_word_ids = set()
    for phrase in label_phrases:
        toks = norm(phrase).split(); n = len(toks)
        for ln in lines:
            ws = ln["words"]
            for i in range(len(ws) - n + 1):
                if [norm(ws[i + j]["text"]) for j in range(n)] == toks:
                    for j in range(n):
                        label_word_ids.add(id(ws[i + j]))

    # --- 2) Etiket sütunlarını belirle (sol / sağ) ---
    label_x = _cluster_x([f["x0"] for f in found], gap=90)
    left_lx = label_x[0]
    right_lx = label_x[1] if len(label_x) > 1 else None

    def side_labels(lx):
        return sorted([f for f in found if abs(f["x0"] - lx) <= 70], key=lambda f: f["top"])

    left_labs = side_labels(left_lx)
    right_labs = side_labels(right_lx) if right_lx is not None else []

    # --- 3) Değer BANDLARI (dar sütun değil) ---
    value_words = [w for w in words if id(w) not in label_word_ids]

    left_lab_right = max((f["x1"] for f in left_labs), default=0)
    right_lab_left = min((f["x0"] for f in right_labs), default=1e9)
    right_lab_right = max((f["x1"] for f in right_labs), default=0)
    page_r = max((w["x1"] for w in words), default=1e9)

    # sol etiketlerin değer bandı: sol etiket sağ kenarı .. sağ etiket sol kenarı
    left_band = (left_lab_right + 4, (right_lab_left - 4) if right_labs else page_r + 1)
    # sağ etiketlerin değer bandı: sağ etiket sağ kenarı .. sayfa sonu
    right_band = (right_lab_right + 4, page_r + 1)

    CAP = 42  # dikey atama mesafesi (satır kaydırmalı değerleri kapsar, dipnotu dışlar)
    # başlık/altbilgi sızmasını önle: etiket bloğunun dışındaki satırları dışla
    top_lim = min(f["top"] for f in found) - 8
    bot_lim = max(f["top"] for f in found) + CAP

    def assign(labs, band):
        res = {}
        if not labs:
            return res
        lo, hi = band
        buckets = {i: [] for i in range(len(labs))}
        for w in value_words:
            if not (lo <= w["x0"] < hi):
                continue
            wc = (w["top"] + w.get("bottom", w["top"])) / 2
            if wc < top_lim or wc > bot_lim:   # başlık/altbilgi bölgesi
                continue
            best = min(range(len(labs)), key=lambda i: abs(labs[i]["top"] - wc))
            # aynı satır güçlü tercih: değer etiketin satırındaysa uzaklık ~0
            if abs(labs[best]["top"] - wc) <= CAP:
                buckets[best].append(w)
        for i, lab in enumerate(labs):
            val = re.sub(r"\s{2,}", " ", _order_reading(buckets[i]))
            if val:
                res[lab["phrase"]] = val
        return res

    result = {}
    result.update(assign(left_labs, left_band))
    result.update(assign(right_labs, right_band))

    # --- Yığılı (kart) düzen yedeği: değer etiketin HEMEN ALTINDA, aynı sütunda ---
    for f in found:
        if f["phrase"] in result:
            continue
        col = [w for w in value_words
               if abs(w["x0"] - f["x0"]) <= 38
               and f["top"] + 5 <= (w["top"] + w.get("bottom", w["top"])) / 2 <= f["top"] + 26]
        if col:
            val = re.sub(r"\s{2,}", " ", _order_reading(col))
            if val:
                result[f["phrase"]] = val
    return result


# ------------------------- Temizleme yardımcıları -------------------------
_IBAN_RE = re.compile(r"TR\d{2}(?:[ ]?\d{4}){5}[ ]?\d{2}", re.I)
_AMT_RE = re.compile(r"-?\d{1,3}(?:\.\d{3})*,\d{2}")


def clean_iban(v: str) -> str:
    m = _IBAN_RE.search((v or "").replace("  ", " "))
    if m:
        return re.sub(r"\s+", "", m.group(0)).upper()
    # boşluklu ham rakamları dene
    digits = re.sub(r"[^0-9A-Z]", "", (v or "").upper())
    m2 = re.search(r"TR\d{24}", digits)
    return m2.group(0) if m2 else ""


def clean_amount(v: str):
    m = _AMT_RE.search(v or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def clean_name(v: str) -> str:
    s = v or ""
    s = s.lstrip(" :：-")                         # baştaki ayraç/kolon
    s = _IBAN_RE.sub(" ", s)
    s = re.sub(r"\bIBAN\b|\bNO\b|\bUNVAN\b|/", " ", s, flags=re.I)
    s = re.sub(r"\d[\d.,: ]*", " ", s)          # rakam blokları
    s = re.sub(r"\s{2,}", " ", s).strip(" .:/-")
    return s


# VakıfBank etiketleri (uzun + kısa alias)
VAKIF_LABELS = [
    "ALICI AD SOYAD/UNVAN", "GÖNDEREN AD SOYAD/UNVAN", "GONDEREN AD SOYAD/UNVAN",
    "GÖNDEREN AD SOYAD", "GONDEREN AD SOYAD", "ALICI AD SOYAD",
    "GÖNDEREN AD", "GONDEREN AD", "ALICI AD",
    "ALICI HESAP NO / IBAN", "ALICI HESAP NO", "GONDEREN HESAP NO", "GÖNDEREN HESAP NO",
    "İŞLEM TARİHİ", "İŞLEM TUTARI", "MASRAF TUTARI", "İŞLEM TÜRÜ", "İŞLEM NO",
    "ALICI BANKA", "SORGU NO", "FİŞ NO", "İŞLEM AÇIKLAMASI",
]


def _pick(m: dict, keys: list) -> str:
    for k in keys:
        if k in m and m[k].strip():
            return m[k].strip()
    return ""


def vakif_fields(pdf_bytes: bytes) -> dict:
    """VakıfBank dekontundan geometrik olarak temizlenmiş alanları döndürür."""
    m = label_value_map(get_words(pdf_bytes), VAKIF_LABELS)
    if not m:
        return {}
    receiver_name = clean_name(_pick(m, ["ALICI AD SOYAD/UNVAN", "ALICI AD SOYAD", "ALICI AD"]))
    sender_name = clean_name(_pick(m, ["GÖNDEREN AD SOYAD/UNVAN", "GONDEREN AD SOYAD/UNVAN",
                                       "GÖNDEREN AD SOYAD", "GONDEREN AD SOYAD",
                                       "GÖNDEREN AD", "GONDEREN AD"]))
    receiver_iban = clean_iban(_pick(m, ["ALICI HESAP NO / IBAN", "ALICI HESAP NO"]))
    sender_iban = clean_iban(_pick(m, ["GONDEREN HESAP NO", "GÖNDEREN HESAP NO"]))
    amount = clean_amount(_pick(m, ["İŞLEM TUTARI"]))
    fee = clean_amount(_pick(m, ["MASRAF TUTARI"]))
    return {
        "receiver_name": receiver_name, "sender_name": sender_name,
        "receiver_iban": receiver_iban, "sender_iban": sender_iban,
        "receiver_bank": clean_name(_pick(m, ["ALICI BANKA"])) or _pick(m, ["ALICI BANKA"]),
        "amount": amount, "fee": fee,
        "date": _pick(m, ["İŞLEM TARİHİ"]),
        "ref_no": _pick(m, ["SORGU NO"]),
        "document_no": re.sub(r"\D", "", _pick(m, ["İŞLEM NO"])),
        "type": _pick(m, ["İŞLEM TÜRÜ"]),
        "_raw": m,
    }

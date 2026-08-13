"""
Veri tutarlılığı kontrolleri / Data consistency checks.

Dekont içindeki sayısal alanların kendi içinde tutarlı olup olmadığını denetler:
  - Komisyon toplamı ≈ Masraf + BSMV
  - Toplam işlem tutarı ≈ İşlem tutarı + Ücret
  - Yazıyla tutar ↔ rakamla tutar
Tutarsızlık, belge üzerinde elle oynama (bir alanı değiştirip diğerini unutma) işareti olabilir.
"""
from __future__ import annotations

import re

_ONES = {"bir": 1, "iki": 2, "üç": 3, "uc": 3, "dört": 4, "dort": 4, "beş": 5, "bes": 5,
         "altı": 6, "alti": 6, "yedi": 7, "sekiz": 8, "dokuz": 9}
_TENS = {"on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "kirk": 40, "elli": 50,
         "altmış": 60, "altmis": 60, "yetmiş": 70, "yetmis": 70, "seksen": 80, "doksan": 90}
_SCALE = {"yüz": 100, "yuz": 100, "bin": 1000, "milyon": 1_000_000, "milyar": 1_000_000_000}

_TOKEN_RE = re.compile(
    "(" + "|".join(sorted(list(_ONES) + list(_TENS) + list(_SCALE), key=len, reverse=True)) + ")"
)


_TR_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ü": "u", "ü": "u", "Ö": "o",
                          "ö": "o", "Ç": "c", "ç": "c", "Ş": "s", "ş": "s", "Ğ": "g",
                          "ğ": "g", "Â": "a", "â": "a"})


def _words_to_int(t: str):
    """Yalnızca tam-sayı Türkçe kelime dizisini sayıya çevirir. Yoksa None."""
    tokens = _TOKEN_RE.findall(t or "")
    if not tokens:
        return None
    total = 0
    current = 0
    for tok in tokens:
        if tok in _ONES:
            current += _ONES[tok]
        elif tok in _TENS:
            current += _TENS[tok]
        elif tok in _SCALE:
            sc = _SCALE[tok]
            if sc >= 1000:
                current = (current or 1) * sc
                total += current
                current = 0
            else:  # yüz
                current = (current or 1) * sc
    return total + current


def parse_turkish_words(s: str):
    """'KırkAltıBin TL' -> 46000.0; 'ONBİN ONALTI TL YETMİŞALTI KR' -> 10016.76.
    LİRA (TL'den önce) ve KURUŞ (TL sonrası, KR'den önce) ayrı çözülür. Bilinmiyorsa None."""
    if not s:
        return None
    t = s.translate(_TR_FOLD).lower().replace("lirasi", "").replace("lira", "")
    kurus = 0.0
    if "kr" in t or "kurus" in t:
        parts = re.split(r"\btl\b", t, maxsplit=1)
        if len(parts) == 2:
            kurus_part = re.split(r"\bkr\b|\bkurus\b", parts[1])[0]
            kurus = (_words_to_int(kurus_part) or 0) / 100.0
            t = parts[0]                       # yalnızca lira kısmı
        else:
            t = t.replace("kurus", "").replace("kr", "")
    t = t.replace("tl", "")
    lira = _words_to_int(t)
    if lira is None and kurus == 0:
        return None
    return round((lira or 0) + kurus, 2)


def _close(a, b, tol=0.02):
    return a is not None and b is not None and abs(a - b) <= max(tol, abs(b) * 0.001 + 0.01)


def check_consistency(amount, fee, total, bsmv_str, amount_words) -> dict:
    """
    Tutarlılık kontrolleri. {checks:[{name, ok, detail}], fail_count}.

    'total' bankaya göre iki farklı anlama gelebilir: Garanti'de KOMİSYON TOPLAMI
    (= Masraf + BSMV), İş Bankası'nda Toplam İşlem Tutarı (= Tutar + Ücret). Bu
    yüzden 'total' iki yorumdan BİRİNE uyuyorsa tutarlı sayılır; hiçbirine uymuyorsa
    tutarsızlık (olası oynama) işaretlenir.
    """
    checks = []

    def num(s):
        if s is None:
            return None
        if isinstance(s, (int, float)):
            return float(s)
        m = re.search(r"\d{1,3}(?:\.\d{3})*,\d{2}", str(s))
        return float(m.group(0).replace(".", "").replace(",", ".")) if m else None

    bsmv = num(bsmv_str); masraf = num(fee); tot = num(total); amt = amount

    # 'Toplam' alanı: Masraf+BSMV (komisyon) VEYA Tutar+Ücret (işlem) ile uyumlu olmalı
    if tot is not None:
        opts = []
        if masraf is not None and bsmv is not None:
            opts.append(("Masraf+BSMV", masraf + bsmv))
        if amt is not None and masraf is not None:
            opts.append(("Tutar+Ücret", abs(amt) + masraf))
        if opts:
            ok = any(_close(v, tot) for _, v in opts)
            best = "; ".join(f"{n}={v:.2f}" for n, v in opts)
            checks.append({"name": "Toplam tutarlılığı", "ok": ok,
                           "detail": f"Toplam {tot:.2f} ↔ ({best})"})

    # Yazıyla tutar ↔ rakamla tutar. Yazı; İŞLEM tutarını, TOPLAM'ı ya da (İşlem+Masraf)'ı
    # ifade edebilir (bankaya göre değişir) — herhangi biriyle uyuşuyorsa TUTARLIDIR.
    if amount_words and (amt is not None or tot is not None):
        wv = parse_turkish_words(amount_words)
        if wv is not None:
            cands = []
            if amt is not None:
                cands.append(abs(amt))
            if tot is not None:
                cands.append(abs(tot))
            if amt is not None and masraf is not None:
                cands.append(abs(amt) + masraf)
            ok = any(_close(wv, c, tol=0.5) for c in cands)
            checks.append({"name": "Yazıyla tutar ↔ rakam", "ok": ok,
                           "detail": f"'{amount_words}' = {wv} ↔ {', '.join(f'{c:.2f}' for c in cands)}"})

    fail = sum(1 for c in checks if not c["ok"])
    return {"checks": checks, "fail_count": fail, "check_count": len(checks)}

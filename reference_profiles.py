"""
Referans parmak-izi profilleri / Reference fingerprint profiles.

FİKİR (kullanıcı): Elimizdeki ONLARCA GERÇEK dijital PDF dekonttan, her banka için o bankanın
gerçek dekontunun 'parmak izini' çıkarırız (numara hane desenleri, para birimi soneki alışkanlığı,
alan kümesi). Gelen bir FOTOĞRAFTAN okunan veriyi bu parmak iziyle KIYASLARIZ; gerçek şablondan
SAPMA varsa işaretleriz. Böylece 'gerçek veri ile okunan veri arasındaki fark' üzerinden çıkarım
yaparız — tek tek sezgilerden daha sağlam, çünkü GERÇEK VERİYE dayanır.

Aşağıdaki profiller, bu projedeki gerçek PDF korpusu MADENLENEREK üretilmiştir (2026-08). 'support'
= o özelliğin kaç gerçek örnekten görüldüğü; yalnız support >= _MIN_SUPPORT olan özellikler KURAL
olarak uygulanır (az örnekten kesin desen çıkarmayız → yanlış-pozitif önlenir). Yeni gerçek PDF
geldikçe bu profiller güncellenir.

Para birimi soneki BANKAYA GÖRE DEĞİŞİR (kritik): VakıfBank/Ziraat/Garanti/DenizBank gerçek
dekontlarında tutar/masraf HEP 'TL' ile yazılır; Enpara/QNB/ING ise TL soneki KULLANMAZ. Bu yüzden
'TL eksik' kuralı banka-özeldir (blanket kural yanlış olurdu).

Bu modül SUNUM/KIYAS katmanıdır; deterministik bulgular üretir ve YZ değerlendiriciye bağlam verir.
"""
from __future__ import annotations

import re

_MIN_SUPPORT = 3   # bir özelliği KURAL saymak için gereken asgari gerçek örnek sayısı

# Korpustan madenlenmiş profiller. Değerler: (değer, support).
# id_lengths: {alan: (izinli_hane_kümesi, support)}
# fee_currency / amount_currency: ("always"|"never", support)
REFERENCE_PROFILES = {
    "vakif": {"label": "VakıfBank", "n": 6,
              "amount_currency": ("always", 9), "fee_currency": ("always", 3),
              "id_lengths": {"sorgu_no": ({10}, 4), "islem_no": ({16}, 6)}},
    "ziraat": {"label": "T.C. Ziraat Bankası", "n": 6,
               "amount_currency": ("always", 6), "fee_currency": ("always", 9),
               "id_lengths": {"sorgu_no": ({10}, 3)}},
    "garanti": {"label": "Garanti BBVA", "n": 12,
                "amount_currency": ("always", 3), "fee_currency": ("always", 9),
                "id_lengths": {}},
    "deniz": {"label": "DenizBank", "n": 2,
              "fee_currency": ("always", 4), "id_lengths": {}},
    "isbank": {"label": "Türkiye İş Bankası", "n": 4,
               "amount_currency": ("always", 6), "id_lengths": {}},
    "yapikredi": {"label": "Yapı ve Kredi Bankası", "n": 4,
                  "id_lengths": {"sorgu_no": ({10}, 3)}},
    "halk": {"label": "Türkiye Halk Bankası", "n": 3,
             "id_lengths": {"sorgu_no": ({10}, 3)}},
    "qnb": {"label": "QNB Bank A.Ş.", "n": 4, "amount_currency": ("never", 2),
            "id_lengths": {"sorgu_no": ({10}, 3), "fis_no": ({15}, 3)}},
    "enpara": {"label": "Enpara.com (QNB)", "n": 18, "amount_currency": ("never", 14),
               "id_lengths": {"fis_no": ({15}, 6)}},   # SORGU NO enpara'da 7 ve 10 → tutarsız, kural yok
    "ing": {"label": "ING Bank A.Ş.", "n": 1, "amount_currency": ("never", 1),
            "id_lengths": {}},
    # Az örnekli/henüz profil yok: akbank(2), fiba, ptt, teb, getir, kuveyt, alternatif → korpus büyüdükçe eklenir
}

# Belgeden okunacak kimlik alanı etiketleri (İ-güvenli desenler).
_ID_LABEL_PAT = {
    "sorgu_no": r"SORGU\s*NO",
    "islem_no": r"[İIıi]?[şsSŞ]LEM\s*NO",
    "fis_no": r"F[İIıi][şsSŞ]\s*NO",
}


def profile(bank_key: str) -> dict | None:
    return REFERENCE_PROFILES.get((bank_key or "").strip().lower())


def _reliable(feat) -> bool:
    """(value, support) özelliği KURAL sayılacak kadar örneğe dayanıyor mu."""
    return isinstance(feat, tuple) and len(feat) == 2 and (feat[1] or 0) >= _MIN_SUPPORT


def check_against_reference(bank_key: str, text: str) -> list:
    """Gelen dekontun metnini bankanın GERÇEK parmak iziyle kıyaslar; sapma bulgularını döndürür.
    Yalnız YETERLİ örneğe (support>=3) dayanan özellikler uygulanır → yanlış-pozitif düşük.
    Not: Fotoğrafta OCR gürültüsü olabileceğinden bu bulgular YZ değerlendiriciye teyit için gider."""
    out = []
    p = profile(bank_key)
    if not p or not text:
        return out
    lbl = p.get("label", bank_key)

    # (1) KİMLİK NUMARASI HANE DESENİ: gerçek şablonda sabit uzunlukta olan bir numara, gelen
    # dekontta FARKLI uzunluktaysa sapmadır (numara uydurulmuş/eksik/fazla okunmuş olabilir).
    for field, (allowed, support) in (p.get("id_lengths") or {}).items():
        if support < _MIN_SUPPORT:
            continue
        pat = _ID_LABEL_PAT.get(field)
        if not pat:
            continue
        m = re.search(pat + r"\s*[:：]?\s*([0-9]{4,})", text, re.I)
        if not m:
            continue
        ln = len(m.group(1))
        if ln not in allowed:
            _exp = "/".join(str(x) for x in sorted(allowed))
            out.append({
                "code": "REF_ID_LENGTH_MISMATCH", "severity": "medium", "weight": 16,
                "tr": f"REFERANS SAPMASI ({lbl}): '{field.replace('_',' ').upper()}' numarası {ln} hane "
                      f"({m.group(1)}); oysa bu bankanın GERÇEK dekontlarında hep {_exp} hanedir. Numara "
                      f"uydurulmuş ya da yanlış üretilmiş olabilir. Fotoğrafta OCR etkisi olabilir; teyit gerekir.",
                "en": f"REFERENCE DEVIATION ({lbl}): the '{field}' number has {ln} digits ({m.group(1)}), but "
                      f"genuine receipts of this bank always use {_exp} digits. May be fabricated/misgenerated.",
                "detail": f"field={field} got_len={ln} expected={_exp} support={support}"})

    # (2) PARA BİRİMİ SONEKİ (banka-özel): gerçek şablonda masraf/tutar HEP 'TL' ile yazılıyorsa,
    # gelen dekontta o alan TL'siz ise sapmadır. (Enpara/QNB gibi TL kullanmayan bankalarda 'never'
    # olduğundan bu kural TETİKLENMEZ → doğru, banka-özel.)
    fee_c = p.get("fee_currency")
    if _reliable(fee_c) and fee_c[0] == "always":
        mf = re.search(r"(?:MASRAF(?:\s*TUTARI)?|[İIıi]?[şs]lem\s*[ÜUu]creti|Komisyon)\s*[:：]?\s*"
                       r"([\d.]+,\d{2})\s*(TL|TRY|₺)?", text, re.I)
        if mf and not mf.group(2):
            out.append({
                "code": "REF_FEE_CURRENCY_MISSING", "severity": "high", "weight": 22,
                "tr": f"REFERANS SAPMASI ({lbl}): MASRAF tutarı ('{mf.group(1)}') PARA BİRİMİ (TL) OLMADAN "
                      f"yazılmış; oysa bu bankanın GERÇEK dekontlarında masraf HER ZAMAN 'TL' ile yazılır "
                      f"({fee_c[1]} gerçek örnekte). Masraf alanı sonradan eklenmiş/değiştirilmiş olabilir.",
                "en": f"REFERENCE DEVIATION ({lbl}): the FEE ('{mf.group(1)}') lacks a currency suffix, but "
                      f"genuine receipts of this bank always write the fee with 'TL'. Possible alteration.",
                "detail": f"fee={mf.group(1)} bank_requires_TL support={fee_c[1]}"})

    return out


def context_for(bank_key: str) -> str:
    """YZ değerlendiriciye verilecek KISA referans-parmak-izi özeti (bilgi tabanına ek)."""
    p = profile(bank_key)
    if not p:
        return ""
    parts = [f"REFERANS PARMAK İZİ — {p.get('label', bank_key)} ({p.get('n', 0)} gerçek PDF örnekten):"]
    ac = p.get("amount_currency")
    if _reliable(ac):
        parts.append(f"- Tutar para birimi: {'HEP TL var' if ac[0]=='always' else 'TL soneki KULLANILMAZ'}.")
    fc = p.get("fee_currency")
    if _reliable(fc):
        parts.append(f"- Masraf para birimi: {'HEP TL var' if fc[0]=='always' else 'TL soneki yok'}.")
    for field, (allowed, support) in (p.get("id_lengths") or {}).items():
        if support >= _MIN_SUPPORT:
            parts.append(f"- {field.replace('_',' ').upper()} hane: hep {'/'.join(str(x) for x in sorted(allowed))}.")
    return "\n".join(parts)

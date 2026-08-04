"""
PDF revizyon karşılaştırması / PDF revision comparison.

Bir PDF'e artımlı güncelleme (incremental update) yapıldığında, orijinal içerik
dosyada kalır ve üzerine yeni bir revizyon EKLENİR. Her %%EOF işaretine kadar olan
bölüm, o ana kadarki geçerli bir PDF'tir. Bu modül önceki revizyon(lar)ı çıkarır,
her birinden kritik dekont alanlarını okur ve mevcut (son) sürümle karşılaştırır.
Böylece "gönderen/alıcı/tutar/IBAN sonradan değiştirilmiş mi?" sorusu yanıtlanır.

Bu, artımlı güncellemeli belgelerde tahrifatın EN kesin kanıtıdır.
"""
from __future__ import annotations

import io
import re

from extract import extract_text_digital, extract_fields

# Karşılaştırılacak alanlar: (anahtar, TR etiket, önem)
FIELDS = [
    ("sender_name", "Gönderen / hesap sahibi", "kritik"),
    ("receiver_name", "Alıcı", "kritik"),
    ("sender_iban", "Gönderen IBAN", "kritik"),
    ("receiver_iban", "Alıcı IBAN", "kritik"),
    ("amount", "İşlem tutarı", "kritik"),
    ("date", "İşlem tarihi", "kritik"),
    ("ref_no", "Referans No", "destekleyici"),
    ("fee", "Masraf / ücret", "destekleyici"),
    ("total", "Toplam / komisyon", "destekleyici"),
    ("bsmv", "BSMV", "destekleyici"),
    ("amount_words", "Yazıyla tutar", "destekleyici"),
]


def extract_revisions(data: bytes) -> list[bytes]:
    """Her %%EOF'a kadar olan bölümü bir revizyon olarak döndürür (eskiden yeniye)."""
    revs = []
    for m in re.finditer(rb"%%EOF", data):
        end = m.end()
        # olası \r\n
        tail = data[end:end + 2]
        extra = len(tail) - len(tail.lstrip(b"\r\n"))
        revs.append(data[:end + extra])
    return revs


def _fields_from_bytes(blob: bytes) -> dict:
    """Bir revizyondan kritik alanları çıkarır."""
    try:
        lay = extract_text_digital(blob, layout=True)
        read = extract_text_digital(blob, layout=False)
        ex = extract_fields(lay, read, blob)
    except Exception:
        return {}
    txt = lay or read or ""
    out = {
        "sender_name": ex.sender.name or "",
        "receiver_name": ex.receiver.name or "",
        "sender_iban": ex.sender.iban or "",
        "receiver_iban": ex.receiver.iban or "",
        "amount": ex.amount.value,
        "date": ex.transaction.date or "",
        "ref_no": ex.transaction.ref_no or "",
        "fee": ex.amount.fee,
        "total": ex.amount.total,
        "bsmv": _find_bsmv(txt),
        "amount_words": _find_amount_words(txt),
    }
    return out


def _find_bsmv(text: str):
    m = re.search(r"BSMV[^0-9]{0,6}(\d{1,3}(?:\.\d{3})*,\d{2})", text, re.I)
    return m.group(1) if m else None


def _find_amount_words(text: str) -> str:
    # Garanti: "YALNIZ KırkAltıBinTL."  / genel: "YALNIZ ... TL"
    m = re.search(r"YALNIZ\s*([A-Za-zÇĞİÖŞÜçğıöşü ]+?TL)\b", text)
    if m:
        return re.sub(r"\s+", "", m.group(1))
    return ""


def _norm(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # tutarları normalize et
        return f"{v:.2f}"
    return re.sub(r"\s+", " ", str(v)).strip().upper()


def compare_revisions(data: bytes) -> dict:
    """
    Önceki (ilk) revizyon ile mevcut (son) sürümü karşılaştırır.
    Döndürür: has_prior, revision_count, changes[], critical_count.
    """
    revs = extract_revisions(data)
    result = {"has_prior": False, "revision_count": len(revs), "changes": [],
              "critical_count": 0, "supporting_count": 0}
    if len(revs) < 2:
        return result

    prev = _fields_from_bytes(revs[0])       # en eski (orijinal)
    curr = _fields_from_bytes(data)          # tam dosya (son sürüm)
    if not prev or not curr:
        return result
    result["has_prior"] = True

    for key, label, sev in FIELDS:
        pv = prev.get(key)
        cv = curr.get(key)
        np, nc = _norm(pv), _norm(cv)
        # ikisi de dolu ve farklıysa değişiklik
        if np and nc and np != nc:
            result["changes"].append({
                "field": key, "label": label, "severity": sev,
                "prev": _display(pv), "curr": _display(cv),
            })
            if sev == "kritik":
                result["critical_count"] += 1
            else:
                result["supporting_count"] += 1
    return result


def _display(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        # Türk biçimi
        s = f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return s
    return str(v)

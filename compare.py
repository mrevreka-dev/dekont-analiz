"""
Çapraz dekont / sıra numarası analizi — Cross-receipt sequence analysis.

Birden çok dekont birlikte incelendiğinde, aynı göndericiye ait dekontlarda
işlem/sorgu/referans numarası ZAMANLA ARTMALIDIR: bir banka her işleme, kronolojik
olarak artan bir sıra/işlem numarası verir. Dolayısıyla:

  - Aynı gönderenin, daha SONRAKİ (ör. 5 dk sonra) bir işleminde sıra numarası
    daha DÜŞÜK ise -> güçlü sahtecilik işareti (numara elle değiştirilmiş / uydurulmuş).
  - Farklı iki işlemde AYNI sıra numarası varsa -> kopyala-yapıştır / şablon sahteciliği.
  - Zaman ile numara arasında makul (monoton artan) bir ilişki varsa -> doğrulayıcı.

Bu modül, tek tek analiz edilmiş dekont raporlarını (analyze_document çıktısı) alır,
gönderene göre gruplar ve zaman-numara tutarlılığını değerlendirir.
"""
from __future__ import annotations

import re
from timing import parse_content_datetime


def _sender_key(rep: dict) -> str:
    """Gönderen kimliği: önce IBAN (normalize), yoksa ad (normalize)."""
    ex = rep.get("extracted", {})
    s = ex.get("sender", {})
    iban = re.sub(r"\s+", "", (s.get("iban") or "")).upper()
    if len(iban) >= 20:
        return "iban:" + iban
    name = re.sub(r"\s+", " ", (s.get("name") or "")).strip().upper()
    if name:
        return "name:" + name
    return ""


def _seq_int(rep: dict):
    """Sıra/işlem numarasını tamsayıya çevirir (yoksa None)."""
    ex = rep.get("extracted", {})
    tx = ex.get("transaction", {})
    seq = tx.get("sequence_number") or ""
    if not seq:
        # yedek: ref_no / document_no içindeki en uzun rakam dizisi
        for c in (tx.get("document_no"), tx.get("ref_no")):
            for run in re.findall(r"\d{6,}", c or ""):
                if len(run) > len(seq):
                    seq = run
    return int(seq) if seq.isdigit() else None


def _txn_dt(rep: dict):
    ex = rep.get("extracted", {})
    dt, _ = parse_content_datetime(ex.get("transaction", {}).get("date") or "")
    return dt


def _bank_key(rep: dict) -> str:
    """Gönderen banka adı (numaralandırma şeması bankaya özgüdür)."""
    ex = rep.get("extracted", {})
    b = ex.get("bank") or ex.get("sender", {}).get("bank") or ""
    return re.sub(r"\s+", " ", b).strip().upper()


def _label(rep: dict, idx: int) -> str:
    name = rep.get("file", {}).get("name") or f"dekont #{idx+1}"
    return name


def compare_receipts(reports: list[dict]) -> dict:
    """
    reports: analyze_document() çıktısı olan dict listesi (>=2 önerilir).
    Döndürür: gruplar, çapraz bulgular ve genel bir çapraz-tutarlılık kararı.
    """
    items = []
    for i, rep in enumerate(reports):
        seq = _seq_int(rep)
        items.append({
            "idx": i,
            "label": _label(rep, i),
            "sender_key": _sender_key(rep),
            "sender_name": rep.get("extracted", {}).get("sender", {}).get("name", ""),
            "bank": _bank_key(rep),
            "seq": seq,
            "seq_len": len(str(seq)) if seq is not None else 0,
            "seq_raw": rep.get("extracted", {}).get("transaction", {}).get("sequence_number", ""),
            "dt": _txn_dt(rep),
            "date_raw": rep.get("extracted", {}).get("transaction", {}).get("date", ""),
            "amount": rep.get("extracted", {}).get("amount", {}).get("value"),
            "score": rep.get("score", {}).get("authenticity_score"),
        })

    findings = []          # {code, severity, tr, en, detail, receipts:[idx,...]}

    # --- Küresel yinelenen sıra numarası (farklı işlemlerde aynı numara) ---
    by_seq: dict[int, list] = {}
    for it in items:
        if it["seq"] is not None:
            by_seq.setdefault(it["seq"], []).append(it)
    for seq, group in by_seq.items():
        if len(group) > 1:
            # aynı numara + farklı tutar/tarih => kopyala-yapıştır sahtecilik
            amounts = {g["amount"] for g in group}
            dates = {g["date_raw"] for g in group}
            if len(amounts) > 1 or len(dates) > 1:
                findings.append({
                    "code": "SEQ_DUPLICATE",
                    "severity": "critical",
                    "tr": f"Aynı işlem/sıra numarası ({seq}) birden çok FARKLI dekontta görülüyor. "
                          f"Bankalar her işleme benzersiz bir numara verir; aynı numaranın farklı "
                          f"tutar/tarihli belgelerde tekrarı, bir dekontun kopyalanıp üzerinde "
                          f"oynandığını gösterir.",
                    "en": f"The same transaction/sequence number ({seq}) appears on multiple different "
                          f"receipts. Banks assign a unique number per transaction; repetition across "
                          f"documents with different amounts/dates indicates copy-paste forgery.",
                    "detail": ", ".join(g["label"] for g in group),
                    "receipts": [g["idx"] for g in group],
                })

    # --- Gönderene göre grupla, zaman-numara monotonluğu ---
    groups: dict[str, list] = {}
    for it in items:
        if it["sender_key"]:
            groups.setdefault(it["sender_key"], []).append(it)

    group_reports = []
    for key, members in groups.items():
        # zaman ve numara birlikte olanlar
        usable = [m for m in members if m["dt"] is not None and m["seq"] is not None]
        pair_findings = []
        if len(usable) >= 2:
            usable.sort(key=lambda m: m["dt"])
            for a, b in zip(usable, usable[1:]):
                if b["dt"] == a["dt"]:
                    continue
                # Sıra numarası karşılaştırması yalnızca AYNI BANKA + AYNI FORMAT (uzunluk)
                # için anlamlıdır; bankaların numaralandırma şemaları farklıdır.
                same_scheme = (a["bank"] and a["bank"] == b["bank"] and a["seq_len"] == b["seq_len"])
                if not same_scheme:
                    pair_findings.append({
                        "code": "SEQ_SCHEME_DIFF",
                        "severity": "info",
                        "tr": f"'{a['label']}' ve '{b['label']}' için işlem numaraları farklı banka/format "
                              f"şemasında ({a['bank'] or '?'} / {b['bank'] or '?'}); sıra karşılaştırması "
                              f"yapılmadı. Sıra tutarlılığı yalnızca aynı bankanın aynı biçimdeki "
                              f"numaraları için değerlendirilebilir.",
                        "en": f"Transaction numbers for '{a['label']}' and '{b['label']}' use different "
                              f"bank/format schemes ({a['bank'] or '?'} / {b['bank'] or '?'}); order not "
                              f"compared. Sequence consistency is only meaningful for same-format numbers "
                              f"from the same bank.",
                        "detail": f"{a['bank']}(len {a['seq_len']}) vs {b['bank']}(len {b['seq_len']})",
                        "receipts": [a["idx"], b["idx"]],
                    })
                    continue
                # b, a'dan SONRA => b.seq > a.seq beklenir (aynı banka & format)
                if b["seq"] < a["seq"]:
                    pair_findings.append({
                        "code": "SEQ_ORDER_VIOLATION",
                        "severity": "high",
                        "tr": f"Zaman ile işlem numarası ÇELİŞİYOR ({a['bank']}): '{b['label']}' işlemi "
                              f"'{a['label']}' işleminden SONRA gerçekleşmesine rağmen "
                              f"({a['date_raw']} → {b['date_raw']}), aynı formattaki işlem numarası ARTMAK "
                              f"yerine AZALMIŞ ({a['seq']} → {b['seq']}). Sıralı numaralandırma kullanan "
                              f"bankalarda sonraki işlemin numarası daha büyük olmalıdır; bu, numaranın "
                              f"elle değiştirildiğine işaret edebilir. Bankanın numaralandırma düzenine "
                              f"göre elle doğrulanması önerilir.",
                        "en": f"Time vs. transaction number CONTRADICTION ({a['bank']}): '{b['label']}' "
                              f"occurred AFTER '{a['label']}' ({a['date_raw']} → {b['date_raw']}), yet the "
                              f"same-format transaction number DECREASED ({a['seq']} → {b['seq']}). For "
                              f"banks that number sequentially, a later transaction should carry a higher "
                              f"number — this may indicate the number was altered. Manual verification "
                              f"against the bank's numbering scheme is recommended.",
                        "detail": f"{a['label']} (#{a['seq']} @ {a['date_raw']}) -> "
                                  f"{b['label']} (#{b['seq']} @ {b['date_raw']})",
                        "receipts": [a["idx"], b["idx"]],
                    })
                elif b["seq"] > a["seq"]:
                    # doğrulayıcı: zaman ilerledikçe numara arttı
                    pair_findings.append({
                        "code": "SEQ_ORDER_OK",
                        "severity": "info",
                        "tr": f"Tutarlı ({a['bank']}): '{b['label']}' işlemi '{a['label']}' işleminden sonra "
                              f"ve aynı formattaki işlem numarası da beklendiği gibi artmış "
                              f"({a['seq']} → {b['seq']}).",
                        "en": f"Consistent ({a['bank']}): '{b['label']}' is after '{a['label']}' and the "
                              f"same-format transaction number increased as expected ({a['seq']} → {b['seq']}).",
                        "detail": f"{a['seq']} @ {a['date_raw']} -> {b['seq']} @ {b['date_raw']}",
                        "receipts": [a["idx"], b["idx"]],
                    })
        findings.extend(pair_findings)
        group_reports.append({
            "sender_key": key,
            "sender_name": members[0]["sender_name"],
            "count": len(members),
            "members": [{"idx": m["idx"], "label": m["label"], "seq": m["seq_raw"],
                         "date": m["date_raw"], "amount": m["amount"]} for m in members],
        })

    # --- Genel karar ---
    crit = [f for f in findings if f["severity"] == "critical"]
    viol = [f for f in findings if f["code"] == "SEQ_ORDER_VIOLATION"]
    ok = [f for f in findings if f["code"] == "SEQ_ORDER_OK"]
    if crit:
        verdict = "inconsistent"
        verdict_tr = ("Çapraz analiz TUTARSIZ: dekontlar arası işlem numarası/zaman ilişkisinde "
                      "sahtecilik işareti bulundu (aynı numara farklı işlemlerde tekrar ediyor).")
        verdict_en = ("Cross-analysis INCONSISTENT: forgery signals found in the sequence/time "
                      "relationship between receipts (same number repeated on different transactions).")
    elif viol:
        verdict = "review_needed"
        verdict_tr = ("İncelenmeli: aynı banka ve formattaki işlem numaraları zamanla artması "
                      "gerekirken azalmış. Sıralı numaralandırma kullanan bankalarda bu bir tahrifat "
                      "işareti olabilir; bankanın numaralandırma düzenine göre elle doğrulayın.")
        verdict_en = ("Review needed: same-bank, same-format transaction numbers decreased over time "
                      "instead of increasing. For sequentially-numbering banks this can indicate "
                      "tampering; verify manually against the bank's numbering scheme.")
    elif ok:
        verdict = "consistent"
        verdict_tr = "Çapraz analiz TUTARLI: işlem numaraları zamanla beklendiği gibi artıyor."
        verdict_en = "Cross-analysis CONSISTENT: transaction numbers increase over time as expected."
    else:
        verdict = "insufficient"
        verdict_tr = ("Çapraz karşılaştırma için yeterli ortak veri yok (aynı gönderen ve aynı bankaya "
                      "ait, zaman + aynı formatta işlem numarası içeren en az iki dekont gerekir).")
        verdict_en = ("Not enough common data for cross-comparison (need at least two receipts from the "
                      "same sender and same bank that include both time and a same-format number).")

    return {
        "count": len(reports),
        "verdict": verdict,
        "verdict_tr": verdict_tr,
        "verdict_en": verdict_en,
        "groups": group_reports,
        "findings": findings,
        "critical_count": len(crit),
        "violation_count": len(viol),
        "items": [{"idx": it["idx"], "label": it["label"], "sender_name": it["sender_name"],
                   "seq": it["seq_raw"], "date": it["date_raw"], "amount": it["amount"],
                   "score": it["score"]} for it in items],
    }

"""
Kalıcı dekont numarası veritabanı / Persistent receipt-number store.

Doğrulanmış (tahrifat bulunmayan, yüksek puanlı) dekontların dekont/işlem/sorgu/referans
numaralarını BANKA BAZLI olarak kalıcı bir SQLite veritabanında saklar. Sonradan yüklenen
dekontları bu geçmiş kayıtlarla karşılaştırır:

  - Aynı banka + aynı numara, FARKLI bir dekontta (farklı tutar/tarih/dosya) görülürse
    -> SEQ_DB_DUPLICATE (kritik): numara başka bir işlemden kopyalanmış/uydurulmuş.
  - Aynı banka + aynı gönderen + aynı formatta numara, zamanla AZALMIŞSA
    -> SEQ_DB_ORDER (yüksek): sıralı numaralandırmaya aykırı, incelenmeli.

Yapılandırma:
  DEKONT_DB_PATH        : SQLite dosya yolu (varsayılan: /data/dekont.db — Railway kalıcı volume).
                          Yazılamıyorsa uygulama dizinine düşer (dağıtım süresince kalıcı).
  DEKONT_STORE_ENABLED  : "0" ise tamamen kapatır.
  DEKONT_STORE_MIN_SCORE: otomatik kayıt için asgari doğruluk puanı (varsayılan 80).

Tüm işlemler en iyi çaba ilkesiyle çalışır; veritabanı hatası analizi ASLA bozmaz.
"""
from __future__ import annotations

import os
import re
import sqlite3
import datetime as _dt

from timing import parse_content_datetime

_DEF_PATHS = ["/data/dekont.db", os.path.join(os.path.dirname(__file__), "..", "dekont_store.db")]


def enabled() -> bool:
    return os.environ.get("DEKONT_STORE_ENABLED", "1") != "0"


def _db_path() -> str:
    p = os.environ.get("DEKONT_DB_PATH")
    if p:
        return p
    for cand in _DEF_PATHS:
        d = os.path.dirname(os.path.abspath(cand))
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return cand
    return _DEF_PATHS[-1]


def _connect():
    path = _db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    con = sqlite3.connect(path, timeout=5, check_same_thread=False)
    con.execute("""CREATE TABLE IF NOT EXISTS receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bank TEXT, seq_number TEXT, seq_len INTEGER,
        ref_no TEXT, document_no TEXT,
        sender_iban TEXT, sender_name TEXT, receiver_iban TEXT,
        amount REAL, txn_date TEXT, txn_dt TEXT,
        sha256 TEXT UNIQUE, score INTEGER, created_at TEXT )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bank_seq ON receipts(bank, seq_number)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_bank_sender ON receipts(bank, sender_iban)")
    # AUDIT LOG: HER analiz (sahte dahil) buraya yazılır -> toplam yükleme sayısı + kara-liste.
    con.execute("""CREATE TABLE IF NOT EXISTS analyses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sha256 TEXT UNIQUE, bank TEXT, is_receipt INTEGER,
        score INTEGER, risk TEXT, is_fake INTEGER,
        seq_number TEXT, ref_no TEXT, document_no TEXT,
        amount REAL, txn_date TEXT, codes TEXT, created_at TEXT )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_an_bankseq ON analyses(bank, seq_number)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_an_fake ON analyses(is_fake)")
    con.commit()
    return con


def _fields(report: dict) -> dict:
    ex = report.get("extracted", {})
    tx = ex.get("transaction", {})
    seq = tx.get("sequence_number") or ""
    dt, _ = parse_content_datetime(tx.get("date") or "")
    return {
        "bank": re.sub(r"\s+", " ", (ex.get("bank") or ex.get("sender", {}).get("bank") or "")).strip().upper(),
        "seq_number": seq,
        "seq_len": len(seq) if seq.isdigit() else 0,
        "ref_no": tx.get("ref_no") or "",
        "document_no": tx.get("document_no") or "",
        "sender_iban": re.sub(r"\s+", "", (ex.get("sender", {}).get("iban") or "")).upper(),
        "sender_name": (ex.get("sender", {}).get("name") or "").strip(),
        "receiver_iban": re.sub(r"\s+", "", (ex.get("receiver", {}).get("iban") or "")).upper(),
        "amount": ex.get("amount", {}).get("value"),
        "txn_date": tx.get("date") or "",
        "txn_dt": dt.isoformat() if dt else "",
        "sha256": report.get("file", {}).get("sha256") or "",
        "score": report.get("score", {}).get("authenticity_score"),
    }


def check(report: dict) -> list[dict]:
    """Yüklenen dekontu geçmiş kayıtlarla karşılaştırır; bulgu listesi döndürür.
    Her bulgu: {code, severity, tr, en, detail}."""
    if not enabled():
        return []
    f = _fields(report)
    findings = []
    if not f["bank"] or not f["seq_number"]:
        return findings
    try:
        con = _connect()
    except Exception:
        return findings
    try:
        cur = con.execute(
            "SELECT sha256, amount, txn_date, txn_dt, sender_iban, sender_name FROM receipts "
            "WHERE bank=? AND seq_number=?", (f["bank"], f["seq_number"]))
        dups = [r for r in cur.fetchall() if r[0] != f["sha256"]]
        if dups:
            d = dups[0]
            findings.append({
                "code": "SEQ_DB_DUPLICATE", "severity": "critical",
                "tr": f"Bu dekonttaki işlem/sıra numarası ({f['seq_number']}, {f['bank']}) daha önce "
                      f"kaydedilmiş FARKLI bir dekontta da mevcut (tutar: {d[1]}, tarih: {d[2] or '—'}). "
                      f"Bankalar her işleme benzersiz numara verir; aynı numaranın başka bir belgede "
                      f"görülmesi, numaranın kopyalandığını/uydurulduğunu gösterir. Yüksek sahtecilik riski.",
                "en": f"The transaction/sequence number in this receipt ({f['seq_number']}, {f['bank']}) already "
                      f"exists on a DIFFERENT previously-recorded receipt (amount: {d[1]}, date: {d[2] or '—'}). "
                      f"Banks assign a unique number per transaction; a repeat elsewhere indicates the number "
                      f"was copied/fabricated. High forgery risk.",
                "detail": f"bank={f['bank']} seq={f['seq_number']} vs sha={d[0][:12]}",
            })

        # Sıra-zaman monotonluğu (aynı banka + aynı gönderen + aynı format)
        if f["sender_iban"] and f["txn_dt"] and f["seq_len"]:
            cur = con.execute(
                "SELECT seq_number, txn_dt, txn_date FROM receipts WHERE bank=? AND sender_iban=? "
                "AND seq_len=? AND sha256<>? AND txn_dt<>''",
                (f["bank"], f["sender_iban"], f["seq_len"], f["sha256"]))
            try:
                cur_dt = _dt.datetime.fromisoformat(f["txn_dt"])
                cur_seq = int(f["seq_number"])
            except Exception:
                cur_dt = cur_seq = None
            if cur_dt is not None:
                for pseq, pdt_s, pdate in cur.fetchall():
                    try:
                        pdt = _dt.datetime.fromisoformat(pdt_s); pseq_i = int(pseq)
                    except Exception:
                        continue
                    if pdt == cur_dt:
                        continue
                    # daha sonraki işlemin numarası daha büyük olmalı
                    later, earlier = (f, {"seq": pseq_i, "date": pdate}) if cur_dt > pdt else \
                                     ({"seq": pseq_i, "date": pdate}, f)
                    later_seq = cur_seq if cur_dt > pdt else pseq_i
                    earlier_seq = pseq_i if cur_dt > pdt else cur_seq
                    if later_seq < earlier_seq:
                        findings.append({
                            "code": "SEQ_DB_ORDER", "severity": "high",
                            "tr": f"Zaman-numara çelişkisi ({f['bank']}): aynı gönderenin daha önce kayıtlı "
                                  f"bir işlemine göre, sonraki işlemin numarası artması gerekirken azalmış "
                                  f"({earlier_seq} → {later_seq}). Sıralı numaralandırmaya aykırı; incelenmeli.",
                            "en": f"Time-number contradiction ({f['bank']}): compared to a previously recorded "
                                  f"transaction from the same sender, the later transaction's number decreased "
                                  f"instead of increasing ({earlier_seq} → {later_seq}). Violates sequential "
                                  f"numbering; review recommended.",
                            "detail": f"{earlier_seq} -> {later_seq}",
                        })
                        break
    except Exception:
        pass
    finally:
        con.close()
    return findings


def record(report: dict) -> bool:
    """Dekont doğrulanmışsa (yüksek puan, kritik tahrifat yok, gerçek dekont) numaralarını kaydeder.
    Zaten kayıtlıysa (aynı sha256) veya uygun değilse False döner."""
    if not enabled():
        return False
    min_score = int(os.environ.get("DEKONT_STORE_MIN_SCORE", "80"))
    sc = report.get("score", {})
    cls = report.get("classification", {})
    if not cls.get("is_receipt", False):
        return False
    if (sc.get("authenticity_score") or 0) < min_score:
        return False
    if sc.get("not_a_receipt"):
        return False
    # kritik/yüksek tahrifat bulgusu varsa kaydetme
    codes = {x.get("code") for x in report.get("findings_en", [])}
    # Kesin tahrifat sinyalleri kaydı engeller. SEQ_DB_ORDER hariç: bazı bankalar sıralı
    # numaralandırma kullanmadığından (ör. Garanti), bu yalnızca 'incelenmeli' tavsiyesidir
    # ve kaydı engellemez — aksi halde veritabanı gerçek dekontlarla dolamaz.
    _BAD = {"REV_AMOUNT_CHANGED", "REV_CONTENT_CHANGED", "TIME_FILE_BEFORE_TXN", "SINGLE_PHOTO_PDF",
            "QR_MISMATCH", "SEQ_DB_DUPLICATE", "RECEIPT_NO_DATE_MISMATCH", "PRODUCER_MISMATCH",
            "AMOUNT_MISMATCH", "BROWSER_RERENDER", "FONT_BROWSER_RERENDER", "FONT_SET_MISMATCH",
            "INTERNAL_DATE_MISMATCH", "PDFIUM_PRODUCED",
            "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH"}
    if codes & _BAD:
        return False
    f = _fields(report)
    if not f["bank"] or not f["seq_number"] or not f["sha256"]:
        return False
    try:
        con = _connect()
    except Exception:
        return False
    try:
        con.execute(
            "INSERT OR IGNORE INTO receipts (bank, seq_number, seq_len, ref_no, document_no, "
            "sender_iban, sender_name, receiver_iban, amount, txn_date, txn_dt, sha256, score, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["bank"], f["seq_number"], f["seq_len"], f["ref_no"], f["document_no"],
             f["sender_iban"], f["sender_name"], f["receiver_iban"], f["amount"],
             f["txn_date"], f["txn_dt"], f["sha256"], f["score"], _dt.datetime.utcnow().isoformat()))
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


_FAKE_CODES = {"REV_AMOUNT_CHANGED", "REV_CONTENT_CHANGED", "TIME_FILE_BEFORE_TXN", "SINGLE_PHOTO_PDF",
               "QR_MISMATCH", "SEQ_DB_DUPLICATE", "RECEIPT_NO_DATE_MISMATCH", "PRODUCER_MISMATCH",
               "AMOUNT_MISMATCH", "BROWSER_RERENDER", "FONT_BROWSER_RERENDER", "FONT_SET_MISMATCH",
               "INTERNAL_DATE_MISMATCH", "PDFIUM_PRODUCED", "IBAN_INVALID", "ISSUER_IBAN_MISMATCH",
               "RECEIVER_BANK_MISMATCH", "STATEMENT_BALANCE_BREAK", "STATEMENT_ROW_COUNT_MISMATCH"}


def log_analysis(report: dict) -> bool:
    """HER analizi (sahte/gerçek fark etmez) audit log'a yazar. Aynı dosya (sha256) bir kez sayılır.
    'kaç dekont yüklendi' sorusunun ve kara-listenin temeli budur."""
    if not enabled():
        return False
    f = _fields(report)
    if not f["sha256"]:
        return False
    sc = report.get("score", {})
    cls = report.get("classification", {})
    codes = sorted({x.get("code") for x in report.get("findings_en", []) if x.get("code")})
    is_fake = 1 if (set(codes) & _FAKE_CODES) or (sc.get("authenticity_score") or 100) < 50 else 0
    try:
        con = _connect()
    except Exception:
        return False
    try:
        con.execute(
            "INSERT OR IGNORE INTO analyses (sha256, bank, is_receipt, score, risk, is_fake, "
            "seq_number, ref_no, document_no, amount, txn_date, codes, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["sha256"], f["bank"], 1 if cls.get("is_receipt") else 0,
             sc.get("authenticity_score"), sc.get("risk_level"), is_fake,
             f["seq_number"], f["ref_no"], f["document_no"], f["amount"], f["txn_date"],
             ",".join(c for c in codes if c in _FAKE_CODES), _dt.datetime.utcnow().isoformat()))
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


def check_blocklist(report: dict) -> list[dict]:
    """Yüklenen dosya, DAHA ÖNCE SAHTE olarak görülmüş bir belgeyle eşleşiyor mu?
    (a) Aynı dosya (sha256) daha önce sahte damgalandıysa, (b) aynı banka+sıra numarası
    daha önce sahte bir belgede görüldüyse -> KNOWN_FAKE."""
    if not enabled():
        return []
    f = _fields(report)
    out = []
    if not f["sha256"]:
        return out
    try:
        con = _connect()
    except Exception:
        return out
    try:
        r = con.execute("SELECT is_fake, codes FROM analyses WHERE sha256=? AND is_fake=1",
                        (f["sha256"],)).fetchone()
        hit_seq = None
        if not r and f["bank"] and f["seq_number"]:
            hit_seq = con.execute(
                "SELECT codes FROM analyses WHERE bank=? AND seq_number=? AND is_fake=1 AND sha256<>? LIMIT 1",
                (f["bank"], f["seq_number"], f["sha256"])).fetchone()
        if r or hit_seq:
            why = ("aynı dosya daha önce sahte olarak işaretlenmişti" if r
                   else f"aynı banka+sıra numarası ({f['seq_number']}) daha önce sahte bir belgede görüldü")
            out.append({
                "code": "KNOWN_FAKE", "severity": "critical", "weight": 60,
                "tr": f"KARA LİSTE: Bu belge daha önce SAHTE olarak tespit edilmiş bir belgeyle eşleşiyor "
                      f"({why}). Bilinen sahte — yüksek risk.",
                "en": f"BLOCKLIST: this document matches a previously flagged forgery ({why}). Known fake.",
                "detail": f"sha={f['sha256'][:12]} bank={f['bank']} seq={f['seq_number']}",
            })
    except Exception:
        pass
    finally:
        con.close()
    return out


def stats() -> dict:
    if not enabled():
        return {"enabled": False, "count": 0, "banks": []}
    try:
        con = _connect()
        try:
            n = con.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
            banks_rows = con.execute(
                "SELECT bank, COUNT(*) c FROM receipts GROUP BY bank ORDER BY c DESC").fetchall()
            banks = [r[0] for r in banks_rows]
            banks_detail = [{"bank": r[0], "count": r[1]} for r in banks_rows]
            rng = con.execute("SELECT MIN(txn_date), MAX(txn_date), "
                              "ROUND(SUM(amount),2), ROUND(AVG(amount),2) FROM receipts").fetchone()
            out = {"enabled": True, "count": n, "banks": banks, "banks_detail": banks_detail,
                   "amount_total": rng[2], "amount_avg": rng[3], "db_path": _db_path()}
            # AUDIT: tüm yüklemeler (sahte dahil)
            try:
                total = con.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
                fake = con.execute("SELECT COUNT(*) FROM analyses WHERE is_fake=1").fetchone()[0]
                by_bank = [{"bank": r[0], "count": r[1], "fake": r[2]} for r in con.execute(
                    "SELECT COALESCE(NULLIF(bank,''),'(bilinmiyor)') b, COUNT(*), SUM(is_fake) "
                    "FROM analyses GROUP BY b ORDER BY COUNT(*) DESC").fetchall()]
                out["audit"] = {"total_uploads": total, "fake": fake, "genuine": total - fake,
                                "by_bank": by_bank}
            except Exception:
                pass
            return out
        finally:
            con.close()
    except Exception as e:
        return {"enabled": True, "count": 0, "banks": [], "error": str(e)}

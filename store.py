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
        sha256 TEXT UNIQUE, score INTEGER, created_at TEXT,
        fee REAL, rail TEXT )""")
    # Eski veritabanları için idempotent sütun ekleme (fee/rail/qr_found sonradan eklendi)
    for _col, _typ in (("fee", "REAL"), ("rail", "TEXT"), ("qr_found", "INTEGER")):
        try:
            con.execute(f"ALTER TABLE receipts ADD COLUMN {_col} {_typ}")
        except Exception:
            pass
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
    # ÖĞRENME: YZ değerlendiricisi bir alanı (ör. blank kalan alıcı adı) belgede HANGİ ETİKETİN
    # yanında bulduğunu bildirdikçe, bunu banka-bazlı öğrenilmiş İPUCU olarak biriktiririz. Sonraki
    # dekontlarda BLANK kritik alanlar bu ipuçlarıyla otomatik doldurulur (kod değişmeden 'öğren-uygula').
    con.execute("""CREATE TABLE IF NOT EXISTS field_hints (
        bank TEXT, field TEXT, label TEXT,
        hits INTEGER DEFAULT 1, last_at TEXT,
        PRIMARY KEY (bank, field, label) )""")
    # HIZ: SONUÇ ÖNBELLEĞİ — aynı dosya (sha256) + aynı motor sürümü ikinci kez yüklenirse tüm
    # işlem hattını (OCR + vision + YZ) atlayıp saklanan raporu ANINDA döndürürüz.
    con.execute("""CREATE TABLE IF NOT EXISTS report_cache (
        sha256 TEXT, engine_version TEXT, report_json TEXT, created_at TEXT,
        PRIMARY KEY (sha256, engine_version) )""")
    con.commit()
    _maybe_unblock(con)
    return con


_UNBLOCK_DONE = False


def _maybe_unblock(con) -> None:
    """DEKONT_UNBLOCK env'inde verilen sıra/sorgu numaralarını (virgülle) kara-listeden çıkarır.
    Yanlış-pozitif nedeniyle sahte damgalanmış gerçek dekontları temizlemek için (idempotent)."""
    global _UNBLOCK_DONE
    if _UNBLOCK_DONE:
        return
    _UNBLOCK_DONE = True
    vals = [s.strip() for s in os.environ.get("DEKONT_UNBLOCK", "").split(",") if s.strip()]
    if not vals:
        return
    try:
        # TAM TEMİZLİK: DEKONT_UNBLOCK içinde "ALL" (büyük/küçük fark etmez) varsa
        # tüm kara-listeyi tek seferde temizle (audit kaydı/sayaç korunur, sadece is_fake=0).
        if any(v.upper() == "ALL" for v in vals):
            con.execute("UPDATE analyses SET is_fake=0 WHERE is_fake=1")
            con.commit()
            return
        for v in vals:
            con.execute("UPDATE analyses SET is_fake=0 WHERE (seq_number=? OR ref_no=? OR "
                        "document_no=? OR sha256=?) AND is_fake=1", (v, v, v, v))
        con.commit()
    except Exception:
        pass


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
        "fee": ex.get("amount", {}).get("fee"),
        "rail": tx.get("rail") or "",
        "qr_found": 1 if (report.get("qr", {}) or {}).get("found") else 0,
    }


def cache_enabled() -> bool:
    return enabled() and os.environ.get("DEKONT_CACHE", "1") != "0"


def _cache_key_ver(engine_version: str) -> str:
    """Önbellek sürüm anahtarı = motor sürümü + DEKONT_CACHE_SALT. Kod düzeltilip aynı dekont
    tekrar yüklendiğinde ESKİ sonucun dönmemesi için: DEKONT_CACHE_SALT'ı değiştirmek (ya da
    ENGINE_VERSION'ı artırmak) tüm önbelleği anında geçersizler."""
    return f"{engine_version or ''}|{os.environ.get('DEKONT_CACHE_SALT', '')}"


def cache_get(sha256: str, engine_version: str) -> dict | None:
    """Aynı dosya+motor sürümü için saklanan raporu döndürür (yoksa None). 'Öğren' değil, HIZ önbelleği."""
    if not sha256 or not cache_enabled():
        return None
    try:
        con = _connect()
        try:
            row = con.execute("SELECT report_json FROM report_cache WHERE sha256=? AND engine_version=?",
                              (sha256, _cache_key_ver(engine_version))).fetchone()
        finally:
            con.close()
        if row and row[0]:
            import json as _j
            r = _j.loads(row[0])
            if isinstance(r, dict):
                r["_from_cache"] = True
                return r
    except Exception:
        return None
    return None


def cache_put(sha256: str, engine_version: str, report: dict) -> bool:
    """Raporu önbelleğe yazar (sha256+motor sürümü anahtarıyla). En fazla ~5000 kayıt tutulur."""
    if not sha256 or not report or not cache_enabled():
        return False
    try:
        import json as _j
        payload = _j.dumps(report, ensure_ascii=False)[:400000]   # aşırı büyük raporları sınırla
        con = _connect()
        try:
            con.execute("INSERT OR REPLACE INTO report_cache (sha256, engine_version, report_json, created_at) "
                        "VALUES (?,?,?,?)", (sha256, _cache_key_ver(engine_version), payload, _dt.datetime.utcnow().isoformat()))
            # basit budama: 5000'i aşarsa en eskileri sil
            con.execute("DELETE FROM report_cache WHERE rowid IN "
                        "(SELECT rowid FROM report_cache ORDER BY created_at DESC LIMIT -1 OFFSET 5000)")
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def record_field_hint(bank: str, field: str, label: str) -> bool:
    """YZ değerlendiricisinin doğruladığı 'bu bankada <field> alanı <label> etiketinin yanındadır'
    ipucunu kalıcı olarak biriktirir (hit sayacı artar). 'Öğren' adımı."""
    bank = (bank or "").strip().lower()
    field = (field or "").strip()
    label = re.sub(r"\s+", " ", (label or "")).strip()
    if not bank or not field or not label or len(label) > 60:
        return False
    if not enabled():
        return False
    try:
        con = _connect()
        try:
            con.execute(
                "INSERT INTO field_hints (bank, field, label, hits, last_at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(bank, field, label) DO UPDATE SET hits=hits+1, last_at=excluded.last_at",
                (bank, field, label, _dt.datetime.utcnow().isoformat()))
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def learned_field_hints(bank: str) -> dict:
    """Bir banka için öğrenilmiş {field: [label,...]} ipuçlarını (güven=hit sırasına göre) döndürür.
    'Uygula' adımı bu ipuçlarını BLANK kritik alanları doldurmak için kullanır."""
    bank = (bank or "").strip().lower()
    if not bank or not enabled():
        return {}
    out: dict = {}
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT field, label FROM field_hints WHERE bank=? ORDER BY hits DESC, last_at DESC",
                (bank,)).fetchall()
        finally:
            con.close()
        for field, label in rows:
            out.setdefault(field, [])
            if label not in out[field]:
                out[field].append(label)
    except Exception:
        return {}
    return out


def learned_rail_fees(bank_display: str) -> dict:
    """Bir banka için, GEÇMİŞTE kaydedilmiş GERÇEK (yüksek skorlu) dekontlardan
    işlem türü (rail) başına gözlenen ücret değerlerini döndürür: {rail: [fee,...]}.
    check_fee_rail bunları seed tarifelerle birleştirir → tarife tablosu tüm bankalar
    için gerçek dekontlardan otomatik öğrenilir. Yalnızca score>=90 kayıtlar kullanılır."""
    key = re.sub(r"\s+", " ", (bank_display or "")).strip().upper()
    if not key:
        return {}
    out: dict = {}
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT rail, fee FROM receipts WHERE UPPER(bank)=? AND rail IS NOT NULL "
                "AND rail<>'' AND fee IS NOT NULL AND fee>0 AND score>=90", (key,)).fetchall()
        finally:
            con.close()
        for rail, fee in rows:
            out.setdefault(rail, [])
            fv = round(float(fee), 2)
            if fv not in out[rail]:
                out[rail].append(fv)
    except Exception:
        return {}
    return out


def max_amount_for_rail(rail: str, min_score: int = 90) -> float | None:
    """Bir işlem türü (rail) için GERÇEK (yüksek skorlu) dekontlarda gözlenen EN YÜKSEK
    tutarı döndürür (sistem geneli, tüm bankalar). FAST üst limiti zamanla artabildiği
    için sabit sayı yerine gerçek dekontlardan öğrenilen tavanı sağlar → yanlış-pozitifi önler."""
    if not rail:
        return None
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT MAX(amount) FROM receipts WHERE rail=? AND amount IS NOT NULL AND score>=?",
                (rail, min_score)).fetchone()
        finally:
            con.close()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def qr_expected(bank_display: str, min_samples: int = 5) -> bool:
    """Bir bankanın GERÇEK (yüksek skorlu) dekontlarında QR HER ZAMAN varsa True.
    Sıfır-FP: en az `min_samples` gerçek dekont ve %100 QR varlığı şartı. Veri
    birikene kadar False döner → 'beklenen QR eksik' bayrağı erken tetiklenmez."""
    key = re.sub(r"\s+", " ", (bank_display or "")).strip().upper()
    if not key:
        return False
    try:
        con = _connect()
        try:
            row = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(qr_found),0) FROM receipts "
                "WHERE UPPER(bank)=? AND score>=90 AND qr_found IS NOT NULL", (key,)).fetchone()
        finally:
            con.close()
    except Exception:
        return False
    if not row:
        return False
    total, with_qr = int(row[0] or 0), int(row[1] or 0)
    return total >= min_samples and with_qr == total


def sequence_anomaly(bank_display: str, seq_number: str, txn_iso: str, sha256: str = "") -> dict | None:
    """Dekont/sıra numarası ↔ işlem tarihi monotonluğu. Bir bankanın numaraları zamanla
    ARTAR: daha erken işlemin numarası daha küçük olmalıdır. Bu dekontun numarası, iddia
    ettiği tarihe göre SIRA DIŞIYSA (ör. daha eski tarih ama daha büyük numara) tahrifat
    işaretidir. Yalnızca numarası GLOBAL MONOTON sayaç olan bankalarda (Enpara/QNB) çalışır;
    yeterli veri (>=4) ve NET bir tersinme (zaman ve numara farkı belirgin) yoksa tetiklenmez."""
    key = re.sub(r"\s+", " ", (bank_display or "")).strip().upper()
    if not key or not seq_number or not str(seq_number).isdigit() or not txn_iso:
        return None
    # Yalnızca GLOBAL MONOTON sayaçlı bankalar (Enpara/QNB — önceki analizle tespit edildi).
    if not any(t in key for t in ("ENPARA", "QNB")):
        return None
    try:
        n = int(seq_number)
        t0 = _dt.datetime.fromisoformat(txn_iso)
    except Exception:
        return None
    try:
        con = _connect()
        try:
            rows = con.execute(
                "SELECT seq_number, txn_dt FROM receipts WHERE UPPER(bank)=? AND score>=95 "
                "AND seq_number GLOB '[0-9]*' AND txn_dt<>'' AND sha256<>?", (key, sha256)).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    pts = []
    for s, td in rows:
        try:
            pts.append((int(s), _dt.datetime.fromisoformat(td)))
        except Exception:
            continue
    if len(pts) < 12:                       # yeterli gerçek veri yok -> KAPALI
        return None
    # KENDİ-KALİBRASYON KAPISI: bankanın GERÇEK verisinde numara↔zaman gerçekten monoton mu?
    # Spearman rank korelasyonu düşükse (veri gürültülü / şube-bazlı / test verisi) denetim
    # KENDİNİ KAPATIR — yanlış-pozitif üretmez.
    seqs = [p[0] for p in pts]
    times = [p[1].timestamp() for p in pts]
    if _spearman(times, seqs) < 0.90:
        return None
    # Bu dekont, monoton trende karşı ÇOK sayıda tersinme yaratıyor mu? (tek tersinme yetmez)
    H = _dt.timedelta(hours=6)
    inv = 0
    worst = None
    for s, td in pts:
        if (s < n and td > t0 + H) or (s > n and td < t0 - H):
            inv += 1
            if worst is None:
                worst = (s, td)
    if inv >= 4 and inv / len(pts) >= 0.30 and worst is not None:
        return _seq_finding(n, t0, worst[0], worst[1], key)
    return None


def _spearman(x: list, y: list) -> float:
    """Spearman rank korelasyonu (numpy). Boş/sabit dizide 0 döner."""
    try:
        import numpy as _np
        if len(x) < 3:
            return 0.0
        rx = _np.argsort(_np.argsort(_np.asarray(x, dtype=float)))
        ry = _np.argsort(_np.argsort(_np.asarray(y, dtype=float)))
        if rx.std() == 0 or ry.std() == 0:
            return 0.0
        return float(_np.corrcoef(rx, ry)[0, 1])
    except Exception:
        return 0.0


def _seq_finding(n, t0, s, td, key):
    return {
        "code": "SEQ_DATE_INVERSION", "severity": "high", "weight": 16,
        "tr": f"NUMARA–TARİH ÇELİŞKİSİ: bu dekontun sıra numarası ({n}) ile işlem tarihi ({t0:%d.%m.%Y %H:%M}) "
              f"bankanın numara sırasına AYKIRI. Aynı bankada numara {s} olan işlem {td:%d.%m.%Y %H:%M} "
              f"tarihli — numaralar zamanla artmalıyken burada sıra ters. İşlem tarihi ya da numarası "
              f"sonradan değiştirilmiş (güçlü sahtecilik işareti).",
        "en": f"NUMBER–DATE INVERSION: this receipt's sequence number ({n}) conflicts with its transaction "
              f"date ({t0:%d.%m.%Y %H:%M}). A receipt numbered {s} at the same bank is dated {td:%d.%m.%Y %H:%M}; "
              f"numbers must increase over time but the order is inverted here — the date or number was altered.",
        "detail": f"n={n} t0={t0.isoformat()} other=({s},{td.isoformat()})"}


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
            "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH",
            "FEE_RAIL_MISMATCH", "RAIL_SAMEBANK_MISMATCH",
            "DATE_IN_FUTURE", "RECEIPT_BEFORE_TXN", "IMAGE_EDITOR_SOFTWARE",
            "ID_CHECKSUM_INVALID", "SELF_TRANSFER"}
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
            "sender_iban, sender_name, receiver_iban, amount, txn_date, txn_dt, sha256, score, created_at, "
            "fee, rail, qr_found) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["bank"], f["seq_number"], f["seq_len"], f["ref_no"], f["document_no"],
             f["sender_iban"], f["sender_name"], f["receiver_iban"], f["amount"],
             f["txn_date"], f["txn_dt"], f["sha256"], f["score"], _dt.datetime.utcnow().isoformat(),
             f.get("fee"), f.get("rail"), f.get("qr_found", 0)))
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
               "RECEIVER_BANK_MISMATCH", "STATEMENT_BALANCE_BREAK", "STATEMENT_ROW_COUNT_MISMATCH",
               "FEE_RAIL_MISMATCH", "RAIL_SAMEBANK_MISMATCH",
               "DATE_IN_FUTURE", "RECEIPT_BEFORE_TXN", "IMAGE_EDITOR_SOFTWARE",
            "ID_CHECKSUM_INVALID", "SELF_TRANSFER"}


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
    # KARA-LİSTE'ye YALNIZCA kanıtlanmış tahrifat koduyla eklenir. Sadece "düşük skor"
    # (AI/görsel yumuşak sinyaller vb.) KALICI + çapraz-dosya (banka+sıra) kara-liste
    # kaydı OLUŞTURMAZ — aksi halde iyi niyetli bir düşük skor, aynı sıra numarasını
    # taşıyan gerçek dekontları da yanlışlıkla "bilinen sahte" yapar (FP çoğaltıcı).
    # Böyle dosyalar zaten her yüklemede kendi bulgularıyla yeniden yakalanır.
    is_fake = 1 if (set(codes) & _FAKE_CODES) else 0
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
                   else f"aynı banka+sıra numarası ({f['seq_number']}) daha önce sahte işaretlenmiş bir belgede görüldü")
            # ÖNEMLİ: Kara-liste eşleşmesi ARTIK tek başına "sahte" hükmü DEĞİLDİR. Bir belgenin
            # daha önce sahte damgalanmış olması, bu sefer de otomatik sahte sayılmasına yol açmaz
            # (eski yanlış-pozitifler zincirleme ceza üretmesin). Bilgi olarak verilir; belge YİNE DE
            # kendi güncel bulgularıyla bağımsız değerlendirilir. severity=info, weight=0 -> skora etkisi yok.
            out.append({
                "code": "KNOWN_FAKE", "severity": "info", "weight": 0,
                "tr": f"BİLGİ — Kara-liste eşleşmesi: Bu belge daha önce SAHTE olarak işaretlenmiş bir belgeyle "
                      f"eşleşiyor ({why}). Bu tek başına sahte hükmü DEĞİLDİR; belge kendi güncel bulgularıyla "
                      f"bağımsız denetlendi. Yine de dikkatle gözden geçirilmesi önerilir.",
                "en": f"NOTE — Blocklist match: this document matches one previously flagged as fake ({why}). "
                      f"This alone is NOT a fake verdict; the document was evaluated independently on its own "
                      f"current findings. Manual review is still recommended.",
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

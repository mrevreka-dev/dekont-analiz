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


def _iban_valid_safe(ib: str):
    """banks.iban_valid'e güvenli erişim (dairesel import'a karşı lazy)."""
    try:
        from banks import iban_valid as _iv
        return _iv(ib)
    except Exception:
        return None


def _complete_for_reuse(f: dict) -> bool:
    """Bu dekont numara-tekrarı/kara-liste veritabanına GÜVENLE kaydedilecek/karşılaştırılacak kadar EKSİKSİZ
    ve DOĞRU mu? Kullanıcı kuralı: alıcı IBAN + alıcı adı + tutar NET okunduysa (ve bir işlem numarası varsa)
    sakla; okunamadıysa KAYDETME (yanlış/eksik veri DB'yi kirletip aynı dekonta yanlış-pozitif üretiyordu).
    Alıcı IBAN mod-97 GEÇERLİ olmalı — böylece bozuk/yanlış-atanan IBAN'lı kayıtlar dışarıda kalır."""
    try:
        _riban = re.sub(r"\s+", "", (f.get("receiver_iban") or "")).upper()
        if _iban_valid_safe(_riban) is not True:
            return False
        if not (f.get("receiver_name") or "").strip():
            return False
        if f.get("amount") is None:
            return False
        _num = any((str(f.get(k) or "").isdigit() and len(str(f.get(k))) >= 6)
                   for k in ("document_no", "ref_no", "seq_number"))
        return bool(_num)
    except Exception:
        return False

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
    # NUMARA-TEKRARI RAPORU İÇİN: önceki dekontun taraf/tutar detaylarını da tut (idempotent ekleme).
    for _col, _typ in (("sender_name", "TEXT"), ("receiver_name", "TEXT"), ("receiver_iban", "TEXT")):
        try:
            con.execute(f"ALTER TABLE analyses ADD COLUMN {_col} {_typ}")
        except Exception:
            pass
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
    # BANKA-İÇİ DEKONT HAFIZASI: her analiz edilen dekontun banka bazlı kanal/etiket özeti.
    con.execute("""CREATE TABLE IF NOT EXISTS bank_corpus (
        bank_key TEXT, sha256 TEXT, rail TEXT, billing TEXT, amount REAL, created_at TEXT,
        PRIMARY KEY (bank_key, sha256) )""")
    # TARAMA KAYDI: web/API'de yapılan her taramanın SİSTEMİN VERDİĞİ cevabı (alanlar + risk). Sonra
    # kullanıcı dekontu yüklediğinde 'web ne demişti vs gerçek ne' karşılaştırması için sorgu/ref ile aranır.
    con.execute("""CREATE TABLE IF NOT EXISTS scan_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT, bank TEXT, sorgu_no TEXT, ref_no TEXT,
        sender_name TEXT, receiver_name TEXT, sender_bank TEXT, receiver_bank TEXT,
        amount REAL, fee REAL, total REAL, rail TEXT, risk TEXT, score INTEGER,
        input_kind TEXT, findings TEXT, created_at TEXT )""")
    # TANI/HATA GÜNLÜĞÜ: HER analizin tanı bilgisi. Gün sonu bu tabloya + Railway loglarına bakıp
    # hata tespiti ve düzeltmesi yapılır. severity: info | warn | error.
    con.execute("""CREATE TABLE IF NOT EXISTS diag_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT, bank TEXT, input_kind TEXT,
        severity TEXT, extraction_empty INTEGER, ai_enabled INTEGER, ai_escalated INTEGER,
        ai_ok INTEGER, ai_verdict TEXT, ai_recovered INTEGER, vision_ok INTEGER,
        blocklist_hit INTEGER, visual_tamper INTEGER, score INTEGER, risk TEXT,
        codes TEXT, notes TEXT, elapsed_ms INTEGER, created_at TEXT )""")
    # BİLİNMEYEN BANKALAR: gönderici IBAN banka kodu tanınan listede olmayan dekontlar. Gün sonu bu tablo
    # gözden geçirilip banka IBAN_BANK_CODES listesine eklenir (kullanıcı kuralı: 'listeye ekleme komutu').
    con.execute("""CREATE TABLE IF NOT EXISTS unknown_banks (
        code TEXT PRIMARY KEY, ai_bank_name TEXT, rail TEXT, sample_sha256 TEXT,
        hit_count INTEGER, first_seen TEXT, last_seen TEXT )""")
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
        "receiver_name": (ex.get("receiver", {}).get("name") or "").strip(),
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


def bank_corpus_add(bank_key: str, sha256: str, rail: str, billing: str, amount) -> bool:
    """Analiz edilen dekontu BANKA-İÇİ hafızaya ekler (kanal/etiket özeti). Aynı dosya (sha) bir kez."""
    bank_key = (bank_key or "").strip().lower()
    if not bank_key or not sha256 or not enabled():
        return False
    try:
        con = _connect()
        try:
            con.execute("INSERT OR IGNORE INTO bank_corpus (bank_key, sha256, rail, billing, amount, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (bank_key, sha256, (rail or ""), (billing or "")[:120],
                         float(amount) if amount is not None else None, _dt.datetime.utcnow().isoformat()))
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def bank_corpus_rows(bank_key: str) -> list:
    """Bu bankanın hafızadaki (canlı) kayıtlarını döndürür: [{rail, billing, amount}, ...]."""
    bank_key = (bank_key or "").strip().lower()
    if not bank_key or not enabled():
        return []
    try:
        con = _connect()
        try:
            cur = con.execute("SELECT rail, billing, amount FROM bank_corpus WHERE bank_key=? "
                              "ORDER BY created_at DESC LIMIT 500", (bank_key,))
            return [{"rail": r[0], "billing": r[1], "amount": r[2]} for r in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def log_scan(report: dict) -> bool:
    """Web/API taramasının SİSTEM CEVABINI kaydeder (alanlar + risk). Sonra kullanıcı dekontu
    yükleyince 'web ne demişti vs gerçek ne' karşılaştırması için sorgu/ref/isim ile aranır."""
    if not enabled() or not report:
        return False
    try:
        ex = report.get("extracted", {}) or {}
        tx = ex.get("transaction", {}) or {}
        amt = ex.get("amount", {}) or {}
        sc = report.get("score", {}) or {}
        rail = next((c["code"].replace("RAIL_IS_", "").lower() for c in report.get("findings_tr", [])
                     if str(c.get("code", "")).startswith("RAIL_IS_")), "")
        fnd = ",".join(sorted({c.get("code", "") for c in report.get("findings_tr", []) if c.get("weight", 0) > 0}))
        con = _connect()
        try:
            con.execute(
                "INSERT INTO scan_log (sha256, bank, sorgu_no, ref_no, sender_name, receiver_name, "
                "sender_bank, receiver_bank, amount, fee, total, rail, risk, score, input_kind, findings, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (report.get("file", {}).get("sha256", ""), ex.get("bank", ""),
                 tx.get("ref_no", ""), tx.get("document_no", ""),
                 (ex.get("sender", {}) or {}).get("name", ""), (ex.get("receiver", {}) or {}).get("name", ""),
                 (ex.get("sender", {}) or {}).get("bank", ""), (ex.get("receiver", {}) or {}).get("bank", ""),
                 amt.get("value"), amt.get("fee"), amt.get("total"), rail,
                 sc.get("risk_level", ""), sc.get("authenticity_score"),
                 report.get("classification", {}).get("input_kind", ""), fnd[:400],
                 _dt.datetime.utcnow().isoformat()))
            con.execute("DELETE FROM scan_log WHERE id IN "
                        "(SELECT id FROM scan_log ORDER BY id DESC LIMIT -1 OFFSET 20000)")
            con.commit()
        finally:
            con.close()
        return True
    except Exception:
        return False


def scan_log_search(q: str, limit: int = 20) -> list:
    """Tarama kaydında arar: sorgu/ref no, gönderici/alıcı adı, banka ya da sha (parça). En yeni önce."""
    q = (q or "").strip()
    if not q or not enabled():
        return []
    try:
        like = f"%{q}%"
        con = _connect()
        try:
            cur = con.execute(
                "SELECT sha256,bank,sorgu_no,ref_no,sender_name,receiver_name,sender_bank,receiver_bank,"
                "amount,fee,total,rail,risk,score,input_kind,findings,created_at FROM scan_log "
                "WHERE sorgu_no LIKE ? OR ref_no LIKE ? OR sender_name LIKE ? OR receiver_name LIKE ? "
                "OR bank LIKE ? OR sha256 LIKE ? ORDER BY id DESC LIMIT ?",
                (like, like, like, like, like, like, int(limit)))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def scan_log_recent(limit: int = 20) -> list:
    """En son yapılan taramalar (en yeni önce)."""
    if not enabled():
        return []
    try:
        con = _connect()
        try:
            cur = con.execute(
                "SELECT sha256,bank,sorgu_no,ref_no,sender_name,receiver_name,sender_bank,receiver_bank,"
                "amount,fee,total,rail,risk,score,input_kind,findings,created_at FROM scan_log "
                "ORDER BY id DESC LIMIT ?", (int(limit),))
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()
    except Exception:
        return []


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


def _samples_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(_db_path())), "diag_samples")
    os.makedirs(d, exist_ok=True)
    return d


def save_diag_sample(sha256: str, data: bytes, ext: str = "bin", max_bytes: int = 30 * 1024 * 1024,
                     retain: int = 400) -> bool:
    """SORUNLU analizin HAM dosyasını (fotoğraf/PDF/video) kalıcı diske saklar — gün sonu tekrar
    tarayıp hatayı üretmek/düzeltmek için. Boyut sınırı aşılırsa saklamaz; retention: en eski
    dosyalar silinerek en fazla `retain` örnek tutulur (disk şişmesin)."""
    if not enabled() or not sha256 or not data:
        return False
    if len(data) > max_bytes:
        print(f"[diag_sample] atlandı (çok büyük {len(data)}B) sha={sha256[:10]}", flush=True)
        return False
    try:
        d = _samples_dir()
        ext = re.sub(r"[^a-z0-9]", "", (ext or "bin").lower())[:8] or "bin"
        path = os.path.join(d, f"{sha256}.{ext}")
        if not os.path.exists(path):
            with open(path, "wb") as fh:
                fh.write(data)
        # retention: en eskileri buda
        files = sorted((os.path.join(d, x) for x in os.listdir(d)), key=lambda p: os.path.getmtime(p))
        for old in files[:-retain] if len(files) > retain else []:
            try:
                os.remove(old)
            except Exception:
                pass
        return True
    except Exception as e:
        print(f"[diag_sample] hata: {type(e).__name__}: {e}", flush=True)
        return False


def diag_samples_recent(limit: int = 200) -> list:
    """Saklanan sorunlu dosya örneklerini (yeni→eski) listeler: sha, uzantı, boyut, zaman."""
    if not enabled():
        return []
    try:
        d = _samples_dir()
        out = []
        for x in os.listdir(d):
            p = os.path.join(d, x)
            if not os.path.isfile(p):
                continue
            sha, _, ext = x.partition(".")
            st = os.stat(p)
            out.append({"sha256": sha, "ext": ext, "bytes": st.st_size,
                        "saved_at": _dt.datetime.utcfromtimestamp(st.st_mtime).isoformat()})
        out.sort(key=lambda r: r["saved_at"], reverse=True)
        return out[:int(limit)]
    except Exception:
        return []


def diag_sample_file(sha256: str):
    """Verilen sha için saklanan örnek dosyanın (yol, uzantı)'ını döndürür; yoksa (None, None)."""
    if not enabled() or not sha256:
        return None, None
    try:
        d = _samples_dir()
        for x in os.listdir(d):
            if x.startswith(sha256 + "."):
                return os.path.join(d, x), x.partition(".")[2]
    except Exception:
        pass
    return None, None


def log_unknown_bank(code: str, ai_bank_name: str = "", rail: str = "", sample_sha256: str = "") -> bool:
    """Bilinmeyen banka kodunu (gönderici IBAN kodu listede yok) kaydeder → gün sonu IBAN_BANK_CODES'a
    eklenmek üzere. Aynı kod tekrar gelirse hit_count artar, YZ'nin bulduğu banka adı/rail güncellenir."""
    if not enabled() or not code:
        return False
    try:
        con = _connect()
    except Exception:
        return False
    try:
        now = _dt.datetime.utcnow().isoformat()
        row = con.execute("SELECT hit_count FROM unknown_banks WHERE code=?", (code,)).fetchone()
        if row:
            con.execute("UPDATE unknown_banks SET hit_count=hit_count+1, last_seen=?, "
                        "ai_bank_name=COALESCE(NULLIF(?,''), ai_bank_name), "
                        "rail=COALESCE(NULLIF(?,''), rail), "
                        "sample_sha256=COALESCE(NULLIF(?,''), sample_sha256) WHERE code=?",
                        (now, ai_bank_name or "", rail or "", sample_sha256 or "", code))
        else:
            con.execute("INSERT INTO unknown_banks (code, ai_bank_name, rail, sample_sha256, hit_count, "
                        "first_seen, last_seen) VALUES (?,?,?,?,1,?,?)",
                        (code, ai_bank_name or "", rail or "", sample_sha256 or "", now, now))
        con.commit()
        return True
    except Exception:
        return False


def unknown_banks_recent(limit: int = 200) -> list:
    """Kaydedilmiş bilinmeyen banka kodları (gün sonu listeye eklemek için). En çok görülenler önce."""
    if not enabled():
        return []
    try:
        con = _connect()
        rows = con.execute("SELECT code, ai_bank_name, rail, sample_sha256, hit_count, first_seen, last_seen "
                           "FROM unknown_banks ORDER BY hit_count DESC, last_seen DESC LIMIT ?",
                           (int(limit),)).fetchall()
        return [{"code": r[0], "ai_bank_name": r[1], "rail": r[2], "sample_sha256": r[3],
                 "hit_count": r[4], "first_seen": r[5], "last_seen": r[6]} for r in rows]
    except Exception:
        return []


def log_diag(d: dict) -> bool:
    """HER analizin tanı bilgisini kalıcı diag_log tablosuna yazar (gün sonu hata inceleme/düzeltme için).
    d: sha256, bank, input_kind, severity, extraction_empty, ai_enabled, ai_escalated, ai_ok, ai_verdict,
    ai_recovered, vision_ok, blocklist_hit, visual_tamper, score, risk, codes(list), notes, elapsed_ms."""
    if not enabled():
        return False
    try:
        con = _connect()
    except Exception:
        return False
    try:
        _codes = d.get("codes")
        if isinstance(_codes, (list, tuple, set)):
            _codes = ",".join(str(c) for c in _codes)
        con.execute(
            "INSERT INTO diag_log (sha256, bank, input_kind, severity, extraction_empty, ai_enabled, "
            "ai_escalated, ai_ok, ai_verdict, ai_recovered, vision_ok, blocklist_hit, visual_tamper, "
            "score, risk, codes, notes, elapsed_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.get("sha256"), d.get("bank"), d.get("input_kind"), d.get("severity", "info"),
             int(bool(d.get("extraction_empty"))), int(bool(d.get("ai_enabled"))),
             int(bool(d.get("ai_escalated"))), int(bool(d.get("ai_ok"))), d.get("ai_verdict"),
             int(bool(d.get("ai_recovered"))), int(bool(d.get("vision_ok"))),
             int(bool(d.get("blocklist_hit"))), int(bool(d.get("visual_tamper"))),
             d.get("score"), d.get("risk"), _codes, (d.get("notes") or "")[:1000], d.get("elapsed_ms"),
             _dt.datetime.utcnow().isoformat()))
        con.commit()
        return True
    except Exception:
        return False
    finally:
        con.close()


def diag_log_recent(limit: int = 100, only_problems: bool = False) -> list:
    """Son tanı kayıtlarını döndürür (gün sonu inceleme). only_problems=True ise yalnız warn/error
    ya da sorun içerenler (çıkarım boş / YZ başarısız / kurtarılmış / görsel tahrifat)."""
    if not enabled():
        return []
    try:
        con = _connect()
    except Exception:
        return []
    try:
        q = ("SELECT created_at, bank, input_kind, severity, extraction_empty, ai_ok, ai_verdict, "
             "ai_recovered, vision_ok, blocklist_hit, visual_tamper, score, risk, codes, notes, "
             "elapsed_ms, sha256 FROM diag_log ")
        if only_problems:
            q += ("WHERE severity IN ('warn','error') OR extraction_empty=1 OR ai_ok=0 OR "
                  "ai_recovered=1 OR vision_ok=0 ")
        q += "ORDER BY id DESC LIMIT ?"
        rows = con.execute(q, (int(limit),)).fetchall()
        cols = ["created_at", "bank", "input_kind", "severity", "extraction_empty", "ai_ok",
                "ai_verdict", "ai_recovered", "vision_ok", "blocklist_hit", "visual_tamper",
                "score", "risk", "codes", "notes", "elapsed_ms", "sha256"]
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        return []
    finally:
        con.close()


def check_number_reuse(report: dict) -> list[dict]:
    """BANKA-BAZLI NUMARA TEKRARI — bu dekonttaki tanımlayıcı numaralardan (işlem/doküman no,
    sıra/sorgu no, referans no) herhangi biri, AYNI bankada daha önce analiz edilmiş FARKLI bir
    dosyada (sha256) görülmüş mü? Bankalar her işleme BENZERSİZ numara verir; aynı numaranın başka
    bir belgede tekrar etmesi numaranın kopyalandığını/uydurulduğunu gösterir → sahtecilik.

    Mevcut SEQ_DB_DUPLICATE yalnız TEMİZ (kaydedilmiş) dekontların 'receipts' tablosuna bakar ve
    yalnız seq_number'ı karşılaştırır. Bu denetim ise HER analizi (sahte dahil) tutan 'analyses'
    tablosuna bakar ve seq/ref/document alanlarının HEPSİNİ karşılaştırır. Böylece ikisi de
    reddedilmiş (ör. RECEIVER_BANK_MISMATCH'li) iki dekont aynı işlem numarasını taşısa bile,
    ikinci/üçüncü tekrar burada yakalanır. HER BANKA KENDİ İÇİNDE değerlendirilir (banka zorunlu).

    YANLIŞ-POZİTİF KORUMASI: AYNI dekont tekrar tarandığında (dosya yeniden kaydedilip sha256 değişse
    bile) numara + tutar + alıcı AYNI olur → bu aynı işlemdir, bulgu ÜRETİLMEZ. Yalnız numara aynı iken
    tutar ya da alıcı POZİTİF olarak FARKLIYSA (kopyala-yapıştır) sahtecilik olarak işaretlenir."""
    if not enabled():
        return []
    f = _fields(report)
    out = []
    bank = f["bank"]
    sha = f["sha256"]
    if not bank or not sha:
        return out
    # EKSİKSİZLİK KAPISI: MEVCUT dekont eksiksiz+doğru okunmadıysa (alıcı IBAN geçerli + alıcı adı + tutar)
    # numara-tekrarı KARŞILAŞTIRMASI YAPMA — güvenilmez veriyle kıyas yanlış-pozitif üretir. Zaten böyle
    # okumalar DB'ye de kaydedilmez (bkz. log_analysis). Hem depolama hem karşılaştırma aynı kuralı izler.
    if not _complete_for_reuse(f):
        return out
    # Tanımlayıcı numaralar (alan etiketi + değer). Yalnız 6+ haneli gerçek numaralar; kısa/ortak
    # sayılar (şube kodu vb.) yanlış-pozitif üretmesin diye elenir.
    nums = []
    for label_tr, label_en, val in (
        ("işlem/doküman no", "transaction/document no", f["document_no"]),
        ("sıra/sorgu no", "sequence/query no", f["seq_number"]),
        ("referans no", "reference no", f["ref_no"]),
    ):
        v = re.sub(r"\s+", "", str(val or ""))
        if v and v.isdigit() and len(v) >= 6:
            nums.append((label_tr, label_en, v))
    if not nums:
        return out
    try:
        con = _connect()
    except Exception:
        return out
    try:
        def _round2(a):
            try:
                return round(float(a), 2)
            except Exception:
                return None

        def _norm_name(s):
            return re.sub(r"\s+", " ", (s or "").strip()).upper()

        def _daykey(s):
            # işlem tarihini gün bazında karşılaştır (saat farkı OCR kaynaklı olabilir → günü esas al)
            dt, _ = parse_content_datetime(s or "")
            return dt.date().isoformat() if dt else None

        _cur_amt = _round2(f.get("amount"))
        _cur_riban = re.sub(r"\s+", "", (f.get("receiver_iban") or "")).upper()
        _cur_rname = _norm_name(f.get("receiver_name"))
        _cur_day = _daykey(f.get("txn_date"))

        seen = set()
        for label_tr, label_en, v in nums:
            if v in seen:
                continue
            # AYNI banka, FARKLI dosya (sha); numara seq/ref/document alanlarından birinde geçiyor mu?
            rows = con.execute(
                "SELECT sha256, amount, txn_date, sender_name, receiver_name, receiver_iban, created_at "
                "FROM analyses WHERE bank=? AND sha256<>? AND (seq_number=? OR ref_no=? OR document_no=?) "
                "ORDER BY id ASC LIMIT 20",
                (bank, sha, v, v, v)).fetchall()
            if not rows:
                continue
            # AYNI DEKONTUN TEKRAR TARANMASI yanlış-pozitif üretmesin. Farklılık kararı YALNIZCA EN KARARLI
            # tanımlayıcılara dayanır: (1) TUTAR (re-taramada hep aynıdır; sahtecilikte hemen hemen her zaman
            # farklıdır), (2) kesin GEÇERLİ (mod-97) ve farklı ALICI IBAN. İşlem TARİHİ ve alıcı ADI FARK
            # KRİTERİNDEN ÇIKARILDI: bunlar re-taramalar arasında değişebiliyor (OCR/AI okuma farkı, valör↔
            # işlem tarihi karışması, isim maskeleme) ve aynı dekontta YANLIŞ 'sahte' üretiyordu. Yani numara
            # aynı iken TUTAR pozitif farklıysa YA DA iki taraf da geçerli-IBAN olup farklıysa → kopyala-yapıştır
            # sahteciliği; aksi halde AYNI işlem varsayılır ve bulgu verilmez.
            # FARKLILIK KARARI: TUTAR ya da İŞLEM TARİHİ (GÜN). Kullanıcı kuralı: sahtecilik SADECE tarih
            # değiştirilerek de yapılabilir → yalnız tutara bakmak YANLIŞ. Kayıtlar artık YALNIZ EKSİKSİZ+DOĞRU
            # okunan dekontlardan oluştuğu için (bkz. _complete_for_reuse) tarih/tutar re-taramada KARARLIDIR;
            # aynı numara + tutar aynı + tarih(gün) aynı → AYNI dekont (bulgu YOK). Aynı numara iken tutar
            # POZİTİF farklı YA DA tarih(gün) farklı → kopyala-yapıştır sahteciliği (tutar VEYA sadece-tarih).
            # (Alıcı IBAN farkı ölçüt DIŞI — OCR varyansına en açık alan.)
            _forgery = None
            for pr in rows:
                p_amt = _round2(pr[1])
                p_day = _daykey(pr[2])
                amt_diff = (_cur_amt is not None and p_amt is not None and _cur_amt != p_amt)
                day_diff = bool(_cur_day and p_day and _cur_day != p_day)
                if amt_diff or day_diff:
                    _forgery = pr
                    break
            if _forgery is None:
                # tüm önceki kayıtlar AYNI işlemi gösteriyor → aynı dekontun tekrar taranması, bulgu yok
                seen.add(v)
                continue
            r = _forgery
            if True:
                seen.add(v)
                _amt = r[1] if r[1] is not None else "—"
                _dtx = r[2] or "—"
                _snd = r[3] or "—"
                _rcv = r[4] or "—"
                _riban = r[5] or "—"
                _seen_at = (r[6] or "")[:10] or "—"
                _onceki_tr = (f"ÖNCEKİ DEKONT — gönderen: {_snd}; alıcı: {_rcv}; alıcı IBAN: {_riban}; "
                              f"tutar: {_amt}; işlem tarihi: {_dtx}; sisteme ilk görülme: {_seen_at}")
                _prev_en = (f"EARLIER RECEIPT — sender: {_snd}; receiver: {_rcv}; receiver IBAN: {_riban}; "
                            f"amount: {_amt}; txn date: {_dtx}; first seen: {_seen_at}")
                out.append({
                    "code": "NUMBER_REUSE", "severity": "critical",
                    "tr": f"BANKA-BAZLI NUMARA TEKRARI ({bank}): bu dekonttaki {label_tr} ({v}) daha önce "
                          f"analiz edilmiş FARKLI bir dekontta da görülmüş. {bank} her işleme benzersiz numara "
                          f"verir; aynı numaranın başka bir belgede tekrar etmesi numaranın kopyalandığını/"
                          f"uydurulduğunu gösterir. Yüksek sahtecilik riski. {_onceki_tr}.",
                    "en": f"BANK-SCOPED NUMBER REUSE ({bank}): the {label_en} on this receipt ({v}) was already "
                          f"seen on a DIFFERENT previously-analyzed receipt. {bank} assigns a unique number per "
                          f"transaction; a repeat on another document indicates the number was copied/fabricated. "
                          f"High forgery risk. {_prev_en}.",
                    "detail": f"bank={bank} num={v} vs sha={r[0][:12]} snd={_snd} rcv={_rcv} amt={_amt}",
                    "onceki_dekont": {"sender_name": r[3] or "", "receiver_name": r[4] or "",
                                      "receiver_iban": r[5] or "", "amount": r[1], "txn_date": r[2] or "",
                                      "first_seen": r[6] or "", "sha256": r[0]},
                })
    except Exception:
        pass
    finally:
        con.close()
    return out


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
            "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH", "SENDER_BANK_MISMATCH",
            "FEE_RAIL_MISMATCH", "RAIL_SAMEBANK_MISMATCH", "NUMBER_REUSE",
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
               "RECEIVER_BANK_MISMATCH", "SENDER_BANK_MISMATCH", "STATEMENT_BALANCE_BREAK", "STATEMENT_ROW_COUNT_MISMATCH",
               "FEE_RAIL_MISMATCH", "RAIL_SAMEBANK_MISMATCH", "NUMBER_REUSE",
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
    # EKSİKSİZLİK KAPISI (kullanıcı kuralı): Alıcı IBAN + alıcı adı + tutar NET okunduysa (ve işlem numarası
    # varsa) tanımlayıcı numaraları KAYDET; okunamadıysa numaraları SAKLAMA (boş yaz). Böylece eksik/yanlış
    # okunmuş bir dekont, numara-tekrarı (NUMBER_REUSE) karşılaştırmasında başka dekontlarla EŞLEŞİP yanlış
    # 'sahte' üretemez. Denetim satırı (kaç yüklendi + is_fake/kara-liste) yine yazılır.
    _complete = _complete_for_reuse(f)
    _seq = f["seq_number"] if _complete else ""
    _ref = f["ref_no"] if _complete else ""
    _doc = f["document_no"] if _complete else ""
    _amt_store = f["amount"] if _complete else None
    _rib_store = f.get("receiver_iban") if _complete else ""
    try:
        con = _connect()
    except Exception:
        return False
    try:
        con.execute(
            "INSERT OR IGNORE INTO analyses (sha256, bank, is_receipt, score, risk, is_fake, "
            "seq_number, ref_no, document_no, amount, txn_date, codes, created_at, "
            "sender_name, receiver_name, receiver_iban) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f["sha256"], f["bank"], 1 if cls.get("is_receipt") else 0,
             sc.get("authenticity_score"), sc.get("risk_level"), is_fake,
             _seq, _ref, _doc, _amt_store, f["txn_date"],
             ",".join(c for c in codes if c in _FAKE_CODES), _dt.datetime.utcnow().isoformat(),
             f.get("sender_name"), f.get("receiver_name"), _rib_store))
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
        # AYNI DOSYANIN (sha256) tekrar taranması KARA-LİSTE göstergesi ÜRETMEZ: belge zaten kendi GÜNCEL
        # bulgularıyla yeniden değerlendirilir; "daha önce sahte demiştik" etiketi (özellikle eski yanlış-
        # pozitiflerden, ör. bozuk dönemde OCR'ın DATE_IN_FUTURE'ı) kafa karıştırır ve aynı dekontu haksız
        # yere kara-listede gösterir. Kara-liste YALNIZ FARKLI bir dosyanın, daha önce sahte görülmüş bir
        # belgeyle AYNI banka+sıra numarasını taşıması durumunda BİLGİ olarak gösterilir.
        # AYNI İŞLEMİN farklı-sha kopyası kara-liste FP'si üretmesin: eşleşen sahte kayıt, bu dekontla
        # AYNI tutar+alıcı+işlem tarihini taşıyorsa (aynı belgenin tekrar taranması) göstergesi ÜRETİLMEZ.
        def _round2(a):
            try:
                return round(float(a), 2)
            except Exception:
                return None

        def _daykey(s):
            dt, _ = parse_content_datetime(s or "")
            return dt.date().isoformat() if dt else None

        _cur_amt = _round2(f.get("amount"))
        _cur_riban = re.sub(r"\s+", "", (f.get("receiver_iban") or "")).upper()
        _cur_rname = re.sub(r"\s+", " ", (f.get("receiver_name") or "").strip()).upper()
        _cur_day = _daykey(f.get("txn_date"))
        hit_seq = None
        if f["bank"] and f["seq_number"]:
            _rows = con.execute(
                "SELECT amount, receiver_iban, receiver_name, txn_date FROM analyses "
                "WHERE bank=? AND seq_number=? AND is_fake=1 AND sha256<>? LIMIT 20",
                (f["bank"], f["seq_number"], f["sha256"])).fetchall()
            for _pr in _rows:
                p_amt = _round2(_pr[0])
                p_riban = re.sub(r"\s+", "", (_pr[1] or "")).upper()
                # KİRLİ/EKSİK kayıtları YOK SAY: önceki sahte kaydın tutarı okunmamışsa güvenilir bir
                # 'farklı belge' kanıtı değildir → atla (FP önler).
                if p_amt is None:
                    continue
                # Farklılık: TUTAR ya da İŞLEM TARİHİ (GÜN) — sahtecilik sadece tarih değiştirilerek de yapılır.
                # Alıcı IBAN farkı ölçüt DIŞI (OCR varyansına en açık). Kayıtlar eksiksiz okumalardan olduğu için
                # tutar/tarih kararlı: numara aynı + (tutar farklı VEYA gün farklı) → FARKLI belge; ikisi de aynı → aynı dekont.
                p_day = _daykey(_pr[3])
                if (_cur_amt is not None and _cur_amt != p_amt) or (_cur_day and p_day and _cur_day != p_day):
                    hit_seq = _pr   # gerçekten FARKLI bir belge → kara-liste göstergesi
                    break
        print(f"[blocklist] bank={f['bank']!r} seq={f['seq_number']!r} sha={f['sha256'][:10]} "
              f"cross_doc_hit={bool(hit_seq)}", flush=True)
        if hit_seq:
            why = (f"aynı banka+sıra numarası ({f['seq_number']}) daha önce sahte işaretlenmiş FARKLI "
                   f"bir belgede görüldü")
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

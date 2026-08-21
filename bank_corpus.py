"""
BANKA-İÇİ DEKONT HAFIZASI (bank corpus) — her bankanın GÖRÜLMÜŞ dekontlarını banka bazlı saklar
ve yeni bir dekontu YALNIZ KENDİ bankasının geçmişiyle karşılaştırır (kullanıcı kuralı: bankalar
arası kıyas yapılmaz).

İki katman:
  1) SEED — bu projede incelenen GERÇEK dekontlardan çıkarılmış banka-içi normlar (kod içinde
     kalıcı; "yüklenen eski dekontları içeride sakla" isteğinin karşılığı).
  2) Canlı birikim — analiz edilen her dekont store'a (SQLite) eklenir; summary SEED + store'u
     birleştirir. store yoksa yalnız SEED kullanılır (kayıpsız çalışır).
"""
from __future__ import annotations

# --- SEED: incelenen gerçek dekontlar (banka → görülen kanal/etiket normları) ---
# Her kayıt: {rail, billing, amount, note}. Aynı bankadan birden çok örnek = o bankanın normu.
SEED = {
    "enpara": [
        # 13 gerçek Enpara dekontundan çıkarılan norm: interbank olanların TAMAMI EFT; aynı banka
        # kodu (00157→00157) HAVALE. Gerçek üretici iText/Ibtech; sahteler pdfium/browser (rerender).
        {"rail": "eft", "billing": "EFT TUTARI / EFT ÜCRETİ (0 TL)", "amount": 56900.0,
         "note": "Interbank Enpara → hep EFT ('(FAST)' teslim rayı). Üretici iText/Ibtech."},
        {"rail": "eft", "billing": "EFT TUTARI / EFT ÜCRETİ (0 TL)", "amount": 100000.0, "note": "Interbank → EFT."},
        {"rail": "eft", "billing": "EFT TUTARI / EFT ÜCRETİ (0 TL)", "amount": 3000.0, "note": "Interbank → EFT."},
        {"rail": "havale", "billing": "Enpara→Enpara (00157→00157)", "amount": 35000.0,
         "note": "Aynı banka kodu → HAVALE (banka-içi). Fiş No ilk 8 hane = işlem tarihi."},
    ],
    "akbank": [
        {"rail": "eft", "billing": "GECEFT KOMİSYON / GEC EFT BSMV", "amount": 75000.0, "note": "GEÇ EFT (kesin EFT)."},
        {"rail": "eft", "billing": "GECEFT KOMİSYON / GEC EFT BSMV", "amount": 35000.0, "note": "GEÇ EFT (kesin EFT)."},
        {"rail": "eft", "billing": "KOMİSYON / BSMV (başlık EFT)", "amount": 1008.37, "note": "Başlık-temelli EFT."},
        {"rail": "eft", "billing": "KOMİSYON / BSMV (başlık EFT)", "amount": 100000.0, "note": "Başlık-temelli EFT."},
    ],
    "isbank": [
        {"rail": "fast", "billing": "FAST Ücreti ve Vergi", "amount": 5000.0, "note": "Giden Fast İşlemi."},
    ],
    "garanti": [
        {"rail": "fast", "billing": "HESAPTAN FAST", "amount": 3400.0, "note": "FAST REF NO taşır."},
    ],
    "vakif": [
        {"rail": "fast", "billing": "FAST Giden Anlık Ödeme / MASRAF TUTARI (TL)", "amount": 40416.0, "note": "Masraf DAİMA TL'li."},
        {"rail": "havale", "billing": "Hesaptan Havale", "amount": 6000.0, "note": "Banka-içi havale (gönderici IBAN maskeli olabilir)."},
    ],
    "yapikredi": [
        {"rail": "fast", "billing": "GİDEN FAST TUTARI (DEKONT TİPİ EFT yazsa da)", "amount": 8600.0,
         "note": "DEKONT TİPİ:EFT yanıltıcı; GİDEN FAST TUTARI → FAST."},
    ],
    "deniz": [
        {"rail": "fast", "billing": "Giden FAST / Masraf 16,76 TL", "amount": 100000.0, "note": "Kolonsuz düzen."},
        {"rail": "fast", "billing": "Giden FAST / Masraf 16,76 TL", "amount": 20400.0, "note": "Kolonsuz düzen."},
        {"rail": "fast", "billing": "Giden FAST / Masraf 16,76 TL", "amount": 22500.0, "note": "Kolonsuz düzen."},
    ],
    "papara": [
        {"rail": "fast", "billing": "FAST Para Transferi / Ücretsiz", "amount": 3000.0, "note": "EPK; ücretsiz FAST."},
    ],
    "alternatif": [
        {"rail": "fast", "billing": "Giden FAST Ödemesi", "amount": 1500.0, "note": "FAST Sorgu Numarası taşır."},
    ],
    "kuveyt": [
        {"rail": "fast", "billing": "IBAN'a Para Transferi (FAST) / Senaryo DEKONT/EFT", "amount": 5000.0,
         "note": "Senaryo/Tip 'DEKONT/EFT' GENEL; gerçek kanal Açıklama'daki '(FAST)' → FAST. "
                 "Gönderen IBAN basılmaz (yalnız 'Gönderilen IBAN' = alıcı). ŞEBNEM AYLİN DUYAR → Mustafa YEŞİLMEN (Enpara)."},
    ],
}


def _store_rows(bank_key: str) -> list:
    """Canlı store'dan bu bankanın kayıtlarını çeker (varsa). store yoksa boş liste."""
    try:
        import store as _s
        return _s.bank_corpus_rows(bank_key) or []
    except Exception:
        return []


def summary(bank_key: str) -> dict:
    """Bu bankanın (SEED + canlı) görülen kanal dağılımı ve etiket normları."""
    if not bank_key:
        return {"count": 0, "rails": {}, "billing_seen": [], "kaynak": "yok"}
    seed = SEED.get(bank_key, [])
    live = _store_rows(bank_key)
    rows = list(seed) + list(live)
    rails = {}
    billing = []
    for r in rows:
        rl = r.get("rail")
        if rl:
            rails[rl] = rails.get(rl, 0) + 1
        b = r.get("billing")
        if b and b not in billing:
            billing.append(b)
    return {
        "count": len(rows),
        "seed_count": len(seed),
        "live_count": len(live),
        "rails": rails,
        "billing_seen": billing[:8],
        "kaynak": "banka-içi (SEED + canlı)" if rows else "bu bankadan henüz örnek yok",
    }


def compare_rail(bank_key: str, rail: str) -> dict:
    """Yeni dekontun kanalını, AYNI bankanın geçmişiyle karşılaştırır (bankalar arası DEĞİL)."""
    s = summary(bank_key)
    if not s["count"]:
        return {"durum": "kısmi", "sonuc": f"Bu bankadan ({bank_key}) daha önce örnek yok; kanal yalnız "
                                            f"dekontun kendi etiketinden belirlendi."}
    seen = s["rails"].get(rail, 0)
    total = s["count"]
    if seen:
        return {"durum": "yapıldı", "sonuc": f"Banka-içi karşılaştırma: bu bankadan görülen {total} dekontun "
                                             f"{seen}'inde de kanal '{rail.upper()}' idi → tutarlı."}
    others = ", ".join(f"{k.upper()}×{v}" for k, v in s["rails"].items())
    return {"durum": "kısmi", "sonuc": f"Banka-içi karşılaştırma: bu bankadan daha önce '{rail.upper()}' "
                                       f"görülmedi (görülen: {others}). Kanalı dekontun kendi etiketi belirledi."}

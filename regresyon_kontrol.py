#!/usr/bin/env python3
"""
REGRESYON BEKÇİSİ (CLI) — motorun kendi kendini denetlemesi.
Tek kaynak: app/engine/self_check.py (IMPROVEMENTS günlüğü + değişmez testleri).
Aynı testler canlıda /api/v1/self_check ucundan da çalışır.

Kullanım: python3 regresyon_kontrol.py
Çıkış kodu 0 = tüm iyileştirmeler korunuyor, !=0 = en az biri ezilmiş.
"""
import sys

try:
    from app.engine import self_check          # kaynak yerleşim
except Exception:
    import self_check                           # düz (flat) yerleşim


def main():
    r = self_check.run()
    print("REGRESYON BEKÇİSİ\n" + "=" * 60)
    for c in r["checks"]:
        mark = "✅ GEÇTİ " if c["ok"] else "❌ EZİLMİŞ"
        print(f"{mark} | #{c['id']} {c['name']}")
        if not c["ok"]:
            print(f"          ↳ {c['detail']}")
    print("=" * 60)
    if r["all_ok"]:
        print(f"SONUÇ: {r['passed']}/{r['total']} iyileştirme korunuyor. ✅")
        sys.exit(0)
    print(f"SONUÇ: {r['total'] - r['passed']}/{r['total']} iyileştirme EZİLMİŞ — acilen bak!")
    sys.exit(1)


if __name__ == "__main__":
    main()

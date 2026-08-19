"""
YZ Değerlendirici (AI Adjudicator) — kural motorunun ÜSTÜNE oturan akıl-yürütme + düzeltme katmanı.

MANTIK (kullanıcı isteği):
  1) ÖNCE deterministik kural motoru çalışır (alanlar + bulgular üretir).
  2) Bir TAHRİFAT/UYUŞMAZLIK bulgusu VAR ise, YA DA kritik alanlar BOŞ/ŞÜPHELİ ise, dekont bu
     katmana ESKALE edilir.
  3) Bu katman görüntüyü DOĞRUDAN inceler ve:
       - Bulgunun GERÇEK mi yoksa kural/okuma HATASI mı olduğunu değerlendirir (bulgu üzerinde çalışır).
       - Yanlış okunan alanı (ör. IBAN) NETLEŞTİRİP YENİDEN OKUR; BOŞ bırakılan alanları yeniden analiz
         eder (isim yanlış yerden alınmışsa düzeltir).
       - Uzman gibi gerekçeli bir HÜKÜM üretir (benim değerlendirdiğim gibi).
       - Sistemin o BANKA için kod/kural iyileştirmesi gerekiyorsa yapılandırılmış bir TEŞHİS notu üretir
         (geliştiriciye; otomatik kod değiştirmez — adli araçta bu riskli).

GÜVENLİK KORKULUKLARI:
  - Matematiksel KESİN kanıtları (aynı-banka çelişkisi, revizyon-tahrifatı, dijital-PDF'te IBAN mod-97,
    kimlik alan çelişkisi) EZMEZ; onların ÜSTÜNE akıl yürütür. (Fotoğrafta IBAN geçersizliği ise
    genelde OKUMA hatasıdır → yeniden okunur.)
  - Yalnız görüntüde AÇIKÇA okunabilen değeri düzeltir; emin değilse boş bırakır ve banka teyidi önerir.
  - Halüsinasyon yasağı: kanıtsız hüküm vermez. Çıktısı deterministik hükümden AYRI tutulur (denetlenebilir).

BAĞIMSIZLIK: Mevcut API cevap anahtarlarını/URL'lerini DEĞİŞTİRMEZ. Sonuç, rapora EK (additive) bir
alan olarak (`yapay_zeka_degerlendirmesi`) eklenir; yalnızca DEKONT_AI_ADJUDICATOR=1 ve ANTHROPIC_API_KEY
varken çalışır.

Yapılandırma:
  ANTHROPIC_API_KEY        : gerekli.
  DEKONT_AI_ADJUDICATOR    : '1' ise aktif (varsayılan '0' — kapalı, mevcut davranış korunur).
  DEKONT_ADJUDICATOR_MODEL : model adı (varsayılan DEKONT_VISION_MODEL ya da claude-3-5-sonnet-latest).
"""
from __future__ import annotations

import os
import io
import json
import base64
import urllib.request
import urllib.error

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-3-5-sonnet-latest"

# Deterministik olarak KESİN kabul edilen bulgu kodları — YZ bunları EZEMEZ (yalnız açıklar).
_HARD_PROOF_CODES = {
    "SAMEBANK_RAIL_CONTRADICTION", "ID_FIELD_MISMATCH", "REV_AMOUNT_CHANGED", "REV_CONTENT_CHANGED",
    "AMOUNT_MISMATCH", "SELF_TRANSFER", "SEQ_DB_DUPLICATE", "SEQ_CREATION_BACKDATE",
    "PDFIUM_PRODUCED", "BROWSER_RERENDER",
}
# Fotoğraf/OCR'da OKUMA hatası olabilen (YZ'nin yeniden-okuyup düzeltebileceği) bulgular.
_REREAD_CODES = {
    "IBAN_INVALID", "ISSUER_IBAN_MISMATCH", "RECEIVER_BANK_MISMATCH", "ID_CHECKSUM_INVALID",
    "LOW_IMAGE_QUALITY", "CONSISTENCY_FAIL", "VISION_TEXT_TAMPER", "NOT_A_RECEIPT",
}

# Yeniden okunması/doldurulması öncelikli kritik alanlar.
_CRITICAL_FIELDS = ["sender.name", "sender.iban", "receiver.name", "receiver.iban",
                    "amount.value", "transaction.date"]


def is_enabled() -> bool:
    if os.environ.get("DEKONT_AI_ADJUDICATOR", "0") != "1":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _get(d: dict, dotted: str):
    cur = d
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def should_adjudicate(findings: list, extraction: dict, input_kind: str = "pdf") -> tuple[bool, list]:
    """Bu dekont YZ değerlendiricisine gitmeli mi? (tetik, nedenler) döndürür.
    Tetikler: (a) high/critical bir bulgu var; (b) kritik alan(lar) boş; (c) yeniden-okunabilir bulgu."""
    # HIZ: yalnız GERÇEKTEN gerekince eskale et (temiz dekontta YZ'ye gidilmez → hızlı ve ucuz).
    reasons = []
    sev = {(f.get("code")): f.get("severity") for f in (findings or [])}
    if any(s in ("high", "critical") for s in sev.values()):
        reasons.append("Yüksek/kritik önem taşıyan bir bulgu var.")
    # Yalnız EN KRİTİK alanlar boşsa (alıcı adı/IBAN, tutar) — düşük öncelikli boşluklar tetiklemez.
    _core = [f for f in ("receiver.name", "receiver.iban", "amount.value")
             if not _get(extraction or {}, f)]
    if _core:
        reasons.append("Kritik alan boş: " + ", ".join(_core))
    return (bool(reasons), reasons)


def _img_b64(pil_img, max_dim: int = 1900):
    from PIL import Image
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        s = max_dim / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return base64.b64encode(buf.getvalue()).decode("ascii")


_SCHEMA_HINT = (
    '{\n'
    '  "verdict": "gerçek | şüpheli | sahte | belirsiz",\n'
    '  "confidence": 0-100,\n'
    '  "reasoning_tr": "uzman gerekçesi (kısa paragraf, kanıta dayalı)",\n'
    '  "corrected_fields": { "receiver.iban": "TR...", "sender.name": "...", ... },  // yalnız GÖRÜNTÜDE '
    'AÇIKÇA okuduğun ve kuralın YANLIŞ/BOŞ verdiği alanlar; emin değilsen KOYMA\n'
    '  "finding_reviews": [ {"code":"IBAN_INVALID","gercek_mi":false,"aciklama":"foto okuma hatası; doğru IBAN ..."} ],\n'
    '  "verify_suggestion": "banka teyidi için ne sorgulanmalı (sorgu/işlem no vb.)",\n'
    '  "improvement_notes": [ {"bank":"denizbank","field":"receiver.name","problem":"...",'
    '"suggestion":"...","label_hint":"Alıcı Adı Soyadı"} ]  // label_hint: bu alanın belgede YANINDA '
    'durduğu ETİKET METNİ (sistemin ÖĞRENİP sonraki dekontlarda otomatik uygulaması için)\n'
    '}'
)


def _build_prompt(extraction: dict, findings: list, bank_ctx: str, input_kind: str,
                  text_source: str) -> str:
    fields_json = json.dumps(extraction or {}, ensure_ascii=False, indent=1)[:3500]
    finds = [{"code": f.get("code"), "severity": f.get("severity"), "tr": (f.get("tr") or "")[:300]}
             for f in (findings or []) if f.get("severity") in ("high", "critical", "medium") or f.get("weight", 0) > 0]
    finds_json = json.dumps(finds, ensure_ascii=False)[:2500]
    kind_note = ("Bu bir FOTOĞRAF/GÖRÜNTÜdür — IBAN/kimlik gibi rakam-alanlarda kuralın 'geçersiz' demesi "
                 "genelde OKUMA hatasıdır; görüntüden NETLEŞTİRİP doğru değeri OKU."
                 if (input_kind == "image" or text_source in ("ocr", "vision"))
                 else "Bu DİJİTAL bir PDF'tir — metin güvenilirdir; IBAN mod-97/aynı-banka gibi kesin bulguları EZME.")
    return (
        "Sen bir BANKA DEKONTU ADLİ DEĞERLENDİRİCİSİsin. Önce deterministik kural motoru çalıştı ve "
        "aşağıdaki ALANLARI ve BULGULARI üretti. Şimdi GÖRSELİ (varsa) DOĞRUDAN incele ve şu işleri yap:\n"
        "1) Her BULGUYU değerlendir: gerçek bir tahrifat/uyuşmazlık mı, yoksa kuralın/OCR'ın HATASI mı? "
        "Gerekçelendir.\n"
        "2) Kuralın YANLIŞ okuduğu ya da BOŞ bıraktığı alanları GÖRÜNTÜDEN yeniden oku ve düzelt "
        "(özellikle IBAN'lar, isimler, tutar). İsim yanlış yerden alınmışsa doğru yerden al.\n"
        "3) Uzman gibi, KANITA DAYALI bir HÜKÜM ver (gerçek/şüpheli/sahte/belirsiz) + güven yüzdesi. "
        "reasoning_tr KISA ve SADE olsun: EN FAZLA 2 cümle, teknik jargon yok, net konuş.\n"
        "4) Bu bankanın çıkarımında SİSTEMİK bir sorun görürsen (kod/kural iyileştirmesi), "
        "improvement_notes'a banka+alan+sorun+öneri olarak yaz.\n\n"
        "KESİN KANITLARI EZME: aynı-banka çelişkisi, revizyon-tahrifatı, kimlik alan çelişkisi, "
        "dijital-PDF'te IBAN mod-97 hatası MATEMATİKSEL kesinliktir — bunları yalnız AÇIKLA, çürütme. "
        "Yalnız GÖRÜNTÜDE açıkça okunabilen değeri düzelt; emin değilsen alanı KOYMA ve banka teyidi öner. "
        "ASLA uydurma.\n\n"
        f"BELGE TÜRÜ NOTU: {kind_note}\n\n"
        f"--- BANKA BAĞLAMI ---\n{bank_ctx}\n\n"
        f"--- KURALIN ÇIKARDIĞI ALANLAR (JSON) ---\n{fields_json}\n\n"
        f"--- KURALIN BULGULARI (JSON) ---\n{finds_json}\n\n"
        "SADECE şu şemada GEÇERLİ JSON döndür (başka açıklama yazma):\n" + _SCHEMA_HINT
    )


def adjudicate(extraction: dict, findings: list, bank_key: str = "", pil_image=None,
               input_kind: str = "pdf", text_source: str = "digital", timeout: float = 60.0) -> dict | None:
    """YZ değerlendiricisini çalıştırır. Dönen dict rapora EK alan olarak konur; hata/kapalıysa None."""
    if not is_enabled():
        return None
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("DEKONT_ADJUDICATOR_MODEL") or os.environ.get("DEKONT_VISION_MODEL") or DEFAULT_MODEL
    try:
        import bank_knowledge as _bk
        bank_ctx = _bk.context_for(bank_key)
    except Exception:
        bank_ctx = ""
    prompt = _build_prompt(extraction, findings, bank_ctx, input_kind, text_source)

    content = []
    if pil_image is not None:
        try:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                         "data": _img_b64(pil_image)}})
        except Exception:
            pass
    content.append({"type": "text", "text": prompt})

    # HIZ: kısa/sade çıktı istendiği için 1024 yeterli (daha hızlı üretim, daha düşük gecikme).
    body = {"model": model, "max_tokens": 1024, "messages": [{"role": "user", "content": content}]}
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, method="POST")
    req.add_header("x-api-key", api_key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            _b = e.read().decode("utf-8")[:400]
        except Exception:
            _b = ""
        print(f"[ai_adjudicator] HTTP {e.code} model={model}: {_b}", flush=True)
        return None
    except Exception as e:
        print(f"[ai_adjudicator] error model={model}: {type(e).__name__}: {e}", flush=True)
        return None

    try:
        parts = payload.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except Exception:
        return None
    obj = _parse_json(text)
    if not obj:
        return None
    return _sanitize(obj)


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        o = json.loads(text[a:b + 1])
        return o if isinstance(o, dict) else None
    except Exception:
        return None


def _sanitize(obj: dict) -> dict:
    """Model çıktısını güvenli/temiz hâle getirir (tip ve alan kontrolü)."""
    out = {}
    v = str(obj.get("verdict", "belirsiz")).strip().lower()
    out["verdict"] = v if v in ("gerçek", "gercek", "şüpheli", "supheli", "sahte", "belirsiz") else "belirsiz"
    try:
        out["confidence"] = max(0, min(100, int(obj.get("confidence") or 0)))
    except Exception:
        out["confidence"] = 0
    out["reasoning_tr"] = str(obj.get("reasoning_tr") or "")[:2000]
    cf = obj.get("corrected_fields") or {}
    out["corrected_fields"] = {str(k): str(vv)[:120] for k, vv in cf.items()} if isinstance(cf, dict) else {}
    fr = obj.get("finding_reviews") or []
    out["finding_reviews"] = [
        {"code": str(x.get("code", "")), "gercek_mi": bool(x.get("gercek_mi", True)),
         "aciklama": str(x.get("aciklama", ""))[:500]}
        for x in fr if isinstance(x, dict)][:20]
    out["verify_suggestion"] = str(obj.get("verify_suggestion") or "")[:500]
    im = obj.get("improvement_notes") or []
    out["improvement_notes"] = [
        {"bank": str(x.get("bank", "")), "field": str(x.get("field", "")),
         "problem": str(x.get("problem", ""))[:300], "suggestion": str(x.get("suggestion", ""))[:300],
         "label_hint": str(x.get("label_hint", ""))[:60]}
        for x in im if isinstance(x, dict)][:20]
    return out


def learn_from(adjudication: dict, bank_key: str) -> list:
    """YZ'nin ürettiği improvement_notes'lardan MAKİNE-UYGULANABİLİR ipuçlarını (bank+field+label_hint)
    kalıcı store'a yazar → 'öğren' adımı. Döndürür: öğrenilen (field,label) listesi.
    Güvenli: yalnız hem alanı DÜZELTMİŞ (corrected_fields) hem label_hint vermiş notlar öğrenilir."""
    if not adjudication:
        return []
    learned = []
    try:
        import store as _store
    except Exception:
        return []
    corrected = set((adjudication.get("corrected_fields") or {}).keys())
    for note in (adjudication.get("improvement_notes") or []):
        field = (note.get("field") or "").strip()
        label = (note.get("label_hint") or "").strip()
        # Banka anahtarı OTORİTER olarak çağırandan (registry bank_key) gelir; YZ'nin serbest
        # metin 'bank' değeri yalnız yedektir (tutarsız olabilir).
        bank = (bank_key or note.get("bank") or "").strip().lower()
        # Yalnız YZ'nin GERÇEKTEN doğru okuyup düzelttiği alan için, ve bir etiket ipucu varsa öğren.
        if field and label and (field in corrected) and bank:
            try:
                if _store.record_field_hint(bank, field, label):
                    learned.append((field, label))
            except Exception:
                pass
    return learned


def apply_corrections(extraction: dict, adjudication: dict, hard_proof_codes=None) -> dict:
    """YZ'nin GÖRÜNTÜDEN düzelttiği alanları çıkarım dict'ine EK olarak işler. Deterministik KESİN
    bulguların dokunduğu alanlar korunur (YZ ezmesin). Yeni bir dict döndürür; orijinali bozmaz.
    Doğrulama: yalnız GEÇERLİ (ör. IBAN mod-97) düzeltmeler uygulanır."""
    import copy
    import banks as _b
    ex = copy.deepcopy(extraction or {})
    corr = (adjudication or {}).get("corrected_fields") or {}
    applied = {}
    for dotted, val in corr.items():
        if not val:
            continue
        # IBAN düzeltmesi yalnız mod-97 GEÇERLİYSE uygulanır (YZ yanlış IBAN uydurmasın)
        if dotted.endswith(".iban"):
            n = _b.normalize_iban(val)
            # YZ yanlış IBAN uydurmasın: yalnız mod-97 KESİN GEÇERLİ (True) düzeltme uygulanır.
            if _b.iban_valid(n) is not True:
                continue
            val = n
        parts = dotted.split(".")
        cur = ex
        ok = True
        for p in parts[:-1]:
            if not isinstance(cur.get(p), dict):
                ok = False
                break
            cur = cur[p]
        if ok and cur.get(parts[-1]) in (None, "", 0):
            cur[parts[-1]] = val
            applied[dotted] = val
        elif ok and cur.get(parts[-1]) != val:
            # dolu ama farklı: yalnız IBAN gibi doğrulanabilir alanlarda güncelle
            if dotted.endswith(".iban"):
                cur[parts[-1]] = val
                applied[dotted] = val
    ex["_ai_applied_corrections"] = applied
    return ex

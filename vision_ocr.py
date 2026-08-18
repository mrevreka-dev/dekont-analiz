"""
Görüntü-anlayan yapay zeka ile alan çıkarımı / Vision-LLM field extraction.

Tesseract OCR'ın bulanık/düşük çözünürlüklü telefon fotoğraflarında başarısız olduğu
durumlarda, bir görüntü-anlayan model (Anthropic Claude vision) fotoğrafı doğrudan
"okuyup" dekont alanlarını yapısal JSON olarak döndürür. Bu, insan gözüyle okunabilen
ama tesseract'ın çözemediği fotoğraflarda alıcı/gönderen/IBAN/tutar/tarih/referans
bilgilerini güvenilir biçimde çıkarır.

Yapılandırma (ortam değişkenleri / environment variables):
  ANTHROPIC_API_KEY     : gerekli — yoksa vision devre dışı, tesseract'a düşülür.
  DEKONT_VISION_MODEL   : model adı (varsayılan: claude-3-5-sonnet-latest).
  DEKONT_VISION_ENABLED : "0" ise tamamen kapatır (varsayılan açık, anahtar varsa).

Güvenlik: yalnızca kullanıcının yüklediği görsel modele gönderilir; başka veri paylaşılmaz.
Hiçbir yan etki (kayıt/gönderim) yoktur; yalnızca okuma amaçlıdır.
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

_PROMPT = (
    "Bu bir Türk bankası dekontu (havale/EFT/FAST makbuzu) fotoğrafı olabilir. Görseli dikkatle "
    "OKU ve SADECE aşağıdaki alanları içeren geçerli bir JSON döndür. Emin olamadığın alanı BOŞ "
    "bırak (uydurma). Tutarları ondalık nokta ile sayı olarak ver (5.000,00 -> 5000.00). IBAN'ı "
    "boşluksuz büyük harf yaz (TR + 24 hane). Metin Türkçe olabilir.\n\n"
    "Alanlar:\n"
    '{\n'
    '  "full_text": "",            // ÖNCE BUNU DOLDUR: dekonttaki TÜM yazının SADIK dökümü (aşağıya bak)\n'
    '  "is_receipt": true/false,   // görselde banka dekontu içeriği var mı\n'
    '  "bank": "",                 // dekontu düzenleyen/gönderen banka\n'
    '  "sender_name": "",          // gönderen ad soyad/unvan\n'
    '  "sender_iban": "",\n'
    '  "receiver_name": "",        // alıcı ad soyad/unvan\n'
    '  "receiver_iban": "",\n'
    '  "receiver_bank": "",        // alıcının bankası (varsa)\n'
    '  "amount": null,             // işlem tutarı (sayı)\n'
    '  "amount_currency": "",      // TRY/TL/USD/EUR\n'
    '  "fee": null,                // masraf/komisyon (sayı)\n'
    '  "total": null,              // toplam/hesaptan çekilen (sayı)\n'
    '  "date": "",                 // işlem tarihi ve saati (gg.aa.yyyy ss:dd:ss)\n'
    '  "ref_no": "",               // referans/sorgu no (FAST Sorgu No vb.)\n'
    '  "document_no": "",          // dekont/işlem no\n'
    '  "type": "", "channel": "", "description": "",\n'
    '  "tamper_suspected": false,   // görselde DİJİTAL OYNAMA/DÜZENLEME izi var mı\n'
    '  "tamper_fields": [],         // şüpheli alan adları (ör. ["receiver_name","amount"])\n'
    '  "tamper_confidence": 0,      // 0-100 arası güven\n'
    '  "tamper_reason": ""          // kısa gerekçe (Türkçe)\n'
    '}\n\n'
    "TAHRİFAT DEĞERLENDİRMESİ (çok önemli): Her alanın ÜZERİNDE dijital oynama olup "
    "olmadığını dikkatle incele. Bir metni ŞÜPHELİ işaretle eğer: yazı tipi/kalınlığı/"
    "boyutu çevresindeki metinden FARKLIYSA; karakterler ÜST ÜSTE BİNMİŞ, çakışmış ya da "
    "kırpılmışsa; harf aralıkları düzensizse; TABAN ÇİZGİSİ (baseline) satırdaki diğer "
    "metinden kaymışsa; keskinlik/bulanıklık, koyuluk/renk ya da sıkıştırma dokusu "
    "çevreden farklıysa; harflerin kenarında hâle/leke/artefakt varsa. ÖZELLİKLE alıcı adı, "
    "gönderici adı, TUTAR ve IBAN alanlarını incele. Şüphe varsa tamper_suspected=true yap, "
    "hangi alanların şüpheli olduğunu tamper_fields'a yaz ve güveni ver. Emin değilsen düşük "
    "güven ver ama yine de bildir.\n\n"
    "SADIK METİN DÖKÜMÜ (full_text) — ÇOK ÖNEMLİ: Dekonttaki GÖRÜNEN TÜM yazıyı, "
    "OLDUĞU GİBİ ve ETİKET:DEĞER yapısını KORUYARAK dök. Her 'Etiket : Değer' kendi satırında "
    "olsun. Belge İKİ SÜTUNLU/İKİ BLOKLU ise (ör. solda gönderen/hesap bilgisi, sağda alıcı; ya da "
    "'ALICI ÜNVANI' ile 'MÜŞTERİ ÜNVANI'/'GÖNDEREN' ayrı bloklarda) bunları BİRBİRİNE KARIŞTIRMA — "
    "her etiketi kendi değeriyle eşleştir. Alıcı ve gönderen etiketlerini (ALICI ÜNVANI, ALICI IBAN, "
    "MÜŞTERİ ÜNVANI, GÖNDEREN, GÖNDEREN IBAN, ALAN BANKA/KATILIMCI, EFT/FAST TUTARI, ÜCRET, SORGU NO, "
    "SIRA NO, FİŞ NO, IBAN, TCKN vb.) AYNEN yazıldıkları gibi koru. IBAN'ları tam (TR+24 hane) yaz. "
    "Bu döküm, banka-özel ayrıştırıcıya verilecek; bu yüzden etiketlerin doğruluğu KRİTİK.\n\n"
    "SADECE JSON döndür, başka açıklama yazma."
)


def is_configured() -> bool:
    if os.environ.get("DEKONT_VISION_ENABLED", "1") == "0":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _img_to_b64_jpeg(pil_img, max_dim: int = 1600) -> tuple[str, str]:
    """PIL görüntüyü JPEG base64'e çevirir (uzun kenar max_dim'e küçültülür)."""
    from PIL import Image
    img = pil_img.convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        s = max_dim / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"


def extract_from_image(pil_img, timeout: float = 45.0) -> dict | None:
    """
    Görselden dekont alanlarını vision modeli ile çıkarır.
    Başarılıysa dict, aksi halde None (yapılandırma yok / hata) döndürür.
    """
    if not is_configured():
        return None
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.environ.get("DEKONT_VISION_MODEL", DEFAULT_MODEL)
    try:
        b64, media = _img_to_b64_jpeg(pil_img)
    except Exception:
        return None

    # Bugünün tarihini prompt'a ekle: vision, bugüne/geçmişe ait GEÇERLİ işlem tarihlerini
    # ısrarla 'gelecek tarih' sanıp yanlış tahrifat şüphesi üretiyordu. Bu bağlam onu keser.
    import datetime as _dt
    try:
        _today = _dt.date.today().strftime("%d.%m.%Y")
    except Exception:
        _today = ""
    _date_note = (f"\n\nBUGÜNÜN TARİHİ: {_today}. Bu tarihe EŞİT ya da ÖNCESİNDEKİ işlem/belge "
                  "tarihleri TAMAMEN NORMALDİR — bunları ASLA 'gelecek tarih' diye tahrifat "
                  "işaretleme. Yalnızca bu tarihten KESİNLİKLE SONRAKİ tarihler geleceğe aittir.") \
        if _today else ""

    body = {
        "model": model,
        # full_text (tam metin dökümü) + yapısal alanlar + tahrifat analizi tek yanıtta döner;
        # 1024 yetersizdi (yoğun dekontta yanıt kesilip JSON bozuluyor, vision None düşüyordu).
        "max_tokens": 4096,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                {"type": "text", "text": _PROMPT + _date_note},
            ],
        }],
    }
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
            _body = e.read().decode("utf-8")[:500]
        except Exception:
            _body = ""
        print(f"[vision_ocr] HTTP {e.code} model={model}: {_body}", flush=True)
        return None
    except Exception as e:
        print(f"[vision_ocr] error model={model}: {type(e).__name__}: {e}", flush=True)
        return None

    # Yanıt metnini birleştir
    try:
        parts = payload.get("content", [])
        text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    except Exception:
        return None
    if not text:
        return None
    # JSON gövdesini ayıkla (model bazen ``` ile sarabilir)
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b <= a:
        return None
    try:
        obj = json.loads(text[a:b + 1])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None

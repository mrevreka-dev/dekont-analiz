"""
URL'den güvenli dosya indirme / Safe remote file fetch (for public S3 etc.).

API'ye dosya yüklemek yerine public bir URL (ör. S3) verildiğinde, dosyayı sunucu tarafında
indirip analiz hattına verir. Public bir servis olduğundan SSRF (Server-Side Request Forgery)
koruması uygulanır:
  - Yalnızca http/https,
  - Ana makine ÖZEL/iç ağ IP'sine çözümleniyorsa REDDEDİLİR (localhost, 10.x, 192.168.x,
    172.16-31.x, 169.254.x bulut-metadata dahil),
  - Yönlendirmeler (redirect) her adımda tekrar doğrulanır,
  - Boyut ve zaman aşımı sınırı.

Yapılandırma:
  DEKONT_URL_FETCH_ENABLED : "0" ise URL ile indirme kapalı (varsayılan açık).
  DEKONT_URL_ALLOW_HOSTS   : virgülle ayrılmış host son-ekleri; verilirse SADECE bunlara izin
                             verilir (ör. "amazonaws.com,mycdn.com").
"""
from __future__ import annotations

import os
import socket
import ipaddress
import urllib.request
import urllib.parse
import urllib.error


def enabled() -> bool:
    return os.environ.get("DEKONT_URL_FETCH_ENABLED", "1") != "0"


def _allow_hosts() -> list[str]:
    return [h.strip().lower() for h in os.environ.get("DEKONT_URL_ALLOW_HOSTS", "").split(",") if h.strip()]


def _is_public_ip(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved
                or a.is_multicast or a.is_unspecified)


def _check_public(url: str) -> None:
    """URL'yi doğrula: şema http/https, host public IP'ye çözümleniyor, (varsa) izinli host."""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("Yalnızca http/https adresleri desteklenir.")
    host = p.hostname
    if not host:
        raise ValueError("Geçersiz URL (ana makine yok).")
    allow = _allow_hosts()
    if allow and not any(host.lower() == h or host.lower().endswith("." + h) for h in allow):
        raise ValueError(f"Bu host'a izin verilmiyor: {host}")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"Ana makine çözümlenemedi: {host}")
    ips = {i[4][0] for i in infos}
    if not ips or not all(_is_public_ip(ip) for ip in ips):
        raise ValueError("Adres iç/özel ağa çözümleniyor — güvenlik nedeniyle engellendi (SSRF).")


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)      # her yönlendirmeyi yeniden doğrula
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, max_bytes: int, timeout: float = 25.0) -> tuple[bytes, str]:
    """URL'den dosyayı güvenli biçimde indirir. Döndürür: (bytes, dosya_adı).
    Hata durumunda ValueError fırlatır."""
    if not enabled():
        raise ValueError("URL ile indirme kapalı (DEKONT_URL_FETCH_ENABLED=0).")
    _check_public(url)
    opener = urllib.request.build_opener(_SafeRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "dekont-analiz/1.0"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            clen = resp.headers.get("Content-Length")
            if clen and clen.isdigit() and int(clen) > max_bytes:
                raise ValueError("Dosya çok büyük.")
            data = resp.read(max_bytes + 1)
    except urllib.error.HTTPError as e:
        raise ValueError(f"İndirme başarısız (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"İndirme başarısız: {getattr(e, 'reason', e)}")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"İndirme başarısız: {type(e).__name__}")
    if len(data) > max_bytes:
        raise ValueError("Dosya çok büyük.")
    if not data:
        raise ValueError("Boş dosya.")
    path = urllib.parse.urlparse(url).path
    name = os.path.basename(urllib.parse.unquote(path)) or "document"
    return data, name

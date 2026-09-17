from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


PRIVATE_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def validate_public_http_url(url: str) -> tuple[bool, str]:
    try:
        parts = urlsplit(url.strip())
    except Exception:
        return False, "URL не распознан."
    if parts.scheme not in {"http", "https"}:
        return False, "Разрешены только http/https URL."
    if not parts.hostname:
        return False, "URL должен содержать хост."
    host = parts.hostname.lower()
    if host in PRIVATE_HOSTS or host.endswith(".local"):
        return False, "Локальные и private URL заблокированы."
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False, "Private IP ranges заблокированы."
    except ValueError:
        try:
            for result in socket.getaddrinfo(host, None):
                ip = ipaddress.ip_address(result[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return False, "Хост резолвится в private IP range."
        except socket.gaierror:
            return True, ""
    return True, ""


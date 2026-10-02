"""TLS helpers for Windows machines with expired certificates in the system store.

Python (OpenSSL) loads every certificate from the Windows "ROOT" and "CA" stores,
including long-expired cross-signed ones, and can then fail a perfectly valid site with
"certificate has expired" while Windows' own tools (browsers, curl) accept it.

This module builds a verifying context from the same Windows stores but skips expired
certificates. Verification stays fully on. It is only used as a retry after an
expired-certificate failure, so normal connections are untouched.
"""
from __future__ import annotations

import ssl
import sys
import time

SERVER_AUTH_OID = "1.3.6.1.5.5.7.3.1"
X509_V_ERR_CERT_HAS_EXPIRED = 10

_cached: tuple | None = None


def is_expired_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", exc)
    return (isinstance(reason, ssl.SSLCertVerificationError)
            and getattr(reason, "verify_code", None) == X509_V_ERR_CERT_HAS_EXPIRED)


def _not_expired(der: bytes, now: float) -> bool:
    """True unless the certificate is provably expired."""
    try:
        probe = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        probe.load_verify_locations(cadata=der)
        info = probe.get_ca_certs()
        if not info:
            return True
        return ssl.cert_time_to_seconds(info[0]["notAfter"]) > now
    except (ssl.SSLError, ValueError, TypeError, KeyError):
        return True  # cannot tell: leave the decision to OpenSSL


def build_context(entries, now: float | None = None):
    """Build a verifying context from (der, encoding, trust) tuples, as returned by
    ssl.enum_certificates(), skipping expired certificates. None if nothing loaded."""
    now = time.time() if now is None else now
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)  # CERT_REQUIRED + hostname check
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    loaded = 0
    for der, encoding, trust in entries:
        if encoding != "x509_asn":
            continue
        if trust is not True and SERVER_AUTH_OID not in trust:
            continue
        if not _not_expired(der, now):
            continue
        try:
            ctx.load_verify_locations(cadata=der)
            loaded += 1
        except ssl.SSLError:
            continue
    return ctx if loaded else None


def windows_store_entries() -> list:
    out: list = []
    for store in ("ROOT", "CA"):
        try:
            out.extend(ssl.enum_certificates(store))  # Windows only
        except (OSError, AttributeError):
            pass
    return out


def fallback_context():
    """Context to retry with after an expired-certificate failure (Windows only)."""
    global _cached
    if sys.platform != "win32":
        return None
    if _cached is None:
        _cached = (build_context(windows_store_entries()),)
    return _cached[0]

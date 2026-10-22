"""Constants for the SSL Reverse Proxy integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "sslreverseproxy"
NAME: Final = "SSL Reverse Proxy"
MANUFACTURER: Final = "SSLReverseProxy"
VERSION: Final = "0.1.1"

# The /api/instance contract this client speaks; a higher one may break it.
API_VERSION: Final = 1

SCAN_INTERVAL: Final = timedelta(seconds=60)
CERTIFICATE_SCAN_INTERVAL: Final = timedelta(minutes=15)

DEFAULT_VERIFY_SSL: Final = True

# Permission names as /api/whoami reports them.
PERMISSION_SERVER_READ: Final = "ServerRead"
PERMISSION_CERT_READ: Final = "CertRead"
PERMISSION_RELOAD: Final = "ProxyReload"
PERMISSION_RENEW: Final = "CertRenew"
REQUIRED_PERMISSIONS: Final = frozenset({PERMISSION_SERVER_READ, PERMISSION_CERT_READ})

# A repair issue asks for a new key from this long before the key expires.
KEY_EXPIRY_WARNING: Final = timedelta(days=7)

SERVICE_RELOAD: Final = "reload"
SERVICE_RENEW_CERTIFICATE: Final = "renew_certificate"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_CERTIFICATE: Final = "certificate"

ISSUE_KEY_EXPIRING: Final = "api_key_expiring"

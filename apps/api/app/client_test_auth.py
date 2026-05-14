"""
Narrow client-testing authentication: map bearer access keys to tenant IDs.

When APALYM_CLIENT_TEST_KEYS is unset or empty, the API runs in open demo mode
(fixed demo tenant). When set, every /v1/* route except auth helpers requires
Authorization: Bearer <access_key>. Clients cannot choose an arbitrary tenant:
the tenant is always derived from the validated key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

_cache: dict[str, tuple[str, str]] | None = None


@dataclass(frozen=True)
class ClientTestPrincipal:
    tenant_id: str
    workspace_id: str
    display_label: str


def _strip_segments(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(";") if part.strip()]


def load_client_test_key_map() -> dict[str, tuple[str, str]]:
    """
    Parse APALYM_CLIENT_TEST_KEYS:
      key1:tenant-a:Acme Corp;key2:tenant-b:Contoso

    If only two segments are present (key:tenant), display label defaults to tenant_id.
    """
    global _cache
    if _cache is not None:
        return _cache

    raw = (os.getenv("APALYM_CLIENT_TEST_KEYS") or "").strip()
    if not raw:
        _cache = {}
        return _cache

    mapping: dict[str, tuple[str, str]] = {}
    for segment in _strip_segments(raw):
        parts = segment.split(":")
        if len(parts) < 2:
            continue
        access_key = parts[0].strip()
        tenant_id = parts[1].strip()
        label = parts[2].strip() if len(parts) > 2 else tenant_id
        if not access_key or not tenant_id:
            continue
        mapping[access_key] = (tenant_id, label or tenant_id)

    _cache = mapping
    return _cache


def client_test_auth_enabled() -> bool:
    return bool(load_client_test_key_map())


def extract_bearer_token(authorization_header: str | None) -> Optional[str]:
    if not authorization_header:
        return None
    parts = authorization_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def resolve_principal(access_key: str) -> Optional[ClientTestPrincipal]:
    row = load_client_test_key_map().get(access_key)
    if not row:
        return None
    tenant_id, label = row
    return ClientTestPrincipal(
        tenant_id=tenant_id,
        workspace_id=tenant_id,
        display_label=label,
    )


def invalidate_cache() -> None:
    global _cache
    _cache = None

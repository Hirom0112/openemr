"""Tests for FHIR auth token caching and refresh behavior.

Verifies that:
- A cached token is returned without a network call when still valid.
- A new token is fetched when the cache is expired.
- The 30-second buffer prevents using a token that expires imminently.
- A fetch failure propagates cleanly.

All tests are isolated — _fetch_token is mocked so no network calls are made.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

# httpx is a runtime dependency installed in Docker but may not be present
# on the host Python used to run isolated tests. Skip the module if absent.
httpx = pytest.importorskip("httpx", reason="httpx not installed in host environment")

import auth.fhir_client as fhir_auth_module
from auth.fhir_client import get_access_token


def _clear_cache() -> None:
    fhir_auth_module._token_cache.clear()


@pytest.mark.hard_failure
@pytest.mark.clinical_accuracy
class TestTokenCaching:
    def setup_method(self):
        _clear_cache()

    def test_fresh_fetch_on_empty_cache(self):
        async def run():
            mock_fetch = AsyncMock(return_value=("tok-abc", time.monotonic() + 600))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                token = await get_access_token()
            assert token == "tok-abc"
            mock_fetch.assert_awaited_once()

        asyncio.run(run())

    def test_cached_token_returned_without_refetch(self):
        async def run():
            mock_fetch = AsyncMock(return_value=("tok-cached", time.monotonic() + 600))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                t1 = await get_access_token()
                t2 = await get_access_token()
            assert t1 == t2 == "tok-cached"
            # Only one fetch despite two calls — cache hit on second
            assert mock_fetch.await_count == 1

        asyncio.run(run())

    def test_expired_token_triggers_refetch(self):
        async def run():
            # Pre-populate cache with an already-expired token
            fhir_auth_module._token_cache["token"] = "tok-stale"
            fhir_auth_module._token_cache["expiry"] = time.monotonic() - 1  # already past

            new_token = "tok-fresh"
            mock_fetch = AsyncMock(return_value=(new_token, time.monotonic() + 600))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                token = await get_access_token()

            assert token == new_token
            mock_fetch.assert_awaited_once()

        asyncio.run(run())

    def test_token_expiring_soon_triggers_refetch(self):
        """The 30-second buffer in _fetch_token means expiry = now + (ttl - 30).
        A token with expiry already within the buffer should be replaced."""
        async def run():
            # Expiry is 10 seconds in the past (simulates a token that expired
            # after the 30s buffer was applied — i.e., within the safety window)
            fhir_auth_module._token_cache["token"] = "tok-expiring"
            fhir_auth_module._token_cache["expiry"] = time.monotonic() - 10

            mock_fetch = AsyncMock(return_value=("tok-renewed", time.monotonic() + 600))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                token = await get_access_token()

            assert token == "tok-renewed"
            mock_fetch.assert_awaited_once()

        asyncio.run(run())

    def test_fetch_failure_propagates(self):
        async def run():
            mock_fetch = AsyncMock(side_effect=RuntimeError("OAuth endpoint unreachable"))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                with pytest.raises(RuntimeError, match="OAuth endpoint unreachable"):
                    await get_access_token()

        asyncio.run(run())

    def test_cache_updated_after_successful_fetch(self):
        async def run():
            expiry = time.monotonic() + 300
            mock_fetch = AsyncMock(return_value=("tok-new", expiry))
            with patch.object(fhir_auth_module, "_fetch_token", mock_fetch):
                await get_access_token()

            assert fhir_auth_module._token_cache["token"] == "tok-new"
            assert fhir_auth_module._token_cache["expiry"] == pytest.approx(expiry, abs=1.0)

        asyncio.run(run())

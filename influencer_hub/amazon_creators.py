"""Small asynchronous client for Amazon's official Creators API.

The API client is deliberately not on the dispatch hot path: deal posting must
continue when Amazon catalog metadata is unavailable or the account is not
currently eligible for API access. Credentials are read from the environment;
only the public credential ID is safe to keep in configuration.

Current endpoints use OAuth client credentials (scope ``creatorsapi::default``)
and ``POST /catalog/v1/{operation}`` with an ``x-marketplace`` header.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable

import aiohttp

from . import config


_TOKEN_ENDPOINTS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}


class AmazonCreatorsAPIError(RuntimeError):
    """Sanitized Creators API error (never includes configured credentials)."""


class AmazonCreatorsAPI:
    """OAuth-authenticated Amazon catalog client for the configured marketplace."""

    def __init__(
        self,
        credential_id: str | None = None,
        credential_secret: str | None = None,
        version: str | None = None,
        marketplace: str | None = None,
        associate_tag: str | None = None,
        endpoint: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.credential_id = (credential_id or config.AMAZON_CREATORS_API_CLIENT_ID).strip()
        self.credential_secret = (credential_secret or config.AMAZON_CREATORS_API_CLIENT_SECRET).strip()
        self.version = (version or config.AMAZON_CREATORS_API_VERSION).strip()
        self.marketplace = (marketplace or config.AMAZON_CREATORS_API_MARKETPLACE).strip()
        self.associate_tag = (associate_tag or config.AMAZON_ASSOCIATE_TAG).strip()
        self.endpoint = (endpoint or config.AMAZON_CREATORS_API_ENDPOINT).rstrip("/")
        self.timeout = float(timeout or config.AMAZON_CREATORS_API_TIMEOUT)
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _validate_credentials(self) -> str:
        if not self.credential_id:
            raise AmazonCreatorsAPIError("AMAZON_CREATORS_API_CLIENT_ID is not configured")
        if not self.credential_secret:
            raise AmazonCreatorsAPIError(
                "AMAZON_CREATORS_API_CLIENT_SECRET is not configured; set it in the VM's .env"
            )
        token_url = _TOKEN_ENDPOINTS.get(self.version)
        if not token_url:
            raise AmazonCreatorsAPIError(
                "Unsupported Amazon Creators API credential version; expected 3.1, 3.2, or 3.3"
            )
        if not self.marketplace.startswith("www.amazon."):
            raise AmazonCreatorsAPIError("AMAZON_CREATORS_API_MARKETPLACE must be a marketplace host")
        return token_url

    async def get_access_token(self, session: aiohttp.ClientSession | None = None) -> str:
        """Return a cached LWA token, refreshing it before expiry."""
        if self._access_token and time.monotonic() < self._expires_at - 60:
            return self._access_token

        async with self._token_lock:
            if self._access_token and time.monotonic() < self._expires_at - 60:
                return self._access_token
            token_url = self._validate_credentials()
            owns_session = session is None
            client = session or aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout)
            )
            try:
                async with client.post(
                    token_url,
                    json={
                        "grant_type": "client_credentials",
                        "client_id": self.credential_id,
                        "client_secret": self.credential_secret,
                        "scope": "creatorsapi::default",
                    },
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                ) as response:
                    if response.status != 200:
                        raise AmazonCreatorsAPIError(
                            f"Amazon OAuth token request failed with HTTP {response.status}"
                        )
                    body = await response.json(content_type=None)
            except AmazonCreatorsAPIError:
                raise
            except Exception as exc:
                raise AmazonCreatorsAPIError(
                    f"Amazon OAuth token request failed ({type(exc).__name__})"
                ) from None
            finally:
                if owns_session:
                    await client.close()

            token = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise AmazonCreatorsAPIError("Amazon OAuth response did not include an access token")
            try:
                lifetime = max(60, int(body.get("expires_in", 3600)))
            except (TypeError, ValueError):
                lifetime = 3600
            self._access_token = token
            self._expires_at = time.monotonic() + lifetime
            return token

    async def _request(
        self, operation: str, payload: dict[str, Any],
        session: aiohttp.ClientSession | None = None,
    ) -> dict[str, Any]:
        if operation not in {"getItems", "searchItems", "getVariations", "getBrowseNodes"}:
            raise ValueError("Unsupported Creators API operation")
        self._validate_credentials()
        owns_session = session is None
        client = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.timeout)
        )
        try:
            token = await self.get_access_token(client)
            request_payload = dict(payload)
            request_payload.setdefault("partnerTag", self.associate_tag)
            request_payload.setdefault("marketplace", self.marketplace)
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-marketplace": self.marketplace,
                "User-Agent": f"{config.AMAZON_CREATORS_API_APP_NAME}/1.0 (Python; Influencers-deals)",
            }
            async with client.post(
                f"{self.endpoint}/{operation}",
                json=request_payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as response:
                if response.status != 200:
                    raise AmazonCreatorsAPIError(
                        f"Amazon Creators API {operation} failed with HTTP {response.status}"
                    )
                body = await response.json(content_type=None)
                if not isinstance(body, dict):
                    raise AmazonCreatorsAPIError("Amazon Creators API returned a non-object response")
                return body
        except AmazonCreatorsAPIError:
            raise
        except Exception as exc:
            raise AmazonCreatorsAPIError(
                f"Amazon Creators API {operation} request failed ({type(exc).__name__})"
            ) from None
        finally:
            if owns_session:
                await client.close()

    async def get_items(
        self, item_ids: Iterable[str],
        resources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch catalog data for one or more ASINs (Amazon permits max 10/request)."""
        ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()]
        if not ids:
            return {"itemsResult": {"items": []}}
        if len(ids) > 10:
            raise ValueError("Amazon Creators API GetItems accepts at most 10 item IDs per request")
        payload: dict[str, Any] = {
            "itemIds": ids,
            "itemIdType": "ASIN",
            "resources": list(resources or ("itemInfo.title", "images.primary.small")),
        }
        return await self._request("getItems", payload)

    async def get_item(self, asin: str) -> dict[str, Any]:
        """Convenience wrapper to look up a single ASIN."""
        return await self.get_items([asin])

    async def search_items(
        self,
        keywords: str,
        search_index: str = "All",
        item_count: int = 10,
        resources: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Search the India catalog through the official Creators API."""
        query = keywords.strip()
        if not query:
            raise ValueError("keywords must not be empty")
        if not 1 <= int(item_count) <= 10:
            raise ValueError("item_count must be between 1 and 10")
        payload: dict[str, Any] = {
            "keywords": query,
            "searchIndex": search_index,
            "itemCount": int(item_count),
            "resources": list(resources or ("itemInfo.title", "images.primary.small")),
        }
        return await self._request("searchItems", payload)


# Friendly alias for callers that prefer a client-style name.
AmazonCreatorsClient = AmazonCreatorsAPI

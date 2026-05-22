from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from ..._credential import build_credential

ARM_BASE = "https://management.azure.com"
DEFAULT_API_VERSION = "2016-06-01"

JsonObject = dict[str, Any]


class ArmClient:
    def __init__(self) -> None:
        self._credential = build_credential()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token,
            "https://management.azure.com/.default",
        )
        return token.token

    async def get(
        self,
        path: str,
        *,
        api_version: str = DEFAULT_API_VERSION,
        params: JsonObject | None = None,
    ) -> JsonObject:
        session = await self._ensure_session()
        url = f"{ARM_BASE}{path}"
        query: JsonObject = {"api-version": api_version}
        if params:
            query.update(params)
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.get(url, headers=headers, params=query) as resp:
            resp.raise_for_status()
            return await _read_json_object(resp)

    async def post(
        self,
        path: str,
        body: JsonObject | None = None,
        *,
        api_version: str = DEFAULT_API_VERSION,
    ) -> JsonObject:
        session = await self._ensure_session()
        url = f"{ARM_BASE}{path}"
        query: JsonObject = {"api-version": api_version}
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.post(url, headers=headers, params=query, json=body) as resp:
            resp.raise_for_status()
            return await _read_json_object(resp)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._credential.close()


class DataPlaneClient:
    """HTTP client for connector data plane invocation (V2 / AI Gateway).

    Uses ``https://apihub.azure.com/.default`` token scope instead of
    the ARM management plane scope.
    """

    def __init__(self) -> None:
        self._credential = build_credential()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token,
            "https://apihub.azure.com/.default",
        )
        return token.token

    async def request(
        self,
        method: str,
        url: str,
        *,
        body: JsonObject | None = None,
        params: JsonObject | None = None,
    ) -> JsonObject:
        session = await self._ensure_session()
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.request(
            method,
            url,
            headers=headers,
            params=params,
            json=body,
        ) as resp:
            resp.raise_for_status()
            if resp.content_length == 0:
                return {}
            return await _read_json_object(resp)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._credential.close()


async def _read_json_object(response: aiohttp.ClientResponse) -> JsonObject:
    payload = await response.json()
    if isinstance(payload, dict):
        return payload
    return {}

import asyncio

import aiohttp
from azure.identity import DefaultAzureCredential

ARM_BASE = "https://management.azure.com"
DEFAULT_API_VERSION = "2016-06-01"


class ArmClient:
    def __init__(self):
        self._credential = DefaultAzureCredential()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token, "https://management.azure.com/.default"
        )
        return token.token

    async def get(self, path: str, *, api_version: str = DEFAULT_API_VERSION, params: dict | None = None) -> dict:
        session = await self._ensure_session()
        url = f"{ARM_BASE}{path}"
        query = {"api-version": api_version}
        if params:
            query.update(params)
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.get(url, headers=headers, params=query) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def post(self, path: str, body: dict | None = None, *, api_version: str = DEFAULT_API_VERSION) -> dict:
        session = await self._ensure_session()
        url = f"{ARM_BASE}{path}"
        query = {"api-version": api_version}
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.post(url, headers=headers, params=query, json=body) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._credential.close()


class DataPlaneClient:
    """HTTP client for connector data plane invocation (V2 / AI Gateway).

    Uses ``https://apihub.azure.com/.default`` token scope instead of
    the ARM management plane scope.
    """

    def __init__(self):
        self._credential = DefaultAzureCredential()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _get_token(self) -> str:
        token = await asyncio.to_thread(
            self._credential.get_token, "https://apihub.azure.com/.default"
        )
        return token.token

    async def request(
        self,
        method: str,
        url: str,
        *,
        body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        session = await self._ensure_session()
        headers = {"Authorization": f"Bearer {await self._get_token()}"}
        async with session.request(
            method, url, headers=headers, params=params, json=body
        ) as resp:
            resp.raise_for_status()
            if resp.content_length == 0:
                return {}
            return await resp.json()

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._credential.close()

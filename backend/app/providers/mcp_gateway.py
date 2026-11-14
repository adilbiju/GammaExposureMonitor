from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl

from app.settings import Settings


class FileTokenStorage:
    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    async def get_tokens(self) -> OAuthToken | None:
        value = self._read().get("tokens")
        return OAuthToken.model_validate(value) if value else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        payload = self._read()
        payload["tokens"] = tokens.model_dump(mode="json")
        self._write(payload)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        value = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        payload = self._read()
        payload["client_info"] = client_info.model_dump(mode="json")
        self._write(payload)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()


class McpGateway:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = FileTokenStorage(settings.token_store_path)
        self.authorization_url: str | None = None
        self.tools: list[str] = []
        self.tool_schemas: dict[str, dict[str, Any]] = {}
        self.connected = False
        self.last_error: str | None = None
        self._connect_task: asyncio.Task | None = None
        self._authorization_ready = asyncio.Event()
        self._callback_future: asyncio.Future[AuthorizationCodeResult] | None = None

    @property
    def connecting(self) -> bool:
        return self._connect_task is not None and not self._connect_task.done()

    async def _redirect_handler(self, authorization_url: str) -> None:
        self.authorization_url = authorization_url
        self._authorization_ready.set()

    async def _callback_handler(self) -> AuthorizationCodeResult:
        loop = asyncio.get_running_loop()
        self._callback_future = loop.create_future()
        return await self._callback_future

    def submit_callback(self, code: str, state: str, iss: str | None = None) -> None:
        if self._callback_future and not self._callback_future.done():
            self._callback_future.set_result(AuthorizationCodeResult(code=code, state=state, iss=iss))

    def _oauth_provider(self) -> OAuthClientProvider:
        return OAuthClientProvider(
            server_url=self.settings.mcp_server_url,
            client_metadata=OAuthClientMetadata(
                client_name="GEX Lab",
                redirect_uris=[AnyUrl(self.settings.mcp_redirect_uri)],
                scope=None,
            ),
            storage=self.storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
        )

    async def _with_client(self, operation):
        timeout = httpx2.Timeout(30, read=300)
        if self.settings.mcp_bearer_token:
            http_client = httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {self.settings.mcp_bearer_token}"},
                timeout=timeout,
            )
        else:
            http_client = httpx2.AsyncClient(auth=self._oauth_provider(), timeout=timeout)
        async with http_client:
            transport = streamable_http_client(self.settings.mcp_server_url, http_client=http_client)
            async with Client(transport) as client:
                return await operation(client)

    async def _discover(self) -> None:
        self.last_error = None
        try:
            result = await self._with_client(lambda client: client.list_tools())
            self.tools = [tool.name for tool in result.tools]
            self.tool_schemas = {
                tool.name: (getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {})
                for tool in result.tools
            }
            self.connected = True
            self.authorization_url = None
        except Exception as exc:
            self.connected = False
            self.last_error = str(exc)
            raise

    async def start_connection(self) -> str | None:
        if self.connecting:
            return self.authorization_url
        self._authorization_ready.clear()
        self.authorization_url = None
        self._connect_task = asyncio.create_task(self._discover())
        done, _ = await asyncio.wait(
            [self._connect_task, asyncio.create_task(self._authorization_ready.wait())],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=12,
        )
        for task in done:
            if task is self._connect_task and task.exception():
                raise task.exception()
        return self.authorization_url

    async def refresh_status(self) -> None:
        if self._connect_task and self._connect_task.done():
            try:
                self._connect_task.result()
            except Exception:
                pass

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        async def operation(client):
            return await client.call_tool(name, arguments)

        return await self._with_client(operation)

    def disconnect(self) -> None:
        self.storage.clear()
        self.connected = False
        self.tools = []
        self.tool_schemas = {}
        self.authorization_url = None
        self.last_error = None


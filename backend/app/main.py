from __future__ import annotations

from datetime import date

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.calculations.gex import calculate_gex
from app.models import ConnectionStatus, GexResponse
from app.providers.demo import DemoProvider
from app.providers.mcp_gateway import McpGateway
from app.providers.robinhood_mcp import RobinhoodMcpProvider
from app.settings import get_settings

settings = get_settings()
gateway = McpGateway(settings)
demo_provider = DemoProvider()
mcp_provider = RobinhoodMcpProvider(gateway)

app = FastAPI(title="GEX Lab API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": settings.data_mode}


@app.get("/api/status", response_model=ConnectionStatus)
async def connection_status() -> ConnectionStatus:
    await gateway.refresh_status()
    if settings.data_mode.lower() == "demo":
        return ConnectionStatus(
            mode="demo",
            connected=True,
            detail="Using deterministic demo data. Set DATA_MODE=mcp to connect Robinhood.",
        )
    return ConnectionStatus(
        mode="mcp",
        connected=gateway.connected,
        connecting=gateway.connecting,
        authorization_url=gateway.authorization_url,
        server_url=settings.mcp_server_url,
        tools=gateway.tools,
        detail=gateway.last_error,
    )


@app.post("/api/mcp/connect", response_model=ConnectionStatus)
async def connect_mcp() -> ConnectionStatus:
    if settings.data_mode.lower() != "mcp":
        raise HTTPException(status_code=409, detail="Set DATA_MODE=mcp in .env and restart the backend.")
    try:
        await gateway.start_connection()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"MCP connection failed: {exc}") from exc
    return await connection_status()


@app.get("/api/mcp/callback", response_class=HTMLResponse)
async def mcp_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    iss = request.query_params.get("iss")
    error = request.query_params.get("error")
    if error:
        return HTMLResponse(f"<h1>Robinhood authorization failed</h1><p>{error}</p>", status_code=400)
    if not code or not state:
        return HTMLResponse("<h1>Missing OAuth callback parameters</h1>", status_code=400)
    gateway.submit_callback(code, state, iss)
    return HTMLResponse(
        """
        <!doctype html><html><body style="font-family:system-ui;background:#09110f;color:#e7f0ea;padding:3rem">
        <h1>Robinhood connected</h1><p>You can close this tab and return to GEX Lab.</p>
        <script>setTimeout(() => window.close(), 1200)</script></body></html>
        """
    )


@app.post("/api/mcp/disconnect", response_model=ConnectionStatus)
async def disconnect_mcp() -> ConnectionStatus:
    gateway.disconnect()
    return await connection_status()


@app.get("/api/gex", response_model=GexResponse)
async def gex(
    symbol: str = Query(default="SPY", min_length=1, max_length=8, pattern=r"^[A-Za-z.]+$"),
    expirations: list[date] | None = Query(default=None),
) -> GexResponse:
    provider = demo_provider if settings.data_mode.lower() == "demo" else mcp_provider
    try:
        snapshot = await provider.snapshot(symbol.upper())
        return calculate_gex(snapshot, expirations, settings.risk_free_rate)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Market data request failed: {exc}") from exc


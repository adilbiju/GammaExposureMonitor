# Gamma Exposure(GEX) Monitor

A local options gamma-exposure dashboard built with FastAPI, React, Vite, and Plotly. It calculates dollar GEX per 1% underlying move from open interest, using positive call gamma and negative put gamma, to aid in options trading.

## Features

- One-symbol workflow optimized for SPY and QQQ
- Expiration filtering and manual/60-second refresh
- Net, call, and put GEX by strike
- Call wall, put wall, and largest absolute GEX levels
- Robinhood Trading MCP connection over OAuth


## Local setup

### Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
uvicorn app.main:app --app-dir backend --reload --port 8000
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev
```

Open `http://127.0.0.1:5173`. Vite proxies `/api` to FastAPI.

After the one-time installs above, you can start both services together using:

```bash
./run-local.sh
```

## Connect Robinhood MCP

1. Set `DATA_MODE=mcp` in `.env` and restart the backend.
2. Open the app and select **Connect Robinhood**.
3. Complete Robinhood's OAuth flow in the new browser tab.

OAuth tokens are stored locally at `.data/robinhood-oauth.json` by default.
You can point `MCP_SERVER_URL` at another compatible Streamable HTTP MCP server, to use it instead.

## GEX Calculation

For each contract:

```text
GEX = gamma × open interest × 100 × spot² × 0.01 × direction
direction = +1 for calls, -1 for puts
```

The gamma profile recomputes Black–Scholes gamma at each simulated spot using the contract's implied volatility and time to expiration. This is an estimate of dealer positioning, not a direct observation of dealer inventory.

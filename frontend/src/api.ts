import type { ConnectionStatus, GexResponse } from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(payload.detail || `Request failed (${response.status})`);
  }
  return response.json() as Promise<T>;
}

export function getStatus(): Promise<ConnectionStatus> {
  return request("/api/status");
}

export function connectMcp(): Promise<ConnectionStatus> {
  return request("/api/mcp/connect", { method: "POST" });
}

export function disconnectMcp(): Promise<ConnectionStatus> {
  return request("/api/mcp/disconnect", { method: "POST" });
}

export function getGex(symbol: string, expirations?: string[]): Promise<GexResponse> {
  const params = new URLSearchParams({ symbol });
  expirations?.forEach((expiration) => params.append("expirations", expiration));
  return request(`/api/gex?${params.toString()}`);
}


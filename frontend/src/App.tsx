import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { CircleAlert, Link2, LoaderCircle, RefreshCw, Search, Unplug } from "lucide-react";
import Plot from "react-plotly.js";
import { connectMcp, disconnectMcp, getGex, getStatus } from "./api";
import type { ConnectionStatus, ContractExposure, GexResponse } from "./types";

const CALL_COLOR = "#4f9fe8";
const PUT_COLOR = "#e1685c";
const SPOT_COLOR = "#42c6b7";
const GRID_COLOR = "#252b33";
const DISPLAY_MONEYNESS_RANGE = 0.10;

const plotConfig = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"] as never,
};

function compactMoney(value: number): string {
  const absolute = Math.abs(value);
  const sign = value < 0 ? "−" : value > 0 ? "+" : "";
  if (absolute >= 1e9) return `${sign}$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `${sign}$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `${sign}$${(absolute / 1e3).toFixed(1)}K`;
  return `${sign}$${absolute.toFixed(0)}`;
}

function price(value: number | null): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

function shortExpiry(value: string): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(
    new Date(`${value}T12:00:00`),
  );
}

function heatColor(value: number, maximum: number): string {
  if (!value || !maximum) return "rgba(255, 255, 255, 0.025)";
  const intensity = Math.min(1, Math.abs(value) / maximum);
  const alpha = 0.12 + intensity * 0.7;
  return value > 0 ? `rgba(31, 119, 170, ${alpha})` : `rgba(181, 68, 62, ${alpha})`;
}

function Stat({ label, value, subtext, tone }: { label: string; value: string; subtext?: string; tone?: "positive" | "negative" }) {
  return (
    <div className="stat-cell">
      <span className="label">{label}</span>
      <span className={`stat-value ${tone ?? ""}`}>{value}</span>
      {subtext && <span className="stat-subtext">{subtext}</span>}
    </div>
  );
}

export default function App() {
  const [symbolInput, setSymbolInput] = useState("SPY");
  const [symbol, setSymbol] = useState("SPY");
  const [data, setData] = useState<GexResponse | null>(null);
  const [status, setStatus] = useState<ConnectionStatus | null>(null);
  const [selectedExpirations, setSelectedExpirations] = useState<string[]>([]);
  const [chartMode, setChartMode] = useState<"strikes" | "profile" | "heatmap">("strikes");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [loading, setLoading] = useState(true);
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tableQuery, setTableQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState<"call" | "put">("call");
  const [chainExpiration, setChainExpiration] = useState("");

  const refreshStatus = useCallback(async () => {
    try {
      const next = await getStatus();
      setStatus(next);
      return next;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not reach the backend.");
      return null;
    }
  }, []);

  const loadData = useCallback(async (ticker = symbol, expirations = selectedExpirations, silent = false) => {
    if (!silent) setLoading(true);
    setError(null);
    try {
      const result = await getGex(ticker, expirations.length ? expirations : undefined);
      setData(result);
      if (!expirations.length) setSelectedExpirations(result.selected_expirations);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to load GEX data.");
    } finally {
      setLoading(false);
    }
  }, [selectedExpirations, symbol]);

  useEffect(() => {
    void refreshStatus().then((next) => {
      if (next?.connected) void loadData("SPY", [], false);
      else setLoading(false);
    });
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!autoRefresh || !status?.connected) return;
    const timer = window.setInterval(() => void loadData(symbol, selectedExpirations, true), 60_000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, loadData, selectedExpirations, status?.connected, symbol]);

  useEffect(() => {
    if (!status?.connecting) return;
    const timer = window.setInterval(async () => {
      const next = await refreshStatus();
      if (next?.connected) {
        window.clearInterval(timer);
        setConnecting(false);
        void loadData(symbol, [], false);
      }
    }, 1200);
    return () => window.clearInterval(timer);
  }, [loadData, refreshStatus, status?.connecting, symbol]);

  useEffect(() => {
    if (data?.selected_expirations.length && !data.selected_expirations.includes(chainExpiration)) {
      setChainExpiration(data.selected_expirations[0]);
    }
  }, [chainExpiration, data]);

  const submitSymbol = (event: FormEvent) => {
    event.preventDefault();
    const next = symbolInput.trim().toUpperCase();
    if (!next) return;
    setSymbol(next);
    setSelectedExpirations([]);
    setTableQuery("");
    setChainExpiration("");
    void loadData(next, [], false);
  };

  const chooseSymbol = (next: string) => {
    setSymbolInput(next);
    setSymbol(next);
    setSelectedExpirations([]);
    setTableQuery("");
    setChainExpiration("");
    void loadData(next, [], false);
  };

  const toggleExpiration = (expiration: string) => {
    const exists = selectedExpirations.includes(expiration);
    if (exists && selectedExpirations.length === 1) return;
    const next = exists ? selectedExpirations.filter((item) => item !== expiration) : [...selectedExpirations, expiration].sort();
    setSelectedExpirations(next);
    void loadData(symbol, next, false);
  };

  const handleConnect = async () => {
    setConnecting(true);
    setError(null);
    const popup = window.open("about:blank", "robinhood-oauth", "width=560,height=760");
    try {
      const next = await connectMcp();
      setStatus(next);
      if (next.authorization_url && popup) popup.location.href = next.authorization_url;
      else popup?.close();
      if (next.connected) {
        setConnecting(false);
        void loadData(symbol, [], false);
      }
    } catch (cause) {
      popup?.close();
      setConnecting(false);
      setError(cause instanceof Error ? cause.message : "Robinhood connection failed.");
    }
  };

  const handleDisconnect = async () => {
    const next = await disconnectMcp();
    setStatus(next);
    setData(null);
  };

  const activeChainExpiration = data?.selected_expirations.includes(chainExpiration)
    ? chainExpiration
    : data?.selected_expirations[0] ?? "";

  const displayedContracts = useMemo(() => {
    if (!data) return [];
    const minimumStrike = data.spot * (1 - DISPLAY_MONEYNESS_RANGE);
    const maximumStrike = data.spot * (1 + DISPLAY_MONEYNESS_RANGE);
    return data.contracts.filter(
      (contract) => contract.strike >= minimumStrike && contract.strike <= maximumStrike,
    );
  }, [data]);

  const displayedStrikeExposure = useMemo(() => {
    if (!data) return [];
    const minimumStrike = data.spot * (1 - DISPLAY_MONEYNESS_RANGE);
    const maximumStrike = data.spot * (1 + DISPLAY_MONEYNESS_RANGE);
    return data.strike_exposure.filter(
      (row) => row.strike >= minimumStrike && row.strike <= maximumStrike,
    );
  }, [data]);

  const visibleContracts = useMemo(() => {
    if (!data) return [];
    const query = tableQuery.trim().toLowerCase();
    return displayedContracts
      .filter((contract) => contract.option_type === typeFilter)
      .filter((contract) => contract.expiration === activeChainExpiration)
      .filter((contract) => !query || contract.expiration.includes(query) || contract.strike.toString().includes(query) || contract.option_type.includes(query))
      .slice(0, 240);
  }, [activeChainExpiration, data, displayedContracts, tableQuery, typeFilter]);

  const typeContractCount = useMemo(
    () => displayedContracts.filter((contract) => contract.option_type === typeFilter && contract.expiration === activeChainExpiration).length,
    [activeChainExpiration, displayedContracts, typeFilter],
  );

  const heatmap = useMemo(() => {
    if (!data) return { expirations: [] as string[], rows: [] as { strike: number; values: number[]; net: number; isSpot: boolean; isMaximum: boolean }[], maximum: 0 };

    const expirations = data.selected_expirations;
    const buckets = new Map<number, Map<string, number>>();
    for (const contract of data.contracts) {
      const strikeBucket = buckets.get(contract.strike) ?? new Map<string, number>();
      strikeBucket.set(contract.expiration, (strikeBucket.get(contract.expiration) ?? 0) + contract.gex);
      buckets.set(contract.strike, strikeBucket);
    }

    const strikes = [...buckets.keys()].sort((left, right) => left - right);
    if (!strikes.length) return { expirations, rows: [], maximum: 0 };
    const nearestIndex = strikes.reduce(
      (best, strike, index) => Math.abs(strike - data.spot) < Math.abs(strikes[best] - data.spot) ? index : best,
      0,
    );
    const rowCount = 15;
    const start = Math.max(0, Math.min(nearestIndex - Math.floor(rowCount / 2), strikes.length - rowCount));
    const visibleStrikes = strikes.slice(start, start + rowCount).reverse();
    const nearestStrike = strikes[nearestIndex];
    const rows = visibleStrikes.map((strike) => {
      const values = expirations.map((expiration) => buckets.get(strike)?.get(expiration) ?? 0);
      return { strike, values, net: values.reduce((sum, value) => sum + value, 0), isSpot: strike === nearestStrike, isMaximum: false };
    });
    const maximumRowIndex = rows.reduce(
      (largest, row, index) => Math.abs(row.net) > Math.abs(rows[largest].net) ? index : largest,
      0,
    );
    const markedRows = rows.map((row, index) => ({ ...row, isMaximum: index === maximumRowIndex }));
    const maximum = Math.max(0, ...markedRows.flatMap((row) => [...row.values, row.net]).map(Math.abs));
    return { expirations, rows: markedRows, maximum };
  }, [data]);

  const connected = Boolean(status?.connected);
  const positiveRegime = (data?.net_gex ?? 0) >= 0;

  return (
    <div className="terminal-shell">
      <header className="app-header">
        <div className="wordmark">GEX MONITOR</div>
        <div className="header-right">
          <span className={`feed-status ${connected ? "online" : ""}`}>{status?.mode === "demo" ? "DEMO FEED" : connected ? "ROBINHOOD MCP" : "FEED OFFLINE"}</span>
          {status?.mode === "mcp" && (connected ? (
            <button className="text-action" onClick={handleDisconnect}><Unplug size={13} /> Disconnect</button>
          ) : (
            <button className="primary-action" onClick={handleConnect} disabled={connecting}>
              {connecting ? <LoaderCircle className="spin" size={13} /> : <Link2 size={13} />}{connecting ? "Connecting" : "Connect feed"}
            </button>
          ))}
        </div>
      </header>

      {loading && data && (
        <div className="update-indicator" role="status" aria-live="polite" aria-label={`Updating ${symbol} data`}>
          <span>UPDATING {symbol}</span>
          <span className="update-dots" aria-hidden="true"><i /><i /><i /></span>
        </div>
      )}

      <main className="workspace">
        <section className="control-bar">
          <form className="symbol-control" onSubmit={submitSymbol}>
            <span className="control-label">Symbol</span>
            <div className="symbol-field">
              <input value={symbolInput} onChange={(event) => setSymbolInput(event.target.value.toUpperCase())} aria-label="Ticker symbol" maxLength={8} />
              <button type="submit">GO</button>
            </div>
          </form>

          <div className="quick-control">
            <span className="control-label">Quick</span>
            <div className="button-group">
              {["SPY", "QQQ", "IWM"].map((ticker) => <button key={ticker} className={symbol === ticker ? "active" : ""} onClick={() => chooseSymbol(ticker)}>{ticker}</button>)}
            </div>
          </div>

          <div className="expiry-control">
            <span className="control-label">Expirations</span>
            <div className="expiry-list">
              {data?.expirations.map((expiration) => (
                <label key={expiration} className={selectedExpirations.includes(expiration) ? "selected" : ""}>
                  <input type="checkbox" checked={selectedExpirations.includes(expiration)} onChange={() => toggleExpiration(expiration)} />
                  {shortExpiry(expiration)}
                </label>
              ))}
              {!data && <span className="placeholder">—</span>}
            </div>
          </div>

          <div className="refresh-control">
            <label className="auto-control"><input type="checkbox" checked={autoRefresh} onChange={(event) => setAutoRefresh(event.target.checked)} />Auto refresh <span>60s</span></label>
            <button className="refresh-button" onClick={() => void loadData(symbol, selectedExpirations, false)} disabled={!connected || loading} title="Refresh data">
              <RefreshCw className={loading ? "spin" : ""} size={14} /> Refresh
            </button>
          </div>
        </section>

        {error && <div className="error-row"><CircleAlert size={14} />{error}</div>}

        {!connected && !loading ? (
          <section className="offline-panel">
            <span className="label">MARKET DATA</span><h1>No data feed connected</h1><p>Connect the Robinhood MCP to load equity quotes and options-chain data.</p>
            <button className="primary-action" onClick={handleConnect} disabled={connecting}>{connecting ? <LoaderCircle className="spin" size={14} /> : <Link2 size={14} />}{connecting ? "Waiting for authorization" : "Connect Robinhood"}</button>
          </section>
        ) : (
          <>
            <section className="market-strip">
              <div className="instrument-cell">
                <div className="instrument-name"><strong>{data?.symbol ?? symbol}</strong><span>US EQUITY</span></div>
                <div className="instrument-price">{data ? price(data.spot) : "—"}</div>
                {data?.price_change_pct != null && <span className={data.price_change_pct >= 0 ? "change-up" : "change-down"}>{data.price_change_pct >= 0 ? "+" : ""}{data.price_change_pct.toFixed(2)}%</span>}
                <div className="feed-meta"><span>{data?.source ?? "Loading"}</span><span>{data ? new Date(data.as_of).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—"}</span></div>
              </div>
              <Stat label="NET GEX" value={data ? compactMoney(data.net_gex) : "—"} tone={positiveRegime ? "positive" : "negative"} subtext="per 1% move" />
              <Stat label="CALL GEX" value={data ? compactMoney(data.total_call_gex) : "—"} tone="positive" />
              <Stat label="PUT GEX" value={data ? compactMoney(data.total_put_gex) : "—"} tone="negative" />
              <Stat label="ZERO GAMMA" value={data ? price(data.zero_gamma) : "—"} subtext={data?.zero_gamma ? `${(((data.spot / data.zero_gamma) - 1) * 100).toFixed(2)}% distance` : "no crossing"} />
            </section>

            <section className="analysis-grid">
              <article className="data-panel chart-panel">
                <header className="panel-header">
                  <div><h2>{chartMode === "strikes" ? "Gamma exposure by strike" : chartMode === "profile" ? "Gamma exposure profile" : "Strike × expiry heatmap"}</h2></div>
                  <div className="view-tabs">
                    <button className={chartMode === "strikes" ? "active" : ""} onClick={() => setChartMode("strikes")}>STRIKES</button>
                    <button className={chartMode === "profile" ? "active" : ""} onClick={() => setChartMode("profile")}>SPOT PROFILE</button>
                    <button className={chartMode === "heatmap" ? "active" : ""} onClick={() => setChartMode("heatmap")}>HEATMAP</button>
                  </div>
                </header>
                <div className={`chart-area ${chartMode === "heatmap" ? "heatmap-mode" : ""}`}>
                  {data && chartMode === "strikes" && (
                    <Plot
                      data={[
                        { type: "bar", name: "Call", x: displayedStrikeExposure.map((row) => row.strike), y: displayedStrikeExposure.map((row) => row.call_gex), marker: { color: CALL_COLOR }, hovertemplate: "$%{x:.2f}<br>Call %{y:$.3s}<extra></extra>" },
                        { type: "bar", name: "Put", x: displayedStrikeExposure.map((row) => row.strike), y: displayedStrikeExposure.map((row) => row.put_gex), marker: { color: PUT_COLOR }, hovertemplate: "$%{x:.2f}<br>Put %{y:$.3s}<extra></extra>" },
                      ]}
                      layout={{ autosize: true, barmode: "relative", paper_bgcolor: "transparent", plot_bgcolor: "transparent", margin: { l: 64, r: 20, t: 34, b: 48 }, font: { color: "#87909a", family: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif", size: 10 }, xaxis: { title: { text: "STRIKE", font: { size: 9 } }, gridcolor: GRID_COLOR, zeroline: false, tickprefix: "$" }, yaxis: { title: { text: "GEX / 1%", font: { size: 9 } }, gridcolor: GRID_COLOR, zerolinecolor: "#59616c", tickformat: "$.2s" }, legend: { orientation: "h", x: 0, y: 1.08, font: { size: 10 } }, bargap: 0.18, shapes: [{ type: "line", x0: data.spot, x1: data.spot, y0: 0, y1: 1, yref: "paper", line: { color: SPOT_COLOR, width: 1, dash: "dot" } }] }}
                      config={plotConfig} useResizeHandler style={{ width: "100%", height: "100%" }}
                    />
                  )}
                  {data && chartMode === "profile" && (
                    <Plot
                      data={[{ type: "scatter", mode: "lines", name: "Net GEX", x: data.gamma_profile.map((point) => point.spot), y: data.gamma_profile.map((point) => point.net_gex), line: { color: CALL_COLOR, width: 2 }, fill: "tozeroy", fillcolor: "rgba(79, 159, 232, 0.08)", hovertemplate: "$%{x:.2f}<br>%{y:$.3s}<extra></extra>" }]}
                      layout={{ autosize: true, paper_bgcolor: "transparent", plot_bgcolor: "transparent", margin: { l: 64, r: 20, t: 34, b: 48 }, font: { color: "#87909a", family: "-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, Helvetica, Arial, sans-serif", size: 10 }, xaxis: { title: { text: "SIMULATED SPOT", font: { size: 9 } }, gridcolor: GRID_COLOR, zeroline: false, tickprefix: "$" }, yaxis: { title: { text: "NET GEX", font: { size: 9 } }, gridcolor: GRID_COLOR, zerolinecolor: "#59616c", tickformat: "$.2s" }, showlegend: false, shapes: [{ type: "line", x0: data.spot, x1: data.spot, y0: 0, y1: 1, yref: "paper", line: { color: SPOT_COLOR, width: 1, dash: "dot" } }, ...(data.zero_gamma ? [{ type: "line" as const, x0: data.zero_gamma, x1: data.zero_gamma, y0: 0, y1: 1, yref: "paper", line: { color: PUT_COLOR, width: 1, dash: "dash" } }] : [])] }}
                      config={plotConfig} useResizeHandler style={{ width: "100%", height: "100%" }}
                    />
                  )}
                  {data && chartMode === "heatmap" && (
                    <div className="heatmap-shell">
                      <div className="heatmap-key">
                        <span>NET GEX · $ / 1% MOVE</span>
                        <div><i className="negative" /> NEGATIVE <i className="neutral" /> LOW <i className="positive" /> POSITIVE <i className="maximum" /> MAX |NET|</div>
                      </div>
                      <div
                        className="heatmap-grid heatmap-heading"
                        style={{ gridTemplateColumns: `94px repeat(${heatmap.expirations.length}, minmax(74px, 1fr)) 104px` }}
                      >
                        <span>STRIKE</span>
                        {heatmap.expirations.map((expiration) => <span key={expiration}>{shortExpiry(expiration)}</span>)}
                        <span>NET</span>
                      </div>
                      <div className="heatmap-rows">
                        {heatmap.rows.map((row) => (
                          <div
                            key={row.strike}
                            className={`heatmap-grid heatmap-row ${row.isSpot ? "spot-row" : ""} ${row.isMaximum ? "maximum-row" : ""}`}
                            style={{ gridTemplateColumns: `94px repeat(${heatmap.expirations.length}, minmax(74px, 1fr)) 104px` }}
                          >
                            <div className="heatmap-strike">
                              <b>{row.strike.toFixed(2)}</b>
                              <span className="heatmap-row-labels">
                                {row.isSpot && <small>SPOT {data.spot.toFixed(2)}</small>}
                                {row.isMaximum && <small className="maximum-label">MAX |NET|</small>}
                              </span>
                            </div>
                            {row.values.map((value, index) => (
                              <div
                                key={`${row.strike}-${heatmap.expirations[index]}`}
                                className="heatmap-cell"
                                style={{ background: heatColor(value, heatmap.maximum) }}
                                title={`${shortExpiry(heatmap.expirations[index])} · $${row.strike.toFixed(2)} · ${compactMoney(value)}`}
                              >
                                {compactMoney(value)}
                              </div>
                            ))}
                            <div
                              className={`heatmap-cell heatmap-net ${row.isMaximum ? "maximum-net" : ""}`}
                              style={{ background: row.isMaximum ? undefined : heatColor(row.net, heatmap.maximum) }}
                              title={`Net at $${row.strike.toFixed(2)} · ${compactMoney(row.net)}${row.isMaximum ? " · Largest absolute net GEX shown" : ""}`}
                            >
                              {compactMoney(row.net)}
                            </div>
                          </div>
                        ))}
                      </div>
                      <div className="heatmap-foot"><span>HIGHER COLOR INTENSITY = LARGER ABSOLUTE EXPOSURE</span><span>{heatmap.rows.length} STRIKES AROUND SPOT</span></div>
                    </div>
                  )}
                  {!data && <div className="loading-state">{loading ? "LOADING EXPOSURE DATA…" : "NO DATA"}</div>}
                </div>
              </article>

              <aside className="right-rail">
                <section className="data-panel levels-panel">
                  <header className="panel-header"><div><h2>Reference levels</h2></div></header>
                  <div className="level-table">
                    <div><span>Call wall</span><b className="call">{price(data?.call_wall ?? null)}</b></div>
                    <div><span>Put wall</span><b className="put">{price(data?.put_wall ?? null)}</b></div>
                    <div><span>Largest absolute GEX</span><b>{price(data?.largest_gex_strike ?? null)}</b></div>
                    <div><span>Gamma flip</span><b>{price(data?.zero_gamma ?? null)}</b></div>
                    <div><span>Spot</span><b className="spot">{price(data?.spot ?? null)}</b></div>
                  </div>
                </section>
                <section className="data-panel regime-panel">
                  <header className="panel-header"><div><h2>Dealer regime</h2></div></header>
                  <div className="regime-body">
                    <span className={`regime-status ${positiveRegime ? "positive" : "negative"}`}><i />{positiveRegime ? "POSITIVE GAMMA" : "NEGATIVE GAMMA"}</span>
                    <p>{positiveRegime ? "Hedging flows may suppress realized volatility and pull price toward high open-interest strikes." : "Hedging flows may reinforce directional moves and increase realized volatility."}</p>
                  </div>
                </section>
              </aside>
            </section>

            <section className="data-panel chain-panel">
              <header className="panel-header chain-header">
                <div><h2>Options chain</h2><span className="record-count">{typeContractCount} {typeFilter === "call" ? "CALLS" : "PUTS"}</span></div>
                <div className="chain-tools">
                  <div className="view-tabs compact">{(["call", "put"] as const).map((item) => <button key={item} className={typeFilter === item ? "active" : ""} onClick={() => setTypeFilter(item)}>{item === "call" ? "CALLS" : "PUTS"}</button>)}</div>
                  <select className="chain-expiry-select" aria-label="Chain expiration filter" value={activeChainExpiration} onChange={(event) => setChainExpiration(event.target.value)}>
                    {data?.selected_expirations.map((expiration) => <option key={expiration} value={expiration}>{shortExpiry(expiration).toUpperCase()}</option>)}
                  </select>
                  <label className="filter-field"><Search size={12} /><input value={tableQuery} onChange={(event) => setTableQuery(event.target.value)} placeholder="FILTER" /></label>
                </div>
              </header>
              <div className="table-scroll">
                <table>
                  <thead><tr><th>Expiry</th><th>Type</th><th>Strike</th><th>Bid</th><th>Ask</th><th>IV</th><th>Gamma</th><th>Open interest</th><th>Volume</th><th>GEX / 1%</th></tr></thead>
                  <tbody>{visibleContracts.map((contract: ContractExposure) => (
                    <tr key={contract.id}>
                      <td>{shortExpiry(contract.expiration)}</td><td><span className={`type-marker ${contract.option_type}`}>{contract.option_type === "call" ? "C" : "P"}</span></td><td className="strong">${contract.strike.toFixed(2)}</td><td>${contract.bid.toFixed(2)}</td><td>${contract.ask.toFixed(2)}</td><td>{(contract.implied_volatility * 100).toFixed(1)}%</td><td>{contract.gamma.toFixed(5)}</td><td>{contract.open_interest.toLocaleString()}</td><td>{contract.volume.toLocaleString()}</td><td className={contract.gex >= 0 ? "number-positive" : "number-negative"}>{compactMoney(contract.gex)}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
              <footer className="panel-footer"><span>SHOWING {visibleContracts.length} / {typeContractCount} {typeFilter === "call" ? "CALLS" : "PUTS"}</span><span>{data?.calculation_note}</span></footer>
            </section>
          </>
        )}
      </main>
      <footer className="app-footer"><span>GEX MONITOR / LOCAL SESSION</span><span>OPEN-INTEREST MODEL · CALLS + / PUTS −</span></footer>
    </div>
  );
}

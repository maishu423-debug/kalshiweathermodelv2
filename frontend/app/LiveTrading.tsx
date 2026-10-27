"use client";

import { useCallback, useEffect, useState } from "react";


// ── Types ────────────────────────────────────────────────────────────────────

type LiveOrder = {
  id: number;
  created_at: string;
  slot: string;
  market_ticker: string;
  market_label: string;
  side: "yes" | "no";
  limit_price_cents: number;
  contracts_requested: number;
  fill_count: number;
  avg_price_cents: number | null;
  fee_cents: number | null;
  status: "pending" | "filled" | "partial" | "unfilled" | "error" | "unknown" | "mismatch";
  message: string;
  settled: boolean;
  payout_cents: number | null;
  realized_pl_cents: number | null;
};

type LiveState = {
  status: {
    enabled: boolean;
    credentials_configured: boolean;
    environment: "production" | "demo";
    per_trade_contracts: number;
    daily_max_contracts: number;
    contracts_used_today: number;
    contracts_remaining_today: number;
  };
  summary: {
    orders: number;
    contracts_filled: number;
    realized_pl_cents: number;
    wins: number;
    losses: number;
  };
  orders: LiveOrder[];
};


// ── Formatters ───────────────────────────────────────────────────────────────

const ET = "America/New_York";

function usd(cents: number) {
  return (cents / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function signedUsd(cents: number) {
  if (Math.round(cents) === 0) return usd(0);
  return `${cents > 0 ? "+" : "−"}${usd(Math.abs(cents))}`;
}

function etTime(iso: string) {
  return new Date(iso).toLocaleString("en-US", {
    timeZone: ET,
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

const STATUS_TEXT: Record<LiveOrder["status"], string> = {
  pending: "Sending…",
  filled: "Filled",
  partial: "Partly filled",
  unfilled: "Not filled",
  error: "Rejected",
  unknown: "Unknown — check Kalshi",
  mismatch: "WRONG DIRECTION",
};


// ── Component ────────────────────────────────────────────────────────────────

export default function LiveTrading() {
  const [state, setState] = useState<LiveState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/kalshi/autotrader/live", { cache: "no-store" });
      if (!res.ok) throw new Error(`Live trading API ${res.status}`);
      setState((await res.json()) as LiveState);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Live trading unavailable.");
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 5_000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function setLive(enabled: boolean) {
    setBusy(true);
    try {
      const res = await fetch("/api/kalshi/autotrader/live-toggle", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
      setState(data as LiveState);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed.");
    } finally {
      setBusy(false);
    }
  }

  function toggle() {
    if (!state) return;
    if (state.status.enabled) {
      setLive(false);
      return;
    }
    const s = state.status;
    const where = s.environment === "demo" ? "your Kalshi DEMO account (fake money)" : "your REAL Kalshi account (real money)";
    if (
      window.confirm(
        `Turn ON live trading on ${where}?\n\n` +
        `• One trade per cycle, ${s.per_trade_contracts} contracts each\n` +
        `• Never more than ${s.daily_max_contracts} contracts per day\n` +
        `• Orders are placed automatically during the trading window\n\n` +
        `You can switch it off here at any time.`
      )
    ) {
      setLive(true);
    }
  }

  if (!state) {
    return (
      <div className="pt-layout">
        <section className="algo-panel pt-wrap">
          <h3 className="algo-section-title">Live Trading</h3>
          <p className={error ? "algo-error" : "algo-hint"}>{error ?? "Loading live trading…"}</p>
        </section>
      </div>
    );
  }

  const { status, summary, orders } = state;
  const usedPct = Math.min(100, (status.contracts_used_today / status.daily_max_contracts) * 100);
  const mismatch = orders.find((o) => o.status === "mismatch");

  return (
    <div className="pt-layout">
      <section className={`algo-panel pt-wrap lt-panel ${status.enabled ? "lt-panel--on" : ""}`}>
        <div className="pt-head">
          <h3 className="algo-section-title">
            Live Trading
            <span className="algo-hint-inline">
              {" "}— {status.per_trade_contracts} contracts per trade · max {status.daily_max_contracts} per day · one trade per cycle
            </span>
          </h3>
          <div className="pt-controls">
            <span className={`lt-env lt-env--${status.environment}`}>
              {status.environment === "demo" ? "DEMO (fake money)" : "REAL MONEY"}
            </span>
            <span className={`pt-status ${status.enabled ? "lt-status--on" : "pt-status--paused"}`}>
              {status.enabled ? "Live trading ON" : "Live trading OFF"}
            </span>
            <button
              className={`algo-btn ${status.enabled ? "pt-btn-stop" : "algo-btn--trade"}`}
              disabled={busy || (!status.enabled && !status.credentials_configured)}
              onClick={toggle}
            >
              {status.enabled ? "Turn OFF" : "Turn ON"}
            </button>
          </div>
        </div>

        {error && <p className="algo-error">{error}</p>}
        {!status.credentials_configured && (
          <p className="algo-error">
            No Kalshi API key on the server. Set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PEM (read/write key) on the backend to enable live trading.
          </p>
        )}
        {mismatch && (
          <p className="algo-error">
            ⚠ An order moved your Kalshi position the wrong way, so live trading was switched off ({mismatch.market_label || mismatch.market_ticker}, {etTime(mismatch.created_at)} ET). Check your Kalshi account before turning it back on.
          </p>
        )}

        <div className="lt-usage">
          <div className="lt-usage-text">
            <strong>{status.contracts_used_today}</strong> / {status.daily_max_contracts} contracts used today
            <span className="algo-hint-inline"> · {status.contracts_remaining_today} left</span>
          </div>
          <div className="lt-bar"><span style={{ width: `${usedPct}%` }} /></div>
        </div>

        <div className="pt-hero">
          <div className="pt-hero-tile">
            <span className="pt-card-label">Live orders</span>
            <strong>{summary.orders}</strong>
            <span className="pt-card-sub">{summary.contracts_filled} contracts filled in total</span>
          </div>
          <div className="pt-hero-tile">
            <span className="pt-card-label">Settled P/L (estimate)</span>
            <strong className={summary.realized_pl_cents > 0 ? "pt-pos" : summary.realized_pl_cents < 0 ? "pt-neg" : ""}>
              {signedUsd(summary.realized_pl_cents)}
            </strong>
            <span className="pt-card-sub">{summary.wins} won · {summary.losses} lost · verify in Kalshi</span>
          </div>
        </div>

        <h4 className="algo-section-title lt-sub">Live orders</h4>
        {orders.length === 0 ? (
          <p className="algo-hint">No live orders yet. They appear here as soon as the bot places one.</p>
        ) : (
          <div className="pt-scroll">
            <table className="ev-table">
              <thead>
                <tr>
                  <th>Time (ET)</th>
                  <th>Bucket</th>
                  <th>Side</th>
                  <th>Limit</th>
                  <th>Filled</th>
                  <th>Avg price</th>
                  <th>Fee</th>
                  <th>Status</th>
                  <th>Result</th>
                </tr>
              </thead>
              <tbody>
                {orders.map((o) => (
                  <tr key={o.id} title={o.message || undefined}>
                    <td>{etTime(o.created_at)}</td>
                    <td>{o.market_label || o.market_ticker}</td>
                    <td><span className={`ev-badge ${o.side}`}>{o.side.toUpperCase()}</span></td>
                    <td>{o.limit_price_cents}¢</td>
                    <td>{o.fill_count} / {o.contracts_requested}</td>
                    <td>{o.avg_price_cents != null ? `${o.avg_price_cents.toFixed(1)}¢` : "—"}</td>
                    <td>{o.fee_cents != null ? usd(o.fee_cents) : "—"}</td>
                    <td className={o.status === "mismatch" || o.status === "unknown" || o.status === "error" ? "pt-neg" : ""}>
                      {STATUS_TEXT[o.status]}
                    </td>
                    <td className={o.realized_pl_cents != null ? (o.realized_pl_cents >= 0 ? "pt-pos" : "pt-neg") : ""}>
                      {o.settled && o.realized_pl_cents != null ? signedUsd(o.realized_pl_cents) : o.fill_count > 0 ? "open" : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}

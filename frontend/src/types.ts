export type OptionType = "call" | "put";

export interface StrikeExposure {
  strike: number;
  call_gex: number;
  put_gex: number;
  net_gex: number;
}

export interface ProfilePoint {
  spot: number;
  net_gex: number;
}

export interface ContractExposure {
  id: string;
  symbol: string;
  expiration: string;
  strike: number;
  option_type: OptionType;
  gamma: number;
  implied_volatility: number;
  open_interest: number;
  volume: number;
  bid: number;
  ask: number;
  mark: number;
  gex: number;
  reported_gex: number;
  model_gamma: number;
  gamma_discrepancy_ratio: number | null;
  excluded_from_aggregates: boolean;
}

export interface DataQualityImpact {
  excluded_contract_count: number;
  excluded_gex: number;
  raw_total_call_gex: number;
  raw_total_put_gex: number;
  raw_net_gex: number;
  raw_call_wall: number | null;
  raw_put_wall: number | null;
  raw_largest_gex_strike: number | null;
}

export interface GexResponse {
  symbol: string;
  spot: number;
  previous_close: number | null;
  price_change_pct: number | null;
  as_of: string;
  source: string;
  expirations: string[];
  selected_expirations: string[];
  total_call_gex: number;
  total_put_gex: number;
  net_gex: number;
  zero_gamma: number | null;
  call_wall: number | null;
  put_wall: number | null;
  largest_gex_strike: number | null;
  strike_exposure: StrikeExposure[];
  gamma_profile: ProfilePoint[];
  contracts: ContractExposure[];
  data_quality: DataQualityImpact;
  calculation_note: string;
}

export interface ConnectionStatus {
  mode: "demo" | "mcp";
  connected: boolean;
  connecting: boolean;
  authorization_url: string | null;
  server_url: string | null;
  tools: string[];
  detail: string | null;
}


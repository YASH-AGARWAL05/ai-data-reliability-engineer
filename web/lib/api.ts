export type Scenario =
  | "clean"
  | "duplicate_payments"
  | "join_cardinality"
  | "late_refunds"
  | "missing_currency_conversion"
  | "schema_drift";
export type Incident = {
  id: string;
  title: string;
  scenario: Scenario;
  seed: number;
  created_at: string;
};
type ToolEvent = {
  step: number;
  tool: string;
  result: string;
  detail: string;
};
type Evidence = {
  id: number;
  probe: string;
  sql: string;
  columns?: string[];
  rows?: unknown[][];
  error?: string | null;
};
export type Snapshot = {
  expected_net_usd: string;
  actual_net_usd: string | null;
  order_count: number;
  source_order_count: number;
  pending_refunds: number;
  model_rows: unknown[][];
};
type Comparison = {
  before: Snapshot;
  after: Snapshot;
  eligible_for_review: boolean;
  reasons: string[];
};
export type Run = {
  id: string;
  incident_id: string;
  status: string;
  cancel_requested: boolean;
  steps: number;
  tokens: number;
  max_steps: number;
  max_tokens: number;
  evidence: Evidence[];
  events: ToolEvent[];
  hypotheses: Record<
    string,
    {
      status: string;
      evidence_id: number | null;
      competing_evidence_id?: number;
    }
  >;
  diff: string;
  comparison: Comparison | null;
  reasons: string[];
  review_status: string | null;
  review_reason: string | null;
  created_at: string;
  updated_at: string;
};
export type Catalog = {
  pipeline: string;
  datasets: { name: string; rows: number }[];
  models: string[];
  tests: number;
};
type EvaluationCase = {
  scenario: Scenario;
  seed: number;
  expected_cause: string | null;
  predicted_cause: string | null;
  status: string;
  diagnosis_correct: boolean;
  fix_required: boolean;
  fix_correct: boolean | null;
  no_fix_handled_correctly: boolean | null;
  no_collateral_damage: boolean | null;
  evidence_quality: boolean;
  false_positive: boolean;
  steps: number;
  tokens: number;
  cost_usd: null;
  clean_net_usd: string | null;
  after_net_usd: string | null;
  reasons: string[];
};
type EvaluationSummary = {
  total: number;
  diagnosis_correct: number;
  fix_correct: number;
  fix_required_cases: number;
  no_fix_required_cases: number;
  no_fix_handled_correctly: number;
  evidence_quality: number;
  no_collateral_damage: number;
  collateral_assessed: number;
  false_positives: number;
  control_cases: number;
  steps: number;
  tokens: number;
  cost_usd: null;
};
export type EvaluationReport = {
  id: string;
  created_at: string;
  provider: string;
  scripted: boolean;
  model: string | null;
  model_digest?: string | null;
  variant?: string | null;
  variant_digest?: string | null;
  patch_model?: string | null;
  patch_model_digest?: string | null;
  label?: string | null;
  agent_version: string;
  fixture_version: string;
  contract_version: number;
  seeds: number[];
  scenarios: Scenario[];
  complete_suite: boolean;
  results: EvaluationCase[];
  summary: EvaluationSummary;
};

class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: { "content-type": "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new ApiError(
      "Cannot reach the local API. Start make dev and retry.",
      0,
    );
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
        ? body.detail
        : `Request failed (${String(response.status)}).`;
    throw new ApiError(detail, response.status);
  }
  return body as T;
}

export const api = {
  evaluations: (signal?: AbortSignal) =>
    request<EvaluationReport[]>("/evaluations", { signal }),
  catalog: (signal?: AbortSignal) => request<Catalog>("/catalog", { signal }),
  incidents: (signal?: AbortSignal) =>
    request<Incident[]>("/incidents", { signal }),
  incident: (id: string, signal?: AbortSignal) =>
    request<Incident>(`/incidents/${id}`, { signal }),
  runs: (signal?: AbortSignal) => request<Run[]>("/investigations", { signal }),
  run: (id: string, signal?: AbortSignal) =>
    request<Run>(`/investigations/${id}`, { signal }),
  createIncident: (title: string, scenario: Scenario, seed: number) =>
    request<Incident>("/incidents", {
      method: "POST",
      body: JSON.stringify({ title, scenario, seed }),
    }),
  createRun: (incidentId: string) =>
    request<Run>(`/incidents/${incidentId}/investigations`, {
      method: "POST",
      body: "{}",
    }),
  action: (
    id: string,
    action: "cancel" | "resume" | "approve" | "reject",
    reason?: string,
  ) =>
    request<Run>(`/investigations/${id}/${action}`, {
      method: "POST",
      body: reason === undefined ? undefined : JSON.stringify({ reason }),
    }),
};

export function label(value: string): string {
  return value.replaceAll("_", " ");
}
export function date(value: string): string {
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

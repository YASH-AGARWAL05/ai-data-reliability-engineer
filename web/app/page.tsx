"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  date,
  label,
  type Catalog,
  type Incident,
  type Run,
  type Scenario,
} from "../lib/api";
import { Notice, Status } from "../components/ui";

const scenarios: { value: Scenario; name: string }[] = [
  { value: "duplicate_payments", name: "Duplicate payments" },
  { value: "join_cardinality", name: "Join cardinality" },
  { value: "late_refunds", name: "Late refunds" },
  { value: "missing_currency_conversion", name: "Missing currency conversion" },
  { value: "schema_drift", name: "Schema drift" },
  { value: "clean", name: "Clean baseline" },
];

export default function Workspace() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [scenario, setScenario] = useState<Scenario>("duplicate_payments");
  const [seed, setSeed] = useState(7);
  const [pending, setPending] = useState(false);
  const [starting, setStarting] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const [nextCatalog, nextIncidents, nextRuns] = await Promise.all([
        api.catalog(signal),
        api.incidents(signal),
        api.runs(signal),
      ]);
      if (signal?.aborted) return;
      setCatalog(nextCatalog);
      setIncidents(nextIncidents);
      setRuns(nextRuns);
    } catch (cause) {
      if (signal?.aborted) return;
      setError(
        cause instanceof Error
          ? cause.message
          : "The workspace could not load.",
      );
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => {
      controller.abort();
    };
  }, [load]);

  async function create(event: React.SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pending || !title.trim()) return;
    setPending(true);
    setFeedback(null);
    setError(null);
    try {
      const incident = await api.createIncident(title.trim(), scenario, seed);
      setIncidents((current) => [incident, ...current].slice(0, 100));
      setTitle("");
      setFeedback(
        `Created ${incident.title}. Start its investigation from the incident list.`,
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "Could not create incident.",
      );
    } finally {
      setPending(false);
    }
  }

  async function start(incident: Incident) {
    if (starting) return;
    setStarting(incident.id);
    setError(null);
    setFeedback(null);
    try {
      const run = await api.createRun(incident.id);
      setRuns((current) => [run, ...current].slice(0, 100));
      setFeedback(`Investigation queued for ${incident.title}.`);
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Could not start investigation.",
      );
    } finally {
      setStarting(null);
    }
  }

  return (
    <div className="page-wrap">
      <div className="page-heading">
        <div>
          <p className="eyebrow">WORKSPACE / OVERVIEW</p>
          <h1>Reliability workspace</h1>
          <p className="lede">
            Inspect a seeded incident, follow the evidence, then review the
            proposed dbt change.
          </p>
        </div>
        <span className="header-context">JAFFLE SHOP · LOCAL</span>
      </div>
      {error && <Notice message={error} onRetry={() => void load()} />}
      {feedback && (
        <p className="success-note" role="status">
          {feedback}
        </p>
      )}
      <section className="overview-grid" aria-label="Pipeline overview">
        <div className="panel pipeline-panel">
          <div className="panel-kicker">PIPELINE</div>
          <div className="pipeline-name">{catalog?.pipeline ?? "Loading…"}</div>
          <p>
            Seeded CSV sources flow through dbt staging and marts into daily
            revenue.
          </p>
          <div className="pipeline-path">
            <span>RAW DATA</span>
            <i />
            <span>STAGING</span>
            <i />
            <span>REVENUE MART</span>
          </div>
        </div>
        <div className="panel metric-panel">
          <div className="panel-kicker">DATASETS</div>
          <strong>{catalog ? catalog.datasets.length : "—"}</strong>
          <span>seed sources</span>
        </div>
        <div className="panel metric-panel">
          <div className="panel-kicker">MODELS</div>
          <strong>{catalog ? catalog.models.length : "—"}</strong>
          <span>dbt SQL models</span>
        </div>
        <div className="panel metric-panel">
          <div className="panel-kicker">INCIDENTS</div>
          <strong>{loading ? "—" : incidents.length}</strong>
          <span>recent 100 max</span>
        </div>
      </section>
      <div className="workspace-grid">
        <section className="panel form-panel" aria-labelledby="new-incident">
          <p className="eyebrow">01 / GENERATE</p>
          <h2 id="new-incident">New incident</h2>
          <p className="muted">Generate a reproducible local dbt scenario.</p>
          <form onSubmit={(event) => void create(event)}>
            <label htmlFor="title">Incident title</label>
            <input
              id="title"
              name="title"
              required
              maxLength={200}
              value={title}
              onChange={(event) => {
                setTitle(event.target.value);
              }}
              placeholder="e.g. Revenue spike"
              disabled={pending}
            />
            <div className="field-group">
              <label htmlFor="scenario">Scenario</label>
              <select
                id="scenario"
                value={scenario}
                onChange={(event) => {
                  setScenario(event.target.value as Scenario);
                }}
                disabled={pending}
              >
                {scenarios.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
            <div className="field-group">
              <label htmlFor="seed">Seed</label>
              <input
                id="seed"
                type="number"
                min={0}
                max={2147483647}
                required
                value={seed}
                onChange={(event) => {
                  setSeed(Number(event.target.value));
                }}
                disabled={pending}
              />
            </div>
            <button
              className="button button-primary full"
              disabled={pending || !title.trim()}
            >
              {pending ? "Creating…" : "Create incident"}
            </button>
          </form>
        </section>
        <section className="panel catalog-panel" aria-labelledby="sources">
          <p className="eyebrow">02 / INVENTORY</p>
          <h2 id="sources">Data & pipeline</h2>
          <p className="muted">Read from the current dbt template.</p>
          {loading && !catalog ? (
            <p className="muted" role="status">
              Loading sources…
            </p>
          ) : (
            <>
              <h3>Seed datasets</h3>
              <ul className="source-list">
                {catalog?.datasets.map((item) => (
                  <li key={item.name}>
                    <span className="source-icon">CSV</span>
                    <span>{item.name}</span>
                    <span className="source-count">{item.rows} rows</span>
                  </li>
                ))}
              </ul>
              <div className="catalog-divider" />
              <div className="catalog-summary">
                <span>
                  SQL models <strong>{catalog?.models.length ?? 0}</strong>
                </span>
                <span>
                  Singular tests <strong>{catalog?.tests ?? 0}</strong>
                </span>
              </div>
            </>
          )}
        </section>
      </div>
      <section className="panel history-panel" aria-labelledby="history">
        <div className="section-top">
          <div>
            <p className="eyebrow">03 / HISTORY</p>
            <h2 id="history">Incident history</h2>
          </div>
          <span className="section-note">Latest 100 incidents</span>
        </div>
        {loading && !catalog ? (
          <p className="empty" role="status">
            Loading incident history…
          </p>
        ) : incidents.length === 0 ? (
          <p className="empty">
            No incidents yet. Create one to begin an investigation.
          </p>
        ) : (
          <div className="history-list">
            {incidents.map((incident) => {
              const run = runs.find((item) => item.incident_id === incident.id);
              return (
                <div className="history-row" key={incident.id}>
                  <div className="history-main">
                    <strong>{incident.title}</strong>
                    <span>
                      {label(incident.scenario)} · seed {incident.seed} ·{" "}
                      {date(incident.created_at)}
                    </span>
                  </div>
                  <div className="history-status">
                    {run ? (
                      <Status value={run.review_status ?? run.status} />
                    ) : (
                      <span className="muted">Not started</span>
                    )}
                  </div>
                  <div className="history-action">
                    {run ? (
                      <Link
                        className="button button-outline"
                        href={`/investigations/${run.id}`}
                      >
                        View investigation
                      </Link>
                    ) : (
                      <button
                        type="button"
                        className="button button-outline"
                        onClick={() => void start(incident)}
                        disabled={starting !== null}
                      >
                        {starting === incident.id
                          ? "Starting…"
                          : "Start investigation"}
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}

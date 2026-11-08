"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, date, label, type Incident, type Run } from "../../../lib/api";
import { Notice, Status } from "../../../components/ui";

const live = new Set(["queued", "running", "cancelling"]);

export default function InvestigationPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [incident, setIncident] = useState<Incident | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await api.run(id, signal);
        if (signal?.aborted) return;
        setRun(result);
        setError(null);
        const nextIncident = await api.incident(result.incident_id, signal);
        if (!signal?.aborted) setIncident(nextIncident);
      } catch (cause) {
        if (!signal?.aborted)
          setError(
            cause instanceof Error
              ? cause.message
              : "Could not load investigation.",
          );
      }
    },
    [id],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    const interval = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(controller.signal);
    }, 2000);
    return () => {
      controller.abort();
      window.clearInterval(interval);
    };
  }, [load]);
  async function act(action: "cancel" | "resume") {
    if (!run || pending) return;
    setPending(true);
    setError(null);
    setFeedback(null);
    try {
      const updated = await api.action(run.id, action);
      setRun(updated);
      setFeedback(
        action === "cancel"
          ? "Cancellation requested at the next checkpoint."
          : "Investigation requeued.",
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Action failed.");
    } finally {
      setPending(false);
    }
  }
  return (
    <div className="page-wrap">
      <div className="breadcrumbs">
        <Link href="/">Workspace</Link>
        <span>/</span>
        <span>Investigation</span>
      </div>
      {error && <Notice message={error} onRetry={() => void load()} />}
      {feedback && (
        <p className="success-note" role="status">
          {feedback}
        </p>
      )}
      {!run ? (
        <div className="panel empty" role="status">
          Loading investigation…
        </div>
      ) : (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">INVESTIGATION / {run.id.slice(0, 8)}</p>
              <h1>{incident?.title ?? "Investigation"}</h1>
              <p className="lede">
                A live record of decisions, SQL probes, and validation steps.
              </p>
            </div>
            <div className="heading-actions">
              <Status value={run.review_status ?? run.status} />
              {run.status === "ready_for_review" && (
                <Link
                  className="button button-primary"
                  href={`/investigations/${run.id}/patch`}
                >
                  Review patch
                </Link>
              )}
              {["queued", "running"].includes(run.status) && (
                <button
                  type="button"
                  className="button button-outline"
                  disabled={pending}
                  onClick={() => void act("cancel")}
                >
                  {pending ? "Working…" : "Cancel run"}
                </button>
              )}
              {run.status === "cancelled" && (
                <button
                  type="button"
                  className="button button-primary"
                  disabled={pending}
                  onClick={() => void act("resume")}
                >
                  {pending ? "Working…" : "Resume run"}
                </button>
              )}
            </div>
          </div>
          <div className="run-summary">
            <div>
              <span>SCENARIO</span>
              <strong>
                {incident ? label(incident.scenario) : "Loading…"}
              </strong>
            </div>
            <div>
              <span>STEPS</span>
              <strong>
                {run.steps} / {run.max_steps}
              </strong>
            </div>
            <div>
              <span>TOKENS</span>
              <strong>
                {run.tokens} / {run.max_tokens}
              </strong>
            </div>
            <div>
              <span>STARTED</span>
              <strong>{date(run.created_at)}</strong>
            </div>
          </div>
          {run.reasons.length > 0 && (
            <Notice message={run.reasons.join(" · ")} />
          )}
          <div className="investigation-grid">
            <section className="panel timeline-panel">
              <div className="section-top">
                <div>
                  <p className="eyebrow">01 / TRACE</p>
                  <h2>Tool timeline</h2>
                </div>
                {live.has(run.status) && (
                  <span className="live-indicator">
                    <span className="connection-dot" /> Live updates
                  </span>
                )}
              </div>
              {run.events.length === 0 ? (
                <p className="empty">
                  {live.has(run.status)
                    ? "Waiting for the worker to record its first step…"
                    : "No tool events were recorded for this run."}
                </p>
              ) : (
                <ol className="timeline">
                  {run.events.map((event, index) => (
                    <li key={`${String(event.step)}-${String(index)}`}>
                      <span className="timeline-node">
                        {String(event.step).padStart(2, "0")}
                      </span>
                      <div>
                        <div className="timeline-head">
                          <strong>{label(event.tool)}</strong>
                          <Status value={event.result} />
                        </div>
                        {event.detail && <p>{event.detail}</p>}
                      </div>
                    </li>
                  ))}
                </ol>
              )}
            </section>
            <aside className="evidence-column">
              <section className="panel">
                <p className="eyebrow">02 / REASONING</p>
                <h2>Hypotheses</h2>
                {Object.entries(run.hypotheses).length === 0 ? (
                  <p className="muted">
                    Hypotheses appear after the first worker step.
                  </p>
                ) : (
                  <ul className="hypothesis-list">
                    {Object.entries(run.hypotheses).map(([name, item]) => (
                      <li key={name}>
                        <div>
                          <strong>{label(name)}</strong>
                          <Status value={item.status} />
                        </div>
                        <span>
                          {item.evidence_id === null
                            ? "No linked probe yet"
                            : `Evidence #${String(item.evidence_id + 1)}`}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              <section className="panel">
                <p className="eyebrow">03 / SQL EVIDENCE</p>
                <h2>Probe results</h2>
                {run.evidence.length === 0 ? (
                  <p className="muted">
                    SQL results appear as probes complete.
                  </p>
                ) : (
                  run.evidence.map((item) => (
                    <div className="probe" key={item.id}>
                      <div className="probe-head">
                        <strong>{label(item.probe)}</strong>
                        <span>#{item.id + 1}</span>
                      </div>
                      <pre>
                        <code>{item.sql}</code>
                      </pre>
                      {item.error ? (
                        <p className="form-error">{item.error}</p>
                      ) : (
                        <p className="probe-result">
                          {item.rows?.length ?? 0} result rows
                          {item.rows && item.rows.length > 0
                            ? ` · ${JSON.stringify(item.rows)}`
                            : ""}
                        </p>
                      )}
                    </div>
                  ))
                )}
              </section>
            </aside>
          </div>
          {run.diff && (
            <div className="next-review">
              <div>
                <strong>Patch candidate available</strong>
                <span>
                  Inspect the diff, test result, and revenue comparison before
                  deciding.
                </span>
              </div>
              <Link
                className="button button-outline"
                href={`/investigations/${run.id}/patch`}
              >
                Open patch review
              </Link>
            </div>
          )}
        </>
      )}
    </div>
  );
}

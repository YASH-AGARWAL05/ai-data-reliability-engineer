"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, type Run, type Snapshot } from "../../../../lib/api";
import { Notice, ReviewDialog, Status } from "../../../../components/ui";

function metric(snapshot: Snapshot | undefined, key: keyof Snapshot): string {
  const value = snapshot?.[key];
  return value === null || value === undefined ? "—" : String(value);
}

export default function PatchPage() {
  const { id } = useParams<{ id: string }>();
  const [run, setRun] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"approve" | "reject" | null>(null);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const result = await api.run(id, signal);
        if (!signal?.aborted) {
          setRun(result);
          setError(null);
        }
      } catch (cause) {
        if (!signal?.aborted)
          setError(
            cause instanceof Error ? cause.message : "Could not load patch.",
          );
      }
    },
    [id],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => {
      controller.abort();
    };
  }, [load]);
  async function submit(reason: string) {
    if (!run || !dialog || busy) return;
    setBusy(true);
    setDialogError(null);
    try {
      const updated = await api.action(run.id, dialog, reason);
      setRun(updated);
      setDialog(null);
    } catch (cause) {
      setDialogError(
        cause instanceof Error ? cause.message : "Review could not be saved.",
      );
    } finally {
      setBusy(false);
    }
  }
  const test = run?.events
    .filter(
      (item) =>
        item.tool === "run_dbt_tests" && item.detail === "Patch candidate",
    )
    .at(-1);
  return (
    <div className="page-wrap">
      <div className="breadcrumbs">
        <Link href="/">Workspace</Link>
        <span>/</span>
        <Link href={`/investigations/${id}`}>Investigation</Link>
        <span>/</span>
        <span>Patch review</span>
      </div>
      {error && <Notice message={error} onRetry={() => void load()} />}
      {!run ? (
        <div className="panel empty" role="status">
          Loading patch review…
        </div>
      ) : (
        <>
          <div className="page-heading">
            <div>
              <p className="eyebrow">PATCH / REVIEW</p>
              <h1>Review proposed change</h1>
              <p className="lede">
                Check the source diff and independent validation before
                recording a decision.
              </p>
            </div>
            <Status value={run.review_status ?? run.status} />
          </div>
          {run.review_status && (
            <p className="success-note" role="status">
              {run.review_status === "approved" ? "Approved" : "Rejected"}:{" "}
              {run.review_reason}
            </p>
          )}
          {!run.diff && (
            <div className="panel empty">
              No patch candidate is available for this investigation.{" "}
              <Link href={`/investigations/${id}`}>Return to timeline</Link>.
            </div>
          )}
          {run.diff && (
            <>
              <section className="panel review-summary">
                <p className="eyebrow">VALIDATION GATE</p>
                <div className="review-gates">
                  <div>
                    <span>DBT TESTS</span>
                    <Status value={test?.result ?? "not recorded"} />
                  </div>
                  <div>
                    <span>METRIC CHECK</span>
                    <Status
                      value={
                        run.comparison?.eligible_for_review
                          ? "eligible"
                          : "ineligible"
                      }
                    />
                  </div>
                  <div>
                    <span>REVIEW STATE</span>
                    <Status value={run.review_status ?? "awaiting decision"} />
                  </div>
                </div>
                {run.comparison?.reasons.length ? (
                  <p className="form-error">
                    {run.comparison.reasons.join(" · ")}
                  </p>
                ) : null}
              </section>
              <div className="review-grid">
                <section className="panel diff-panel">
                  <div className="section-top">
                    <div>
                      <p className="eyebrow">01 / CHANGE</p>
                      <h2>Model diff</h2>
                    </div>
                    <span className="section-note">Sandbox copy only</span>
                  </div>
                  <pre className="diff">
                    <code>
                      {run.diff.split("\n").map((line, index) => (
                        <span
                          key={index}
                          className={
                            line.startsWith("+") && !line.startsWith("+++")
                              ? "diff-add"
                              : line.startsWith("-") && !line.startsWith("---")
                                ? "diff-remove"
                                : ""
                          }
                        >
                          {line}
                          {"\n"}
                        </span>
                      ))}
                    </code>
                  </pre>
                </section>
                <section className="panel comparison-panel">
                  <p className="eyebrow">02 / IMPACT</p>
                  <h2>Before / after</h2>
                  <p className="muted">
                    Independent revenue reconciliation and order counts.
                  </p>
                  <div className="comparison-head">
                    <span>METRIC</span>
                    <span>BEFORE</span>
                    <span>AFTER</span>
                  </div>
                  {(
                    [
                      ["Expected net USD", "expected_net_usd"],
                      ["Actual net USD", "actual_net_usd"],
                      ["Order count", "order_count"],
                      ["Source orders", "source_order_count"],
                      ["Pending refunds", "pending_refunds"],
                    ] as const
                  ).map(([name, key]) => (
                    <div className="comparison-row" key={key}>
                      <span>{name}</span>
                      <strong>{metric(run.comparison?.before, key)}</strong>
                      <strong>{metric(run.comparison?.after, key)}</strong>
                    </div>
                  ))}
                  <p className="comparison-foot">
                    The server also checks row-level hashes of unrelated models
                    before a patch becomes reviewable.
                  </p>
                </section>
              </div>
              <div className="decision-panel">
                <div>
                  <p className="eyebrow">03 / DECISION</p>
                  <strong>Record your review</strong>
                  <span>
                    Approval stores a decision; it does not apply the change to
                    the source project.
                  </span>
                </div>
                <div className="decision-actions">
                  <button
                    type="button"
                    className="button button-outline"
                    onClick={() => {
                      setDialogError(null);
                      setDialog("reject");
                    }}
                    disabled={
                      run.status !== "ready_for_review" || !!run.review_status
                    }
                  >
                    Reject
                  </button>
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={() => {
                      setDialogError(null);
                      setDialog("approve");
                    }}
                    disabled={
                      run.status !== "ready_for_review" || !!run.review_status
                    }
                  >
                    Approve patch
                  </button>
                </div>
              </div>
            </>
          )}
        </>
      )}
      <ReviewDialog
        action={dialog}
        busy={busy}
        error={dialogError}
        onClose={() => {
          if (!busy) setDialog(null);
        }}
        onSubmit={(reason) => void submit(reason)}
      />
    </div>
  );
}

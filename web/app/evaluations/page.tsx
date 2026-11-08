"use client";

import { useCallback, useEffect, useState } from "react";
import { Notice, Status } from "../../components/ui";
import { api, date, label, type EvaluationReport } from "../../lib/api";
import "./evaluations.css";

function fraction(count: number, total: number): string {
  return total === 0 ? "—" : `${String(count)} / ${String(total)}`;
}

function change(current: number, baseline: number): string {
  const difference = current - baseline;
  return difference > 0 ? `+${String(difference)}` : String(difference);
}

function sameCases(first: EvaluationReport, second: EvaluationReport): boolean {
  return (
    first.agent_version !== second.agent_version &&
    first.provider === second.provider &&
    first.scripted === second.scripted &&
    first.model === second.model &&
    first.variant_digest === second.variant_digest &&
    first.patch_model_digest === second.patch_model_digest &&
    first.fixture_version === second.fixture_version &&
    first.contract_version === second.contract_version &&
    JSON.stringify(first.seeds) === JSON.stringify(second.seeds) &&
    JSON.stringify(first.scenarios) === JSON.stringify(second.scenarios)
  );
}

export default function EvaluationPage() {
  const [reports, setReports] = useState<EvaluationReport[]>([]);
  const [selectedId, setSelectedId] = useState("");
  const [baselineId, setBaselineId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const next = await api.evaluations(signal);
      if (signal?.aborted) return;
      setReports(next);
      setSelectedId((current) =>
        next.some((item) => item.id === current)
          ? current
          : (next[0]?.id ?? ""),
      );
    } catch (cause) {
      if (!signal?.aborted)
        setError(
          cause instanceof Error
            ? cause.message
            : "Evaluation reports could not load.",
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

  const report: EvaluationReport | undefined =
    reports.find((item) => item.id === selectedId) ?? reports.at(0);
  const compatible = report
    ? reports.filter((item) => sameCases(report, item))
    : [];
  const baseline: EvaluationReport | undefined =
    compatible.find((item) => item.id === baselineId) ?? compatible.at(0);

  return (
    <div className="page-wrap">
      <div className="page-heading">
        <div>
          <p className="eyebrow">EVALUATION / RESULTS</p>
          <h1>Evaluation ledger</h1>
          <p className="lede">
            Seeded scenarios scored against ground truth after each
            investigation completes.
          </p>
        </div>
        <span className="header-context">EVALUATION RESULTS</span>
      </div>
      {error && (
        <Notice
          message={error}
          onRetry={() => {
            void load();
          }}
        />
      )}
      {loading && reports.length === 0 ? (
        <div className="panel empty" role="status">
          Loading evaluation reports…
        </div>
      ) : reports.length === 0 ? (
        <section className="panel eval-empty">
          <p className="eyebrow">NO REPORTS YET</p>
          <h2>Run the scenario matrix</h2>
          <p>
            In the project terminal, run <code>make eval</code>. The completed
            report will appear here after you refresh.
          </p>
          <button
            type="button"
            className="button button-outline"
            onClick={() => {
              void load();
            }}
          >
            Refresh reports
          </button>
        </section>
      ) : null}
      {report && (
        <>
          <section className="panel eval-toolbar" aria-label="Report selection">
            <div>
              <label htmlFor="report-select">Evaluation report</label>
              <select
                id="report-select"
                value={report.id}
                onChange={(event) => {
                  setSelectedId(event.target.value);
                  setBaselineId("");
                }}
              >
                {reports.map((item) => (
                  <option key={item.id} value={item.id}>
                    {date(item.created_at)} · agent {item.agent_version} ·{" "}
                    {String(item.summary.total)} cases
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              className="button button-outline"
              onClick={() => {
                void load();
              }}
              disabled={loading}
            >
              {loading ? "Refreshing…" : "Refresh reports"}
            </button>
          </section>
          <div className="eval-meta">
            <span>
              <strong>Agent</strong> {report.agent_version}
            </span>
            <span>
              <strong>Fixture</strong> {report.fixture_version}
            </span>
            <span>
              <strong>Seeds</strong> {report.seeds.join(", ")}
            </span>
            <span>
              <strong>Provider</strong> {report.provider}
            </span>
            <span>
              <strong>Run type</strong>{" "}
              {report.provider === "fake" || report.scripted
                ? "Scripted, not agent performance"
                : report.provider === "random-probe"
                  ? "Random-probe baseline"
                  : report.provider === "ollama-guided"
                    ? "Guided local model with fixed probe sweep"
                    : "Unscripted local model"}
            </span>
            {report.label && (
              <span>
                <strong>Scope</strong> {report.label}
              </span>
            )}
            {report.model && (
              <span>
                <strong>Model</strong> {report.model}
              </span>
            )}
            {report.patch_model && (
              <span>
                <strong>Patch model</strong> {report.patch_model}
              </span>
            )}
          </div>
          {!report.complete_suite && (
            <Notice message="Partial suite: this report does not cover every defined scenario with multiple seeds." />
          )}
          {report.contract_version < 2 && (
            <Notice message="Legacy scoring contract: rerun make eval for separate repair and no-fix scores." />
          )}
          <section className="eval-score-grid" aria-label="Evaluation scores">
            <div className="panel eval-score">
              <span>ROOT CAUSE</span>
              <strong>
                {fraction(
                  report.summary.diagnosis_correct,
                  report.summary.total,
                )}
              </strong>
              <p>Correct diagnosis</p>
            </div>
            <div className="panel eval-score">
              <span>REPAIR</span>
              <strong>
                {fraction(
                  report.summary.fix_correct,
                  report.summary.fix_required_cases,
                )}
              </strong>
              <p>Correct code fixes</p>
            </div>
            <div className="panel eval-score">
              <span>NO CODE FIX</span>
              <strong>
                {fraction(
                  report.summary.no_fix_handled_correctly,
                  report.summary.no_fix_required_cases,
                )}
              </strong>
              <p>Correctly handled without a patch</p>
            </div>
            <div className="panel eval-score">
              <span>EVIDENCE</span>
              <strong>
                {fraction(
                  report.summary.evidence_quality,
                  report.summary.total,
                )}
              </strong>
              <p>Supported and competing probes</p>
            </div>
            <div className="panel eval-score">
              <span>CONTROL</span>
              <strong>
                {fraction(
                  report.summary.false_positives,
                  report.summary.control_cases,
                )}
              </strong>
              <p>False positives on clean cases</p>
            </div>
          </section>
          <div className="eval-detail-grid">
            <section className="panel eval-matrix">
              <div className="section-top">
                <div>
                  <p className="eyebrow">01 / COVERAGE</p>
                  <h2>Scenario × seed</h2>
                </div>
                <span className="section-note">Correct outcome by case</span>
              </div>
              <div className="matrix-scroll">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Scenario</th>
                      {report.seeds.map((seed) => (
                        <th scope="col" key={seed}>
                          Seed {seed}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {report.scenarios.map((scenario) => (
                      <tr key={scenario}>
                        <th scope="row">{label(scenario)}</th>
                        {report.seeds.map((seed) => {
                          const item = report.results.find(
                            (result) =>
                              result.scenario === scenario &&
                              result.seed === seed,
                          );
                          return (
                            <td key={seed}>
                              <span
                                className={`matrix-result ${item?.fix_correct || item?.no_fix_handled_correctly ? "matrix-pass" : "matrix-miss"}`}
                              >
                                {item?.fix_correct ||
                                item?.no_fix_handled_correctly
                                  ? "Correct"
                                  : item?.status === "evaluation_error"
                                    ? "Error"
                                    : "Missed"}
                              </span>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <section className="panel eval-run-cost">
              <p className="eyebrow">02 / EFFORT & SAFETY</p>
              <h2>Run totals</h2>
              <div className="eval-totals">
                <div>
                  <span>Steps used</span>
                  <strong>{report.summary.steps}</strong>
                </div>
                <div>
                  <span>Tokens used</span>
                  <strong>{report.summary.tokens}</strong>
                </div>
                <div>
                  <span>No collateral damage</span>
                  <strong>
                    {fraction(
                      report.summary.no_collateral_damage,
                      report.summary.collateral_assessed,
                    )}
                  </strong>
                </div>
                <div>
                  <span>Dollar cost</span>
                  <strong>Unpriced</strong>
                </div>
              </div>
              <p className="muted">
                Token counts are recorded; monetary cost is not priced.
                Collateral damage is assessed only when a patch reaches
                comparison.
              </p>
            </section>
          </div>
          <section className="panel eval-compare">
            <div className="section-top">
              <div>
                <p className="eyebrow">03 / VERSION COMPARISON</p>
                <h2>Compare agent versions</h2>
              </div>
            </div>
            {baseline ? (
              <>
                <label htmlFor="baseline-select">Baseline report</label>
                <select
                  id="baseline-select"
                  value={baseline.id}
                  onChange={(event) => {
                    setBaselineId(event.target.value);
                  }}
                >
                  {compatible.map((item) => (
                    <option key={item.id} value={item.id}>
                      {date(item.created_at)} · agent {item.agent_version}
                    </option>
                  ))}
                </select>
                <p className="muted">
                  Same fixture, scenarios, and seeds. Change is candidate minus
                  baseline, in case counts.
                </p>
                <div className="compare-grid">
                  <div>
                    <span>Correct diagnosis</span>
                    <strong>
                      {change(
                        report.summary.diagnosis_correct,
                        baseline.summary.diagnosis_correct,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Correct fixes</span>
                    <strong>
                      {change(
                        report.summary.fix_correct,
                        baseline.summary.fix_correct,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Correct no-fix outcomes</span>
                    <strong>
                      {change(
                        report.summary.no_fix_handled_correctly,
                        baseline.summary.no_fix_handled_correctly,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Evidence quality</span>
                    <strong>
                      {change(
                        report.summary.evidence_quality,
                        baseline.summary.evidence_quality,
                      )}
                    </strong>
                  </div>
                  <div>
                    <span>Clean false positives</span>
                    <strong>
                      {change(
                        report.summary.false_positives,
                        baseline.summary.false_positives,
                      )}
                    </strong>
                  </div>
                </div>
              </>
            ) : (
              <p className="empty">
                No comparable report has the same provider, model, fixture,
                scoring contract, and seed set.
              </p>
            )}
          </section>
          <section className="panel eval-cases">
            <div className="section-top">
              <div>
                <p className="eyebrow">04 / CASE RECORD</p>
                <h2>Every run</h2>
              </div>
              <span className="section-note">
                {String(report.results.length)} cases, including failures
              </span>
            </div>
            <div className="case-scroll">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Scenario / seed</th>
                    <th scope="col">Expected cause</th>
                    <th scope="col">Verified cause</th>
                    <th scope="col">Status</th>
                    <th scope="col">Diagnosis</th>
                    <th scope="col">Fix</th>
                    <th scope="col">No-fix handling</th>
                    <th scope="col">Collateral</th>
                    <th scope="col">Evidence</th>
                    <th scope="col">Steps</th>
                    <th scope="col">Tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {report.results.map((item) => (
                    <tr key={`${item.scenario}-${String(item.seed)}`}>
                      <th scope="row">
                        <strong>{label(item.scenario)}</strong>
                        <span>Seed {item.seed}</span>
                        {item.reasons.length > 0 && (
                          <small>{item.reasons.join(" · ")}</small>
                        )}
                      </th>
                      <td>
                        {item.expected_cause
                          ? label(item.expected_cause)
                          : "Unknown"}
                      </td>
                      <td>
                        {item.predicted_cause
                          ? label(item.predicted_cause)
                          : "Undetermined"}
                      </td>
                      <td>
                        <Status value={item.status} />
                      </td>
                      <td>{item.diagnosis_correct ? "Correct" : "Missed"}</td>
                      <td>
                        {item.fix_correct === null
                          ? "Not required"
                          : item.fix_correct
                            ? "Correct"
                            : "Missed"}
                      </td>
                      <td>
                        {item.no_fix_handled_correctly === null
                          ? "Not applicable"
                          : item.no_fix_handled_correctly
                            ? "Correct"
                            : "Missed"}
                      </td>
                      <td>
                        {item.no_collateral_damage === null
                          ? "Not assessed"
                          : item.no_collateral_damage
                            ? "Yes"
                            : "No"}
                      </td>
                      <td>
                        {item.evidence_quality ? "Sufficient" : "Insufficient"}
                      </td>
                      <td>{item.steps}</td>
                      <td>{item.tokens}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <p className="eval-footnote">
              A clean run with no patch is not counted as a correct conclusion
              unless the agent explicitly verifies “no defect.” No false
              positive does not imply a resolved control case.
            </p>
          </section>
        </>
      )}
    </div>
  );
}

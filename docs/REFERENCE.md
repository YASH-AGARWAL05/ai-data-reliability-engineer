# Technical reference

## Layout

`analytics/template/` contains the dbt project, seeds, models, tests, and retained upstream license. `analytics/scenarios/` defines six mutations. `src/ai_data_reliability/analytics/scenarios.py` generates fixtures. `src/ai_data_reliability/investigator/` contains the graph, providers, probes, and sandbox tools. `src/ai_data_reliability/service/` contains API, worker, and persistence code. `src/ai_data_reliability/evaluation/` contains the hidden-truth contract, scorer, runner, and report model. `web/` contains the UI; `tests/` and `e2e/` contain Python and browser tests.

## Investigator tools

`InvestigatorTools` methods accept sandbox IDs rather than arbitrary host paths except at sandbox creation. The table describes public methods in `investigator/sandbox.py`.

| Method                   | Inputs                              | Output             | Guardrail                                                      |
| ------------------------ | ----------------------------------- | ------------------ | -------------------------------------------------------------- |
| `create_sandbox`         | Allowlisted source project path     | Sandbox handle     | Copies project, omits truth and database files                 |
| `read_model_code`        | Sandbox ID, model path              | SQL text           | Existing model SQL path only                                   |
| `get_lineage`            | Sandbox ID, model path              | Lineage            | Reads copied project metadata                                  |
| `get_recent_changes`     | Sandbox ID, model path              | Recent changes     | Bound to copied project                                        |
| `apply_patch_in_sandbox` | Sandbox ID, model path, new SQL     | Unified diff       | Existing model SQL, SQL/Jinja validation, protected files      |
| `run_sql`                | Sandbox ID, SQL, row limit, timeout | Rows or error      | Single bounded SELECT, external access disabled, Docker limits |
| `list_tables`            | Sandbox ID                          | Table names        | Bound to sandbox database                                      |
| `describe_table`         | Sandbox ID, table name              | Column information | Bound to sandbox database                                      |
| `run_dbt_tests`          | Sandbox ID                          | dbt check          | Runs in bounded container                                      |
| `compare_metrics`        | Before and after sandbox IDs        | Metric comparison  | Revenue, order count, and unrelated row checks                 |

The graph accepts a hypothesis only with linked tool evidence and a tested competing probe. Patches require passing dbt tests and eligible reconciliation. See [Architecture](ARCHITECTURE.md).

## HTTP API

The service binds to loopback for local development and has no authentication.

| Method | Route                                     | Purpose                               |
| ------ | ----------------------------------------- | ------------------------------------- |
| GET    | `/health`                                 | Health status                         |
| GET    | `/catalog`                                | Fixture and model catalog             |
| GET    | `/evaluations`                            | Saved evaluation reports              |
| POST   | `/incidents`                              | Create a seeded incident              |
| GET    | `/incidents`                              | List incidents                        |
| GET    | `/incidents/{incident_id}`                | Read an incident                      |
| POST   | `/incidents/{incident_id}/investigations` | Queue an investigation                |
| GET    | `/investigations`                         | List runs                             |
| GET    | `/investigations/{run_id}`                | Read state, evidence, and review data |
| POST   | `/investigations/{run_id}/cancel`         | Request cancellation                  |
| POST   | `/investigations/{run_id}/resume`         | Resume a paused run                   |
| POST   | `/investigations/{run_id}/approve`        | Record approval with reason           |
| POST   | `/investigations/{run_id}/reject`         | Record rejection with reason          |

## UI pages

| Route                        | Main content                                                             |
| ---------------------------- | ------------------------------------------------------------------------ |
| `/`                          | Catalog, incident form, incident and run lists                           |
| `/investigations/[id]`       | Checkpoint timeline, evidence, status, cancellation and resume           |
| `/investigations/[id]/patch` | SQL diff, dbt and metric review, approve and reject controls             |
| `/evaluations`               | Report selection, provider labels, outcome counts, compatible comparison |

## Scenarios and scoring

| Scenario                      | Mutation                 | Expected diagnosis                        | Fix required |
| ----------------------------- | ------------------------ | ----------------------------------------- | ------------ |
| `clean`                       | None                     | `none`, verified clean                    | No           |
| `duplicate_payments`          | Duplicate payment event  | `duplicate_payment_event`                 | Yes          |
| `join_cardinality`            | Multiplied payment join  | `multiply_payment_join`                   | Yes          |
| `late_refunds`                | Delayed refund ingestion | `delay_refund_ingestion`, verified no fix | No           |
| `missing_currency_conversion` | Omitted FX conversion    | `omit_fx_conversion`                      | Yes          |
| `schema_drift`                | Renamed payment amount   | `rename_payment_amount`                   | Yes          |

Each diagnosis needs the matching conclusion and outcome, linked tool evidence from the same run and sandbox, and an empty competing probe. A repair also needs a model SQL diff, eligible metric reconciliation, unchanged order count, and no unrelated row changes. No-fix cases require verified terminal state without a patch. A clean-case defect claim is a false positive. The exact probe pairings are in [Evaluation](EVALUATION.md) and `evaluation/contracts.py`.

The provider interface includes a scripted default, an OpenAI Responses provider, and local Ollama providers. The optional experimental local provider uses a fixed probe sweep and a separate local coding model; it is kept because the local runner and tests use it. No score is attributed to that workflow here.

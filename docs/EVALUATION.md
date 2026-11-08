# Evaluation contract

The generator builds one of six seeded dbt incidents and writes ground truth outside the investigator's sandbox. The scorer reads the hidden cause, expected fix requirement, and clean revenue only after the run. It scores diagnosis, repair, no-fix handling, evidence quality, collateral changes, and clean-control false positives separately.

| Scenario                      | Injected cause               | Correct outcome           | Supporting evidence                       | Competing test                  |
| ----------------------------- | ---------------------------- | ------------------------- | ----------------------------------------- | ------------------------------- |
| `clean`                       | No mutation                  | Verified clean, no patch  | Revenue reconciliation has no discrepancy | Pending refunds empty           |
| `duplicate_payments`          | Duplicate payment event      | Patch model SQL           | Duplicate payment IDs present             | Currency join cardinality empty |
| `join_cardinality`            | Payment join multiplies rows | Patch model SQL           | Join multiplication present               | Duplicate payment IDs empty     |
| `late_refunds`                | Refund ingestion delayed     | Verified no fix, no patch | Pending refunds present                   | Refund model error empty        |
| `missing_currency_conversion` | FX conversion omitted        | Patch model SQL           | Missing conversion present                | Currency join cardinality empty |
| `schema_drift`                | Payment amount renamed       | Patch model SQL           | Schema probe identifies mismatch          | Duplicate payment IDs empty     |

A correct diagnosis requires the conclusion label and outcome to match truth, a verified supporting hypothesis linked to the exact tool-recorded query for this run and sandbox, and a tested competing probe with empty rows. Model-authored text cannot substitute for a query result. A correct repair additionally requires a model SQL diff, eligible graph review state, successful dbt tests, net revenue within USD 0.005 of the clean target, the expected order count, and no row-level changes in unrelated models. Test or configuration edits are rejected. Clean and delayed-refund cases are correct only when the corresponding verified terminal outcome has no patch or comparison. A reported defect on the clean control is a false positive.

## Scorer validation

The Python suite contains explicit hand-built perfect outcomes for each scenario, with seeds 7, 8, and 9. It also tests lazy guesses without evidence, test edits, and patches that change unrelated models. Near misses cover wrong causes, unreconciled patches, and a patch on the delayed-refund case. The graph gate suite drives adversarial scripted decisions across the same scenario and seed matrix. Test collection commands report the suite sizes; they include positive controls as well as adversarial cases.

```sh
.venv/bin/pytest --collect-only -q tests/test_evaluation.py tests/test_scorer_near_misses.py
.venv/bin/pytest --collect-only -q tests/test_graph_adversarial.py tests/test_graph_gate_units.py
.venv/bin/pytest --collect-only -q tests/test_guard_units.py tests/test_investigator_tools.py
```

Run the scripted evaluation with:

```sh
make eval EVAL_ARGS='--provider fake --seeds 7'
```

Its label is **scripted, checks the evaluation plumbing, not agent performance**. The provider follows scenario-specific scripted decisions and exercises the fixture, tools, graph, scorer, report, and dashboard path. It is not evidence of agent behavior with an unscripted model.

The harness supports running real models through an OpenAI provider or local Ollama providers. Broader held-out evaluation across models and seeds is future work. Real-model evaluation so far is limited to small local models, so the project demonstrates the evaluation and safety framework rather than a tuned agent.

The default matrix uses six scenarios and seeds 7, 8, and 9; a partial run is marked as partial. Reports carry provider, model, scenario, seed, source fingerprints, and contract version so different configurations are not silently compared. The dashboard only compares compatible reports.

## Limits

The scenarios are synthetic, the SQL probe menu is fixed, and constrained model output does not test open-ended warehouse exploration. The guard does not close time-of-check/time-of-use or host-process trust. A scripted run validates plumbing only. A local run on a small model or a single seed cannot establish performance on unseen warehouses or models.

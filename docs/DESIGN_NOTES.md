# Design notes

| Decision                                     | Reason                                        | Trade-off                                    |
| -------------------------------------------- | --------------------------------------------- | -------------------------------------------- |
| Use PostgreSQL as the worker queue           | One source of truth for run state             | Local setup needs a database container       |
| Checkpoint the graph                         | Resume at step boundaries                     | SQLite checkpoints are local to this app     |
| Keep truth outside the sandbox               | Prevent the investigator from reading answers | Scoring is a separate post-run process       |
| Copy each dbt project                        | Isolate scenario changes                      | More disk and Docker work per run            |
| Allow only existing model SQL patches        | Bound the write surface                       | Cannot repair macros or configuration        |
| Limit SQL to one SELECT and a probe menu     | Make evidence inspectable                     | Cannot explore arbitrary warehouse questions |
| Require a competing probe                    | Test a plausible alternative                  | Fixed pairings may miss other causes         |
| Reconcile against independent metrics        | Catch total changes caused by patches         | Fixture-specific reconciliation logic        |
| Check unrelated model rows                   | Catch collateral changes hidden by aggregates | Comparison adds execution time               |
| Keep approval separate from applying changes | Make review explicit                          | Reviewers must integrate a patch themselves  |
| Use a scripted provider for default setup    | Run without a key or model download           | It validates plumbing, not model performance |

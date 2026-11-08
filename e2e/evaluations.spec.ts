import { expect, test } from "@playwright/test";

const caseResult = {
  scenario: "duplicate_payments",
  seed: 7,
  expected_cause: "duplicate_payment_event",
  predicted_cause: "duplicate_payment_event",
  status: "ready_for_review",
  diagnosis_correct: true,
  fix_required: true,
  fix_correct: true,
  no_fix_handled_correctly: null,
  no_collateral_damage: true,
  evidence_quality: true,
  false_positive: false,
  steps: 4,
  tokens: 75,
  cost_usd: null,
  clean_net_usd: "100.00",
  after_net_usd: "100.00",
  reasons: [],
};

function report(id: string, agent: string, fixes: number) {
  return {
    id,
    created_at: "2026-11-07T10:00:00Z",
    provider: "fake",
    agent_version: agent,
    fixture_version: "fixture-v1",
    contract_version: 2,
    seeds: [7],
    scenarios: ["duplicate_payments"],
    complete_suite: false,
    results: [{ ...caseResult, fix_correct: fixes === 1 }],
    summary: {
      total: 1,
      diagnosis_correct: 1,
      fix_correct: fixes,
      fix_required_cases: 1,
      no_fix_required_cases: 0,
      no_fix_handled_correctly: 0,
      evidence_quality: 1,
      no_collateral_damage: 1,
      collateral_assessed: 1,
      false_positives: 0,
      control_cases: 0,
      steps: 4,
      tokens: 75,
      cost_usd: null,
    },
  };
}

test("evaluation empty state links the run command", async ({ page }) => {
  await page.route("**/api/evaluations", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/evaluations");
  await expect(
    page.getByRole("heading", { name: "Evaluation ledger" }),
  ).toBeVisible();
  await expect(page.getByText("make eval")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Workspace", exact: true }),
  ).toBeVisible();
});

test("evaluation shows scores, case rows, and matched version comparison", async ({
  page,
}) => {
  await page.route("**/api/evaluations", (route) =>
    route.fulfill({
      json: [report("new", "agent-b", 1), report("old", "agent-a", 0)],
    }),
  );
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/evaluations");
  await expect(
    page.getByRole("heading", { name: "Evaluation ledger" }),
  ).toBeVisible();
  await expect(page.getByText("Partial suite:")).toBeVisible();
  await expect(page.getByText("Scripted, not agent performance")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Compare agent versions" }),
  ).toBeVisible();
  await expect(page.getByText("+1")).toBeVisible();
  await expect(
    page
      .locator(".case-scroll")
      .getByRole("row", { name: /duplicate payments Seed 7/ }),
  ).toBeVisible();
  const table = page.locator(".case-scroll");
  await expect(table).toBeVisible();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 375);
  await page.getByLabel("Evaluation report").selectOption("old");
  await expect(page.getByText("-1")).toBeVisible();
});

test("local model reports do not compare with scripted or baseline reports", async ({
  page,
}) => {
  const local = {
    ...report("local", "agent-c", 0),
    provider: "ollama-local",
    scripted: false,
    model: "llama3.2:3b",
    variant_digest: "local-digest",
    label:
      "ollama-local, llama3.2:3b, 6 runs, single seed, structured output constrained to the probe menu",
  };
  const baseline = {
    ...report("baseline", "agent-d", 0),
    provider: "random-probe",
    scripted: false,
  };
  await page.route("**/api/evaluations", (route) =>
    route.fulfill({ json: [local, baseline, report("fake", "agent-a", 0)] }),
  );
  await page.goto("/evaluations");
  await expect(page.getByText(local.label)).toBeVisible();
  await expect(
    page.getByText(/No comparable report has the same provider/),
  ).toBeVisible();
  await expect(page.getByLabel("Baseline report")).toHaveCount(0);
  await page.getByLabel("Evaluation report").selectOption("baseline");
  await expect(page.getByText("Random-probe baseline")).toBeVisible();
  await expect(
    page.getByText(/No comparable report has the same provider/),
  ).toBeVisible();
  await page.getByLabel("Evaluation report").selectOption("fake");
  await expect(page.getByText("Scripted, not agent performance")).toBeVisible();
});

test("guided reports display both local models and remain a separate comparison", async ({
  page,
}) => {
  const guided = {
    ...report("guided", "agent-e", 1),
    provider: "ollama-guided",
    scripted: false,
    model: "llama3.2:3b",
    patch_model: "qwen2.5-coder:7b",
    patch_model_digest: "coder-digest",
    label:
      "ollama-guided, llama3.2:3b diagnosis, qwen2.5-coder:7b patch, fixed probe sweep, single seed",
  };
  const local = {
    ...report("local", "agent-f", 0),
    provider: "ollama-local",
    scripted: false,
    model: "llama3.2:3b",
  };
  await page.route("**/api/evaluations", (route) =>
    route.fulfill({ json: [guided, local] }),
  );
  await page.goto("/evaluations");
  await expect(
    page.getByText("Guided local model with fixed probe sweep"),
  ).toBeVisible();
  await expect(page.getByText(guided.label)).toBeVisible();
  await expect(page.locator(".eval-meta")).toContainText(
    "Patch model qwen2.5-coder:7b",
  );
  await expect(
    page.getByText(/No comparable report has the same provider/),
  ).toBeVisible();
  await expect(page.getByLabel("Baseline report")).toHaveCount(0);
});

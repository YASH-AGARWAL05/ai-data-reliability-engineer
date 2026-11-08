import { expect, test } from "@playwright/test";

const id = "11111111-1111-4111-8111-111111111111";
const incidentId = "22222222-2222-4222-8222-222222222222";
const snapshot = {
  expected_net_usd: "1736.85",
  actual_net_usd: "1736.85",
  order_count: 99,
  source_order_count: 99,
  pending_refunds: 0,
  model_rows: [],
};
const run = {
  id,
  incident_id: incidentId,
  status: "ready_for_review",
  cancel_requested: false,
  steps: 4,
  tokens: 75,
  max_steps: 10,
  max_tokens: 1000,
  evidence: [
    {
      id: 0,
      probe: "duplicate_payment_ids",
      sql: "select id from raw_payments",
      rows: [["pay-1"]],
      error: null,
    },
  ],
  events: [
    {
      step: 2,
      tool: "run_sql",
      result: "completed",
      detail: "duplicate_payment_ids",
    },
    {
      step: 4,
      tool: "run_dbt_tests",
      result: "passed",
      detail: "Patch candidate",
    },
  ],
  hypotheses: {
    duplicate_payment_ids: { status: "verified", evidence_id: 0 },
  },
  diff: "--- a/models/staging/stg_payments.sql\n+++ b/models/staging/stg_payments.sql\n+select 1\n",
  comparison: {
    before: { ...snapshot, actual_net_usd: "1759.85" },
    after: snapshot,
    eligible_for_review: true,
    reasons: [],
  },
  reasons: [],
  review_status: null,
  review_reason: null,
  created_at: "2026-11-07T10:00:00Z",
  updated_at: "2026-11-07T10:00:00Z",
};

test("investigation links the query result to its hypothesis", async ({
  page,
}) => {
  await page.route(`**/api/investigations/${id}`, (route) =>
    route.fulfill({ json: run }),
  );
  await page.route(`**/api/incidents/${incidentId}`, (route) =>
    route.fulfill({
      json: {
        id: incidentId,
        title: "Duplicate event",
        scenario: "duplicate_payments",
        seed: 7,
        created_at: run.created_at,
      },
    }),
  );
  await page.goto(`/investigations/${id}`);
  await expect(
    page.getByRole("heading", { name: "Duplicate event" }),
  ).toBeVisible();
  await expect(page.getByText("Evidence #1")).toBeVisible();
  await expect(page.getByText("1 result rows")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Review patch" }).first(),
  ).toBeVisible();
});

test("patch view shows the accepted diff and reconciliation", async ({
  page,
}) => {
  await page.route(`**/api/investigations/${id}`, (route) =>
    route.fulfill({ json: run }),
  );
  await page.goto(`/investigations/${id}/patch`);
  await expect(
    page.getByRole("heading", { name: "Review proposed change" }),
  ).toBeVisible();
  await expect(page.locator(".diff")).toContainText("+select 1");
  await expect(page.locator(".review-gates")).toContainText("passed");
  await expect(page.locator(".review-gates")).toContainText("eligible");
  await expect(page.locator(".comparison-panel")).toContainText("1759.85");
  await expect(page.locator(".comparison-panel")).toContainText("1736.85");
  await expect(
    page.getByRole("button", { name: "Approve patch" }),
  ).toBeEnabled();
});

import { expect, test } from "@playwright/test";

test("create, investigate, review and approve a real incident", async ({
  page,
}) => {
  const title = `Revenue spike ${Date.now()}`;
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Reliability workspace" }),
  ).toBeVisible();
  await expect(page.getByText("raw_payments")).toBeVisible();
  await page.getByLabel("Incident title").fill(title);
  await page.getByRole("button", { name: "Create incident" }).click();
  const incident = page.locator(".history-row").filter({ hasText: title });
  await expect(incident).toBeVisible();
  await incident.getByRole("button", { name: "Start investigation" }).click();
  await incident.getByRole("link", { name: "View investigation" }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(page.getByRole("link", { name: "Review patch" })).toBeVisible({
    timeout: 150_000,
  });
  await expect(
    page.getByText("run sql", { exact: true }).first(),
  ).toBeVisible();
  await page.getByRole("link", { name: "Review patch" }).click();
  await expect(
    page.getByRole("heading", { name: "Review proposed change" }),
  ).toBeVisible();
  await expect(page.getByText("passed", { exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Approve patch" }).click();
  await page
    .getByLabel("Reason")
    .fill("Evidence and metrics checked in browser test");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Approve patch" })
    .click();
  await expect(
    page.getByText("Approved: Evidence and metrics checked in browser test"),
  ).toBeVisible();
});

test("empty and API failure states remain navigable", async ({ page }) => {
  await page.route("**/api/incidents", (route) => route.fulfill({ json: [] }));
  await page.route("**/api/investigations", (route) =>
    route.fulfill({ json: [] }),
  );
  await page.goto("/");
  await expect(
    page.getByText("No incidents yet. Create one to begin an investigation."),
  ).toBeVisible();
  await page.goto("/investigations/00000000-0000-0000-0000-000000000000");
  await expect(page.locator(".notice")).toContainText(
    "Investigation not found",
  );
  await expect(
    page.getByRole("link", { name: "Workspace" }).first(),
  ).toBeVisible();
});

test("narrow workspace form works by keyboard", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/");
  const title = page.getByLabel("Incident title");
  await title.focus();
  await page.keyboard.type("Mobile incident");
  await expect(title).toHaveValue("Mobile incident");
  const create = page.getByRole("button", { name: "Create incident" });
  await create.scrollIntoViewIfNeeded();
  await expect(create).toBeInViewport();
  await expect(page.locator("body")).toHaveJSProperty("scrollWidth", 375);
});

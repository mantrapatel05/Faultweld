import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { incidentSummary, problem, renderAt, statusLabel, stubFetch } from "./support";

const INDEX_PATH = "/api/incidents";

function stubIndex(body: unknown, status = 200, delayMs?: number) {
  return stubFetch({ [INDEX_PATH]: { body, status, delayMs } });
}

describe("overview route", () => {
  it("shows a loading panel before the incident index resolves", () => {
    stubIndex({ items: [], next_cursor: null }, 200, 0);
    renderAt("/overview");
    expect(screen.getByRole("heading", { name: "Loading local state" })).toBeInTheDocument();
    expect(screen.getByText("Checking the incident index.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the empty state and an available scenario launcher", async () => {
    stubIndex({ items: [], next_cursor: null });
    renderAt("/overview");
    expect(await screen.findByRole("heading", { name: "No incidents yet" })).toBeInTheDocument();
    expect(screen.getByText("00 records")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run scenario" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("lists incidents with status and updated time when records exist", async () => {
    stubIndex({
      items: [
        incidentSummary(),
        incidentSummary({ incident_id: "inc-2", scenario_id: "readiness-regression", status: "action_proposed" })
      ],
      next_cursor: null
    });
    renderAt("/overview");

    const list = await screen.findByRole("list", { name: "Incidents" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("inc-1")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("healthy-replica-loss")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("open")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText(statusLabel("action_proposed"))).toBeInTheDocument();
    expect(screen.getByText("02 records")).toBeInTheDocument();
    expect(list.querySelectorAll("time")).toHaveLength(2);
  });

  it("links each incident row to its workspace route", async () => {
    stubIndex({ items: [incidentSummary()], next_cursor: null });
    renderAt("/overview");
    expect(await screen.findByRole("link", { name: /inc-1/ })).toHaveAttribute("href", "/incidents/inc-1");
  });

  it("percent-encodes incident identifiers in workspace links", async () => {
    stubIndex({ items: [incidentSummary({ incident_id: "inc 1/2" })], next_cursor: null });
    renderAt("/overview");
    expect(await screen.findByRole("link", { name: /inc 1\/2/ })).toHaveAttribute("href", "/incidents/inc%201%2F2");
  });

  it("requests the documented index path with a page size", async () => {
    const fetchStub = stubIndex({ items: [], next_cursor: null });
    renderAt("/overview");
    await screen.findByRole("heading", { name: "No incidents yet" });
    await waitFor(() => expect(fetchStub.calls).toHaveLength(1));
    expect(fetchStub.calls[0]).toBe("/api/incidents?limit=20");
  });

  it("renders problem details and an alert region when the API is unavailable", async () => {
    const details = problem({ instance: INDEX_PATH });
    stubFetch({ [INDEX_PATH]: { status: details.status, body: details, contentType: "application/problem+json" } });
    renderAt("/overview");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("heading", { name: "The incident index is unavailable" })).toBeInTheDocument();
    expect(within(alert).getByText(details.detail)).toBeInTheDocument();
    expect(screen.getByText("API unavailable", { exact: true })).toBeInTheDocument();
  });

  it("shows the next-page control only when the API returns a cursor", async () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [incidentSummary()], next_cursor: "next-page" } } });
    renderAt("/overview");
    expect(await screen.findByRole("button", { name: "Load more incidents" })).toBeInTheDocument();
  });

  it("does not promote unverified status text into a recovery headline", async () => {
    stubIndex({ items: [incidentSummary({ status: "unknown" })], next_cursor: null });
    renderAt("/overview");
    await screen.findByRole("list", { name: "Incidents" });
    const headings = screen.getAllByRole("heading").map((heading) => heading.textContent ?? "");
    expect(headings.some((text) => /recovered|resolved|fixed/i.test(text))).toBe(false);
  });

  it("allows the user to choose a scenario and script before running", async () => {
    const user = userEvent.setup();
    stubFetch({ [INDEX_PATH]: { body: { items: [], next_cursor: null } } });
    renderAt("/overview");
    await screen.findByRole("heading", { name: "No incidents yet" });
    await user.selectOptions(screen.getByLabelText("Scenario"), "readiness-regression");
    await user.selectOptions(screen.getByLabelText("Investigation script"), "restart");
    expect(screen.getByLabelText("Scenario")).toHaveValue("readiness-regression");
    expect(screen.getByLabelText("Investigation script")).toHaveValue("restart");
  });
});

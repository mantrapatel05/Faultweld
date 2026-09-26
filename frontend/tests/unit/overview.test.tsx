import { screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { incidentSummary, problem, renderAt, statusLabel, stubFetch } from "./support";

const INDEX_PATH = "/api/incidents";

function stubIndex(body: unknown, status = 200, delayMs?: number) {
  return stubFetch({ [INDEX_PATH]: { body, status, delayMs } });
}

describe("overview route", () => {
  it("shows a loading panel before the incident index resolves", () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [] }, delayMs: 0 } });

    renderAt("/overview");

    expect(screen.getByRole("heading", { name: "Loading local state" })).toBeInTheDocument();
    expect(screen.getByText("Checking the read-only incident index.")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the empty state when the API returns no incidents", async () => {
    stubIndex({ items: [], next_cursor: null });

    renderAt("/overview");

    expect(await screen.findByRole("heading", { name: "No incidents yet" })).toBeInTheDocument();
    expect(screen.getByText("00 records")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("Local read-only")).toBeInTheDocument();
  });

  it("lists incidents with status and updated time when records exist", async () => {
    stubIndex({
      items: [
        incidentSummary(),
        incidentSummary({ incident_id: "inc-2", scenario_id: "bad-image", status: "action_proposed" })
      ],
      next_cursor: null
    });

    renderAt("/overview");

    const list = await screen.findByRole("list", { name: "Incidents" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);

    expect(within(rows[0] as HTMLElement).getByText("inc-1")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("cache-pressure")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("open")).toBeInTheDocument();

    expect(within(rows[1] as HTMLElement).getByText(statusLabel("action_proposed"))).toBeInTheDocument();
    expect(screen.getByText("02 records")).toBeInTheDocument();

    const timestamps = list.querySelectorAll("time");
    expect(timestamps).toHaveLength(2);
    expect(timestamps[0]).toHaveAttribute("datetime", "2026-02-01T09:30:00Z");
    expect(timestamps[1]).toHaveAttribute("datetime", "2026-02-01T09:30:00Z");
  });

  it("links each incident row to its workspace route", async () => {
    stubIndex({ items: [incidentSummary()], next_cursor: null });

    renderAt("/overview");

    const link = await screen.findByRole("link", { name: /inc-1/ });
    expect(link).toHaveAttribute("href", "/incidents/inc-1");
  });

  it("percent-encodes incident identifiers in workspace links", async () => {
    stubIndex({ items: [incidentSummary({ incident_id: "inc 1/2" })], next_cursor: null });

    renderAt("/overview");

    const link = await screen.findByRole("link", { name: /inc 1\/2/ });
    expect(link).toHaveAttribute("href", "/incidents/inc%201%2F2");
  });

  it("requests the documented index path with a JSON Accept header", async () => {
    const fetchStub = stubIndex({ items: [], next_cursor: null });

    renderAt("/overview");

    await screen.findByRole("heading", { name: "No incidents yet" });
    await waitFor(() => {
      expect(fetchStub.calls).toHaveLength(1);
    });
    expect(fetchStub.calls[0]).toBe("http://127.0.0.1:8000/api/incidents");
  });

  it("renders the problem detail and alert role when the API is unavailable", async () => {
    const details = problem({ instance: INDEX_PATH });
    stubFetch({
      [INDEX_PATH]: { status: details.status, body: details, contentType: "application/problem+json" }
    });

    renderAt("/overview");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByRole("heading", { name: "The incident index is unavailable" })).toBeInTheDocument();
    expect(within(alert).getByText(details.detail)).toBeInTheDocument();
    expect(screen.getByText("Disconnected", { exact: true })).toBeInTheDocument();
    expect(
      screen.getByText("The workspace remains disconnected. No incident state is being inferred from the browser.")
    ).toBeInTheDocument();
  });

  it("renders a generic message when the failure carries no problem document", async () => {
    stubFetch({ [INDEX_PATH]: { status: 500, body: "upstream failure" } });

    renderAt("/overview");

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("The API returned HTTP 500.")).toBeInTheDocument();
  });

  it("never presents a recovered status that the read-only API cannot prove", async () => {
    stubIndex({
      items: [incidentSummary({ incident_id: "inc-9", status: "recovered" })],
      next_cursor: null
    });

    renderAt("/overview");

    expect(await screen.findByRole("list", { name: "Incidents" })).toBeInTheDocument();
    const headings = screen.getAllByRole("heading").map((heading) => heading.textContent ?? "");
    expect(headings.some((text) => /recovered|resolved|fixed/i.test(text))).toBe(false);
  });
});

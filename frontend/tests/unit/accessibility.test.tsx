import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { incidentDetail, incidentSummary, problem, proposalDetail, renderAt, stubFetch } from "./support";

const INDEX_PATH = "/api/incidents";
const DETAIL_PATH = "/api/incidents/inc-1";
const PROPOSAL_PATH = "/api/proposals/proposal-1";

function headingLevel(heading: HTMLElement): number {
  return Number(heading.tagName.slice(1));
}

function expectSingleMainHeading(name: string | RegExp): void {
  const headings = within(screen.getByRole("main")).getAllByRole("heading", { level: 1 });
  expect(headings).toHaveLength(1);
  expect(headings[0]).toHaveAccessibleName(name);
}

function expectLandmarks(): void {
  expect(screen.getByRole("banner")).toBeInTheDocument();
  expect(screen.getByRole("navigation", { name: "Primary navigation" })).toBeInTheDocument();
  expect(screen.getByRole("main")).toBeInTheDocument();
  expect(screen.getByRole("complementary", { name: "Faultweld navigation" })).toBeInTheDocument();
  expect(screen.getByRole("contentinfo")).toBeInTheDocument();
}

function expectLabelledSections(): void {
  const labelled = Array.from(document.querySelectorAll("[aria-labelledby]"));
  expect(labelled.length).toBeGreaterThan(0);
  for (const element of labelled) {
    const id = element.getAttribute("aria-labelledby");
    expect(id).toBeTruthy();
    expect(document.getElementById(id ?? "")).not.toBeNull();
  }
}

function expectNoSkippedHeadingLevels(): void {
  const levels = within(screen.getByRole("main")).getAllByRole("heading").map(headingLevel);
  expect(levels[0]).toBe(1);
  for (let index = 1; index < levels.length; index += 1) {
    expect((levels[index] as number) - (levels[index - 1] as number)).toBeLessThanOrEqual(1);
  }
}

function expectNamedHeadings(): void {
  for (const heading of screen.getAllByRole("heading")) {
    expect((heading.textContent ?? "").trim().length).toBeGreaterThan(0);
  }
}

describe("accessibility contract", () => {
  it("exposes landmarks and one main heading on the overview", async () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [], next_cursor: null } } });
    renderAt("/overview");
    await screen.findByRole("heading", { name: "No incidents yet" });
    expectLandmarks();
    expectSingleMainHeading("Incident console");
    expectLabelledSections();
    expectNoSkippedHeadingLevels();
    expectNamedHeadings();
  });

  it("keeps the accessibility contract when incidents are listed", async () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [incidentSummary()], next_cursor: null } } });
    renderAt("/overview");
    await screen.findByRole("list", { name: "Incidents" });
    expectSingleMainHeading("Incident console");
    expectNoSkippedHeadingLevels();
    expectNamedHeadings();
  });

  it("keeps the accessibility contract on a populated incident workspace", async () => {
    stubFetch({
      [DETAIL_PATH]: { body: incidentDetail({ proposal_ids: ["proposal-1"] }) },
      [PROPOSAL_PATH]: { body: proposalDetail() }
    });
    renderAt("/incidents/inc-1");
    await screen.findByRole("heading", { name: "Action proposal" });
    expectLandmarks();
    expectSingleMainHeading("inc-1");
    expectLabelledSections();
    expectNoSkippedHeadingLevels();
    expectNamedHeadings();
  });

  it("keeps the accessibility contract on the not-found route", () => {
    stubFetch({});
    renderAt("/not-a-real-route");
    expectLandmarks();
    expectSingleMainHeading("That surface is not wired.");
    expectNoSkippedHeadingLevels();
    expectNamedHeadings();
  });

  it("announces error states through a live alert region", async () => {
    stubFetch({ [INDEX_PATH]: { status: 503, body: problem(), contentType: "application/problem+json" } });
    renderAt("/overview");
    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toHaveTextContent("The incident index is unavailable");
  });

  it("marks decorative elements as hidden from assistive technology", async () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [incidentSummary()], next_cursor: null } } });
    renderAt("/overview");
    await screen.findByRole("list", { name: "Incidents" });
    const decorative = document.querySelectorAll(".brand-mark, .pulse-dot, .state-dot, .row-arrow, .principle-index, .state-mark");
    expect(decorative.length).toBeGreaterThan(0);
    for (const element of decorative) expect(element).toHaveAttribute("aria-hidden", "true");
  });
});

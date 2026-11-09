import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { renderAt, stubFetch } from "./support";

const INDEX_PATH = "/api/incidents";

function primaryNav(): HTMLElement {
  return screen.getByRole("navigation", { name: "Primary navigation" });
}

describe("application routing", () => {
  it("redirects the index route to the overview", async () => {
    stubFetch({ [INDEX_PATH]: { body: { items: [], next_cursor: null } } });
    renderAt("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Incident console" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/overview");
  });

  it("renders a not-found surface for an unknown route", () => {
    stubFetch({});
    renderAt("/not-a-real-route");
    expect(screen.getByRole("heading", { level: 1, name: "That surface is not wired." })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to overview" })).toHaveAttribute("href", "/overview");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reaches every navigation target with the keyboard alone", async () => {
    const user = userEvent.setup();
    stubFetch({ [INDEX_PATH]: { body: { items: [], next_cursor: null } } });
    renderAt("/overview");

    const brand = screen.getByRole("link", { name: "Faultweld overview" });
    const overview = within(primaryNav()).getByRole("link", { name: /Overview/ });
    const workspace = within(primaryNav()).getByRole("link", { name: /Incident workspace/ });

    await user.tab();
    expect(document.activeElement).toBe(brand);
    await user.tab();
    expect(document.activeElement).toBe(overview);
    await user.tab();
    expect(document.activeElement).toBe(workspace);
    await user.keyboard("{Enter}");
    expect(window.location.pathname).toBe("/incidents");
    expect(screen.getByRole("heading", { level: 1, name: "No incident selected" })).toBeInTheDocument();
  });

  it("marks the current route in the navigation for assistive technology", () => {
    stubFetch({});
    renderAt("/incidents");
    const workspace = within(primaryNav()).getByRole("link", { name: /Incident workspace/ });
    expect(workspace).toHaveClass("is-active");
    expect(workspace).toHaveAttribute("aria-current", "page");
    expect(within(primaryNav()).getByRole("link", { name: /Overview/ })).not.toHaveClass("is-active");
  });
});

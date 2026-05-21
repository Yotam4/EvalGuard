import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { Tabs, type TabSpec } from "../Tabs";


type T = "all" | "cli" | "otlp";

function makeTabs(): TabSpec<T>[] {
  return [
    { value: "all",  label: "All",         dataAttr: "all"  },
    { value: "cli",  label: "CLI push",    dataAttr: "cli"  },
    { value: "otlp", label: "OTLP traces", dataAttr: "otlp" },
  ];
}


describe("<Tabs>", () => {
  it("renders a tablist with one tab per spec", () => {
    render(
      <Tabs<T>
        value="all"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    expect(screen.getByRole("tablist", { name: /filter by source/i }))
      .toBeInTheDocument();
    expect(screen.getAllByRole("tab")).toHaveLength(3);
  });

  it("uses aria-selected (not aria-pressed) on the active tab", () => {
    // The audit specifically flagged the previous ``aria-pressed``
    // semantic as wrong for tabs — pin the new shape so a refactor
    // can't regress.
    render(
      <Tabs<T>
        value="cli"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const cli  = screen.getByRole("tab", { name: /cli push/i });
    const otlp = screen.getByRole("tab", { name: /otlp traces/i });
    expect(cli.getAttribute("aria-selected")).toBe("true");
    expect(otlp.getAttribute("aria-selected")).toBe("false");
    // aria-pressed must NOT appear — it would confuse screen
    // readers into announcing the tabs as toggle buttons.
    expect(cli.hasAttribute("aria-pressed")).toBe(false);
    expect(otlp.hasAttribute("aria-pressed")).toBe(false);
  });

  it("uses roving tabindex: active tab is 0, others are -1", () => {
    // ARIA Authoring Practices pattern — the inactive tabs are
    // taken out of the page's tab order so Tab key moves past the
    // whole strip in one step.  Arrow keys move within it.
    render(
      <Tabs<T>
        value="otlp"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.getAttribute("tabindex"))).toEqual([
      "-1", "-1", "0",   // only the active OTLP tab is reachable via Tab
    ]);
  });

  it("calls onChange when a tab is clicked", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="all"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    fireEvent.click(screen.getByRole("tab", { name: /otlp traces/i }));
    expect(onChange).toHaveBeenCalledWith("otlp");
  });

  it("ArrowRight moves selection to the next tab (with wrap)", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="cli"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    // ArrowRight on the tablist — should advance from ``cli`` (idx 1)
    // to ``otlp`` (idx 2).
    fireEvent.keyDown(
      screen.getByRole("tablist"),
      { key: "ArrowRight" },
    );
    expect(onChange).toHaveBeenLastCalledWith("otlp");
  });

  it("ArrowLeft wraps from the first tab to the last", () => {
    // Wrap-around is the standard tab-pattern behaviour and prevents
    // the focus getting "stuck" at an edge.
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="all"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    fireEvent.keyDown(
      screen.getByRole("tablist"),
      { key: "ArrowLeft" },
    );
    expect(onChange).toHaveBeenLastCalledWith("otlp");
  });

  it("Home / End jump to the first / last tab", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="cli"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const tablist = screen.getByRole("tablist");
    fireEvent.keyDown(tablist, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("otlp");
    fireEvent.keyDown(tablist, { key: "Home" });
    expect(onChange).toHaveBeenLastCalledWith("all");
  });

  it("emits the data-* attribute named by dataAttrName", () => {
    render(
      <Tabs<T>
        value="all"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
        dataAttrName="source-tab"
      />,
    );
    // Pin the exact attribute name so Playwright selectors keep
    // working (the /runs/ spec addresses tabs via
    // ``[data-source-tab="cli"]``).
    expect(
      document.querySelector('[data-source-tab="cli"]'),
    ).not.toBeNull();
    expect(
      document.querySelector('[data-source-tab="otlp"]'),
    ).not.toBeNull();
  });

  it("does NOT emit the data-* attribute when dataAttrName is unset", () => {
    render(
      <Tabs<T>
        value="all"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    // ``dataAttrName`` is optional — when the parent doesn't pass
    // one, no per-button attribute should land.
    expect(
      document.querySelector('[data-source-tab]'),
    ).toBeNull();
  });
});

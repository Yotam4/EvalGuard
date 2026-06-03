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


describe("<Tabs> — radiogroup (default, used for filters)", () => {
  // Round-8 review-pass: the default variant flipped from "tabs" to
  // "radiogroup" because every in-repo caller (/runs source filter,
  // /assets kind filter) is a single-select filter, NOT a tab strip
  // revealing a tabpanel.  ``role="tab"`` without an ``aria-controls``
  // panel made screen readers announce "tab, 1 of N" and look for a
  // region that doesn't exist.

  it("renders a radiogroup with one radio per spec", () => {
    render(
      <Tabs<T>
        value="all"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    expect(screen.getByRole("radiogroup", { name: /filter by source/i }))
      .toBeInTheDocument();
    expect(screen.getAllByRole("radio")).toHaveLength(3);
  });

  it("uses aria-checked (not aria-selected, not aria-pressed) on the active item", () => {
    render(
      <Tabs<T>
        value="cli"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const cli  = screen.getByRole("radio", { name: /cli push/i });
    const otlp = screen.getByRole("radio", { name: /otlp traces/i });
    expect(cli.getAttribute("aria-checked")).toBe("true");
    expect(otlp.getAttribute("aria-checked")).toBe("false");
    // The legacy attributes must NOT appear — they would confuse
    // screen readers into announcing the wrong pattern.
    expect(cli.hasAttribute("aria-pressed")).toBe(false);
    expect(cli.hasAttribute("aria-selected")).toBe(false);
  });

  it("uses roving tabindex: active item is 0, others are -1", () => {
    render(
      <Tabs<T>
        value="otlp"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const radios = screen.getAllByRole("radio");
    expect(radios.map((t) => t.getAttribute("tabindex"))).toEqual([
      "-1", "-1", "0",   // only the active OTLP option is reachable via Tab
    ]);
  });

  it("calls onChange when a radio is clicked", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="all"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    fireEvent.click(screen.getByRole("radio", { name: /otlp traces/i }));
    expect(onChange).toHaveBeenCalledWith("otlp");
  });

  it("ArrowRight moves selection to the next item (with wrap)", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="cli"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    fireEvent.keyDown(
      screen.getByRole("radiogroup"),
      { key: "ArrowRight" },
    );
    expect(onChange).toHaveBeenLastCalledWith("otlp");
  });

  it("ArrowLeft wraps from the first item to the last", () => {
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
      screen.getByRole("radiogroup"),
      { key: "ArrowLeft" },
    );
    expect(onChange).toHaveBeenLastCalledWith("otlp");
  });

  it("Home / End jump to the first / last item", () => {
    const onChange = vi.fn();
    render(
      <Tabs<T>
        value="cli"
        onChange={onChange}
        tabs={makeTabs()}
        ariaLabel="Filter by source"
      />,
    );
    const group = screen.getByRole("radiogroup");
    fireEvent.keyDown(group, { key: "End" });
    expect(onChange).toHaveBeenLastCalledWith("otlp");
    fireEvent.keyDown(group, { key: "Home" });
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
    expect(
      document.querySelector('[data-source-tab]'),
    ).toBeNull();
  });
});


describe("<Tabs variant=\"tabs\"> — real tabs (reveals a tabpanel)", () => {
  it("renders tablist + tabs with aria-selected when variant=tabs", () => {
    // Pin the explicit-opt-in tablist semantics so a future caller
    // wiring up a panel-revealing tab strip gets the right ARIA.
    render(
      <Tabs<T>
        value="cli"
        onChange={() => {}}
        tabs={makeTabs()}
        ariaLabel="Pick a tab"
        variant="tabs"
      />,
    );
    expect(screen.getByRole("tablist", { name: /pick a tab/i }))
      .toBeInTheDocument();
    const tabs = screen.getAllByRole("tab");
    expect(tabs).toHaveLength(3);
    const cli = screen.getByRole("tab", { name: /cli push/i });
    expect(cli.getAttribute("aria-selected")).toBe("true");
    expect(cli.hasAttribute("aria-checked")).toBe(false);
  });
});

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { GoldenRowPreview, formatValue } from "../GoldenRowPreview";


describe("formatValue", () => {
  it("passes strings through", () => {
    expect(formatValue("hello")).toBe("hello");
  });
  it("dashes null + undefined", () => {
    expect(formatValue(null)).toBe("—");
    expect(formatValue(undefined)).toBe("—");
  });
  it("JSON-stringifies objects + arrays", () => {
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
    expect(formatValue(["x", "y"])).toBe('["x","y"]');
  });
});


describe("<GoldenRowPreview>", () => {
  it("renders input / expected / output fields", () => {
    render(<GoldenRowPreview rowData={{
      input: "What is X?", expected: "X is …", output: "X is foo",
    }} />);
    const preview = screen.getByTestId("golden-preview");
    expect(preview).toBeInTheDocument();
    expect(preview.querySelector('[data-field="input"]')?.textContent).toBe("What is X?");
    expect(preview.querySelector('[data-field="expected"]')?.textContent).toBe("X is …");
    expect(preview.querySelector('[data-field="output"]')?.textContent).toBe("X is foo");
  });

  it("renders structured input as JSON", () => {
    render(<GoldenRowPreview rowData={{ input: { contexts: ["a", "b"] } }} />);
    expect(
      screen.getByTestId("golden-preview").querySelector('[data-field="input"]')?.textContent,
    ).toBe('{"contexts":["a","b"]}');
  });

  it("shows the unavailable message when row_data is null", () => {
    render(<GoldenRowPreview rowData={null} />);
    expect(screen.getByTestId("golden-preview-unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("golden-preview")).not.toBeInTheDocument();
  });

  it("dashes an absent expected/output without crashing", () => {
    render(<GoldenRowPreview rowData={{ input: "hi" }} />);
    const preview = screen.getByTestId("golden-preview");
    expect(preview.querySelector('[data-field="expected"]')?.textContent).toBe("—");
    expect(preview.querySelector('[data-field="output"]')?.textContent).toBe("—");
  });
});

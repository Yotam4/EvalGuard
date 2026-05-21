import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { PromoteButton } from "../PromoteButton";
import { setServerUrl, setToken } from "../../lib/auth";


function withQueryClient(ui: React.ReactNode) {
  // ``retry: false`` so a 4xx in the test doesn't trigger the
  // default one retry and double-count the fetch.
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>{ui}</QueryClientProvider>
  );
}


describe("<PromoteButton>", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    window.localStorage.clear();
    setServerUrl("https://api.example.com");
    setToken("evk_x");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });


  it("renders the idle 'Promote to golden' label by default", () => {
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    expect(
      screen.getByTestId("promote-button").textContent,
    ).toMatch(/promote to golden/i);
  });


  it("clicking the button reveals the note form (not the immediate POST)", () => {
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    // The form takes over; the trigger button is gone.
    expect(screen.queryByTestId("promote-button")).not.toBeInTheDocument();
    expect(screen.getByTestId("promote-form")).toBeInTheDocument();
    // Note field + Save / cancel both present.
    expect(screen.getByPlaceholderText(/optional note/i)).toBeInTheDocument();
    expect(screen.getByTestId("promote-confirm")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /cancel/i })).toBeInTheDocument();
    // Importantly: the POST has NOT fired yet.  The click only
    // opened the form.
    expect(fetchMock).not.toHaveBeenCalled();
  });


  it("submitting POSTs run_id + row_id + trimmed note", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 1, run_id: "run_a", row_id: "r-1",
        project_id: "proj", promoted_by: "key_a",
        note: "kept", created_at: "2026-05-15T07:30:00",
      }),
    });
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.change(
      screen.getByPlaceholderText(/optional note/i),
      { target: { value: "  kept  " } },
    );
    fireEvent.click(screen.getByTestId("promote-confirm"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.example.com/v1/golden/candidates");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(body).toEqual({ run_id: "run_a", row_id: "r-1", note: "kept" });
  });


  it("submit with empty note omits the field from the body", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 1, run_id: "run_a", row_id: "r-1",
        project_id: "proj", promoted_by: "key_a",
        note: null, created_at: "2026-05-15T07:30:00",
      }),
    });
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.click(screen.getByTestId("promote-confirm"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    // ``note`` not present at all — the server reads "no note"
    // rather than "empty string note", which round-trips cleanly
    // through the server's empty-string-to-NULL normalisation.
    expect(body).toEqual({ run_id: "run_a", row_id: "r-1" });
  });


  it("post-success label flips to ✓ Promoted and idempotently re-opens", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 1, run_id: "run_a", row_id: "r-1",
        project_id: "proj", promoted_by: "key_a",
        note: null, created_at: "2026-05-15T07:30:00",
      }),
    });
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.click(screen.getByTestId("promote-confirm"));
    // Once the mutation resolves, the form collapses and the
    // button shows the success label.
    await waitFor(() =>
      expect(screen.getByTestId("promote-button").textContent)
        .toMatch(/promoted/i),
    );
  });


  it("cancel returns to the idle button without firing a request", () => {
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByTestId("promote-button")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });


  it("consumer-side `key` resets the success label when (runId, rowId) changes", async () => {
    // The call-detail panel passes ``key={`${runId}:${rowId}`}`` so
    // navigating between calls remounts the button cleanly — the
    // previously-promoted row's ``✓ Promoted`` label MUST NOT carry
    // over.  This test simulates that exact consumer pattern.
    fetchMock.mockResolvedValueOnce({
      ok: true, status: 201,
      json: async () => ({
        id: 7, run_id: "run_a", row_id: "r-1",
        project_id: "proj", promoted_by: "key_a",
        note: null, created_at: "2026-05-15T07:30:00",
      }),
    });
    // Use a single QueryClient across rerenders so the mutation
    // state would otherwise persist — which makes the ``key``
    // remount the real fix being tested.
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const Wrap = ({ rowId }: { rowId: string }) => (
      <QueryClientProvider client={client}>
        <PromoteButton key={`run_a:${rowId}`} runId="run_a" rowId={rowId} />
      </QueryClientProvider>
    );
    const { rerender } = render(<Wrap rowId="r-1" />);
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.click(screen.getByTestId("promote-confirm"));
    await waitFor(() =>
      expect(screen.getByTestId("promote-button").textContent).toMatch(/promoted/i),
    );
    // Same QueryClient, different ``rowId`` — the ``key`` forces
    // unmount + remount, so the new button is back at idle.
    rerender(<Wrap rowId="r-2" />);
    expect(screen.getByTestId("promote-button").textContent).toMatch(/promote to golden/i);
    expect(screen.getByTestId("promote-button").textContent).not.toMatch(/promoted/i);
  });


  it("surfaces server errors inline (4xx → red banner, idle button still visible)", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false, status: 404, statusText: "Not Found",
      json: async () => ({ detail: "Run not found." }),
    });
    render(withQueryClient(
      <PromoteButton runId="run_a" rowId="r-1" />,
    ));
    fireEvent.click(screen.getByTestId("promote-button"));
    fireEvent.click(screen.getByTestId("promote-confirm"));
    await waitFor(() => expect(
      screen.getByTestId("promote-error").textContent,
    ).toMatch(/not found/i));
    // The form stays open so the operator can adjust + retry.
    expect(screen.getByTestId("promote-form")).toBeInTheDocument();
  });
});

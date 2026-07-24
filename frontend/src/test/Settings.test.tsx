import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Settings from "../pages/Settings";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: apiMock };
});

const loadResponses: Record<string, unknown> = {
  "/settings/models": {
    functions: { correct: { provider: "ollama", model: "qwen3:8b" } },
    providers: ["ollama", "openai"],
    provider_options: { correct: ["ollama", "openai"] },
  },
  "/settings/provider-models": {
    ollama: { configured: true, ok: true, models: ["qwen3:8b"], detail: "" },
    openai: { configured: true, ok: true, models: ["gpt-5.4", "gpt-5-mini"], detail: "" },
  },
  "/settings/voices": { kokoro: {}, piper: {}, gemini: {} },
  "/settings/profiles": {},
  "/projects/steps": [],
  "/settings/search": {
    semantic_enabled: true,
    embedding_provider: "ollama",
    embedding_model: "qwen3:8b",
  },
  "/library/index/status": {
    chunks: 10,
    repository_chunks: 4,
    paper_chunks: 3,
    embeddings: 10,
    paper_embeddings: 2,
    semantic_enabled: true,
    embedding_model: "qwen3:8b",
  },
  "/settings/backup": {
    retention: 5,
    schedule_hours: 0,
    include_media: false,
    include_repositories: false,
  },
  "/repositories/credentials": { configured: false },
  "/repositories/settings": null,
  "/settings/glossary": { terms: [] },
  "/tags": [],
  "/settings/download": { max_height: 1080 },
  "/settings/prompts": {},
  "/settings/params": {},
  "/settings/advanced": { groups: {} },
  "/settings/cloud": {
    provider: "",
    providers: [],
    all_fields: {},
    config: {},
    remote_base: "",
    auto: false,
    mode: "push",
    last_sync: null,
  },
  "/quickrefs/categories": [],
};

beforeEach(() => {
  apiMock.mockReset();
  apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
    if (options) return { ok: true };
    if (path in loadResponses) return loadResponses[path];
    throw new Error(`unexpected API request: ${path}`);
  });
});

describe("Settings integration", () => {
  it("waits for a valid model choice before saving a changed provider", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    const [functionCell] = await screen.findAllByText("Transcript correction");
    const row = functionCell.closest("tr");
    expect(row).not.toBeNull();

    let selectors = within(row!).getAllByRole("combobox");
    await user.selectOptions(selectors[0], "openai");

    selectors = within(row!).getAllByRole("combobox");
    expect(selectors[0]).toHaveValue("openai");
    expect(selectors[1]).toHaveValue("");
    expect(within(row!).getByRole("option", { name: "choose a model…" })).toBeDisabled();
    expect(apiMock.mock.calls.some(([path, options]) =>
      path === "/settings/models/correct" && options?.method === "PUT")).toBe(false);

    await user.selectOptions(selectors[1], "gpt-5.4");

    await waitFor(() => {
      const saveCall = apiMock.mock.calls.find(([path, options]) =>
        path === "/settings/models/correct" && options?.method === "PUT");
      expect(saveCall).toBeDefined();
      expect(JSON.parse(saveCall![1].body as string)).toEqual({
        provider: "openai",
        model: "gpt-5.4",
      });
    });
  });

  it("shows paper and repository index coverage and includes papers in pending status", async () => {
    render(<Settings />);

    const status = await screen.findByText(/Artifacts: 10 chunks/);
    expect(status).toHaveTextContent("Repositories: 4 evidence chunks");
    expect(status).toHaveTextContent("Papers: 3 evidence chunks / 2 embedded");
    expect(status).toHaveTextContent("rebuild pending or incomplete");
  });
});

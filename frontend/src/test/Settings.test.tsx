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
  "/settings/ollama/models": {
    configured: true,
    ok: true,
    local: true,
    detail: "",
    resources: {
      available: true,
      reason: "",
      ram_total_bytes: 16 * 1024 ** 3,
      ram_available_bytes: 10 * 1024 ** 3,
      vram_total_bytes: 8 * 1024 ** 3,
      vram_free_bytes: 6 * 1024 ** 3,
    },
    models: [
      {
        name: "qwen3:8b",
        digest: "sha256:qwen",
        size_bytes: 5 * 1024 ** 3,
        details: {
          family: "qwen3",
          families: ["qwen3"],
          parameter_size: "8.2B",
          quantization_level: "Q4_K_M",
        },
        capabilities: ["completion", "thinking"],
        native_context_tokens: 131072,
        annotation: { label: "Daily driver", notes: "", labels: ["general"] },
        benchmark: {
          prompt_version: "1",
          completion: true,
          structured_json: true,
          checked_at: "2026-07-26T12:00:00",
        },
        residency: {
          loaded: true,
          size_bytes: 6 * 1024 ** 3,
          size_vram_bytes: 5 * 1024 ** 3,
          context_length: 16384,
          expires_at: "2026-07-26T12:10:00Z",
          processor: "hybrid",
        },
        assessment: {
          tier: "recommended",
          message: "Expected to fit with a GPU safety reserve.",
          requested_context_tokens: 16384,
          estimated_weight_bytes: 5 * 1024 ** 3,
          estimated_context_bytes: 512 * 1024 ** 2,
          estimated_total_bytes: 5.5 * 1024 ** 3,
          acknowledged: false,
        },
      },
      {
        name: "deepseek-r1:671b",
        digest: "sha256:deepseek",
        size_bytes: 404 * 1024 ** 3,
        details: {
          family: "deepseek2",
          families: ["deepseek2"],
          parameter_size: "671B",
          quantization_level: "Q4_K_M",
        },
        capabilities: ["completion", "thinking"],
        native_context_tokens: 131072,
        annotation: { label: "", notes: "", labels: [] },
        benchmark: null,
        assessment: {
          tier: "blocked",
          message: "Estimated memory exceeds the safe combined budget.",
          requested_context_tokens: 16384,
          estimated_weight_bytes: 444 * 1024 ** 3,
          estimated_context_bytes: 32 * 1024 ** 3,
          estimated_total_bytes: 476 * 1024 ** 3,
          acknowledged: false,
        },
      },
    ],
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
    if (path === "/settings/ollama/models?refresh=true") {
      return loadResponses["/settings/ollama/models"];
    }
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

  it("discloses that backups do not hydrate cloud-primary media", async () => {
    render(<Settings />);

    const disclosure = await screen.findByText(
      /Cloud-primary media bytes are not downloaded into a backup/,
    );
    expect(disclosure).toHaveTextContent(
      /backup remains dependent on that configured remote and valid credentials/,
    );
    expect(disclosure).toHaveTextContent(
      /Restore cloud-only media locally.*self-contained media archive/,
    );
  });

  it("shows digest-specific Ollama labels, capabilities, fit, and benchmark results", async () => {
    render(<Settings />);

    const heading = await screen.findByRole("heading", { name: "Daily driver" });
    const card = heading.closest("article");
    expect(card).not.toBeNull();
    expect(within(card!).getByText("Recommended")).toBeInTheDocument();
    expect(within(card!).getByText("thinking")).toBeInTheDocument();
    expect(within(card!).getByText("Dense-context candidate")).toBeInTheDocument();
    expect(within(card!).getByText("Structured output verified")).toBeInTheDocument();
    expect(within(card!).getByText(/Loaded hybrid/)).toBeInTheDocument();
    expect(within(card!).getByText(/structured JSON passed/)).toBeInTheDocument();
    expect(screen.getByText(/6.0 GiB free VRAM/)).toBeInTheDocument();
  });

  it("releases only the selected resident Ollama model", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    const heading = await screen.findByRole("heading", { name: "Daily driver" });
    const card = heading.closest("article");
    await user.click(within(card!).getByRole("button", { name: "Unload from memory" }));

    await waitFor(() => {
      const request = apiMock.mock.calls.find(([path, options]) =>
        path === "/settings/ollama/unload" && options?.method === "POST");
      expect(JSON.parse(request![1].body as string)).toEqual({ model: "qwen3:8b" });
    });
  });

  it("requires the full model name and an administrative reason before overriding a block", async () => {
    const user = userEvent.setup();
    render(<Settings />);

    const modelHeading = await screen.findByRole("heading", { name: "deepseek-r1:671b" });
    const card = modelHeading.closest("article");
    expect(card).not.toBeNull();
    await user.click(within(card!).getByText("Administrator override"));

    const button = within(card!).getByRole("button", { name: "Record administrator override" });
    expect(button).toBeDisabled();
    await user.type(within(card!).getByLabelText(/Type deepseek-r1:671b/), "deepseek-r1:671b");
    await user.type(within(card!).getByLabelText("Administrative reason"), "Temporary controlled evaluation");
    expect(button).toBeEnabled();
    await user.click(button);

    await waitFor(() => {
      const request = apiMock.mock.calls.find(([path, options]) =>
        path === "/settings/ollama/acknowledge" && options?.method === "POST");
      expect(JSON.parse(request![1].body as string)).toEqual({
        model: "deepseek-r1:671b",
        digest: "sha256:deepseek",
        confirmation: "deepseek-r1:671b",
        reason: "Temporary controlled evaluation",
      });
    });
  });
});

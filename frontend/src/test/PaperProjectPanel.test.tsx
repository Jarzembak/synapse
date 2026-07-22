import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, PaperDetail, Project } from "../api";
import PaperProjectPanel from "../components/PaperProjectPanel";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: vi.fn() };
});

vi.mock("../useEventSource", () => ({ useEventSource: vi.fn() }));

const project: Project = {
  id: 7,
  slug: "dense-paper",
  title: "Dense Paper",
  source: "dense-paper.pdf",
  source_type: "paper",
  status: "ready",
  created: "2026-07-21T12:00:00",
};

const detail: PaperDetail = {
  project,
  source: {
    project_id: project.id,
    original_filename: "dense-paper.pdf",
    page_count: 48,
    extracted_characters: 123456,
    source_hash: "abcdef0123456789abcdef",
    parser_version: "docling-test",
    ocr_languages: ["eng"],
    local_only: true,
    privacy_locked: true,
    analysis_blocked: true,
  },
  quality: {
    grade: "POOR",
    blocked: true,
    page_issues: [
      { page: 2, grade: "POOR", reason: "Unreadable methods column" },
      { page: 5, grade: "POOR", reason: "Low-confidence formula", acknowledged: true, acknowledgement_reason: "Verified against the source" },
    ],
    acknowledged_pages: [{ page: 5, acknowledgement_reason: "Verified against the source" }],
  },
  coverage: {
    evidence_blocks: 120,
    mapped_blocks: 80,
    pages_total: 48,
    pages_admitted: 46,
  },
  series: [
    {
      id: 71,
      project_id: project.id,
      audience: "generalist",
      title: "Generalist foundations",
      status: "draft",
      target_minutes: 50,
      parts: [{ id: 711, position: 1, title: "Why it matters", focus: "Build intuition" }],
    },
    {
      id: 73,
      project_id: project.id,
      audience: "expert",
      title: "Expert methodology",
      status: "approved",
      target_minutes: 55,
      parts: [
        { id: 731, position: 1, title: "Estimator", focus: "Derive the estimator" },
        { id: 732, position: 2, title: "Uncertainty", focus: "Audit uncertainty" },
      ],
    },
  ],
};

describe("PaperProjectPanel", () => {
  beforeEach(() => {
    vi.mocked(api).mockResolvedValue(detail);
  });

  it("keeps poor-page acknowledgement visible and audience tracks independent", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PaperProjectPanel project={project} onProjectReload={vi.fn()} onDelete={vi.fn()} />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Extraction review" })).toBeInTheDocument();
    expect(screen.getByText("Review required")).toBeInTheDocument();
    expect(screen.getByText("Unreadable methods column")).toBeInTheDocument();
    expect(screen.getByText("Acknowledged gap")).toHaveAttribute("title", "Verified against the source");

    await user.click(screen.getByRole("button", { name: "Review and acknowledge" }));
    expect(screen.getByRole("heading", { name: "Acknowledge page 2" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Reason" })).toBeInTheDocument();

    const generalistCard = screen.getByRole("heading", { name: "Generalist foundations" }).closest("article");
    const practitionerCard = screen.getByRole("heading", { name: "Practitioner series" }).closest("article");
    const expertCard = screen.getByRole("heading", { name: "Expert methodology" }).closest("article");
    expect(generalistCard).not.toBeNull();
    expect(practitionerCard).not.toBeNull();
    expect(expertCard).not.toBeNull();

    expect(within(generalistCard as HTMLElement).getByRole("link", { name: "Review plan" })).toHaveAttribute("href", "/paper-series/71");
    expect(within(generalistCard as HTMLElement).getByText("1 part")).toBeInTheDocument();
    expect(within(expertCard as HTMLElement).getByRole("link", { name: "Open series" })).toHaveAttribute("href", "/paper-series/73");
    expect(within(expertCard as HTMLElement).getByText("2 parts")).toBeInTheDocument();
    expect(within(practitionerCard as HTMLElement).getByRole("button", { name: "Draft Practitioner plan" })).toBeDisabled();
  });
});

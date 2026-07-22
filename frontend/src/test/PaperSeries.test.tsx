import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, Artifact, PaperSeriesPart, Project } from "../api";
import PaperSeries from "../pages/PaperSeries";

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: vi.fn() };
});

vi.mock("../useEventSource", () => ({ useEventSource: vi.fn() }));

const project: Project = {
  id: 9,
  slug: "continuity-paper",
  title: "Continuity Paper",
  source: "continuity.pdf",
  source_type: "paper",
  status: "ready",
  created: "2026-07-21T12:00:00",
};

const script: Artifact = {
  id: 501,
  project_id: project.id,
  type: "paper_part_script",
  title: "Part 1 script",
  path: "part-1-script.md",
  media_path: null,
  provider: "local",
  model: "test",
  paper_series_id: 91,
  paper_part_id: 911,
  created: "2026-07-21T12:30:00",
  updated: "2026-07-21T12:30:00",
};

const parts: PaperSeriesPart[] = [
  {
    id: 911,
    paper_series_id: 91,
    position: 1,
    title: "Foundations",
    focus: "Introduce the problem and estimator.",
    status: "complete",
    structure_locked: true,
    guide_status: "complete",
    script_status: "complete",
    audio_status: "complete",
    artifacts: [script],
  },
  {
    id: 912,
    paper_series_id: 91,
    position: 2,
    title: "Consequences",
    focus: "Connect results to practical consequences.",
    status: "partial",
    stale: true,
    guide_status: "complete",
    script_status: "stale",
    audio_status: "stale",
  },
];

const detail = {
  project,
  parts,
  artifacts: [script],
  jobs: [],
  series: {
    id: 91,
    project_id: project.id,
    audience: "practitioner" as const,
    title: "Practitioner continuity",
    status: "approved",
    target_minutes: 50,
    plan_version: 3,
    parts,
    user_guidance: "Keep examples grounded in deployment decisions.",
    memory_revision: {
      id: 601,
      paper_series_id: 91,
      paper_part_id: 911,
      revision: 2,
      created: "2026-07-21T12:31:00",
      state: {
        terminology: [{ term: "ELBO", pronunciation: "el-bo", meaning: "evidence lower bound" }],
        completed_topics: ["Estimator setup"],
        promised_callbacks: ["Return to calibration in Part 2"],
        stories_and_analogies: ["The noisy compass"],
      },
    },
  },
};

describe("PaperSeries", () => {
  beforeEach(() => {
    vi.mocked(api).mockResolvedValue(detail);
  });

  it("renders the immutable series bible and makes stale following outputs explicit", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/paper-series/91?part=911"]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <Routes>
          <Route path="/paper-series/:id" element={<PaperSeries />} />
        </Routes>
      </MemoryRouter>,
    );

    const bible = await screen.findByRole("heading", { name: "Series bible" });
    const bibleSection = bible.closest("section");
    expect(bibleSection).not.toBeNull();
    expect(within(bibleSection as HTMLElement).getByText("Revision 2", { exact: false })).toBeInTheDocument();
    expect(within(bibleSection as HTMLElement).getByText("ELBO — /el-bo/ — evidence lower bound")).toBeInTheDocument();
    expect(within(bibleSection as HTMLElement).getByText("Return to calibration in Part 2")).toBeInTheDocument();
    expect(within(bibleSection as HTMLElement).getByText("The noisy compass")).toBeInTheDocument();
    expect(within(bibleSection as HTMLElement).getByText("Keep examples grounded in deployment decisions.")).toBeInTheDocument();

    expect(screen.getByRole("button", { name: "Rebuild this and following" })).toBeInTheDocument();
    const partTwoTab = screen.getByRole("button", { name: "Part 2Stale" });
    expect(partTwoTab).toBeInTheDocument();

    await user.click(partTwoTab);
    expect(screen.getByText("Following output stale")).toBeInTheDocument();
    expect(screen.getAllByText("Stale — rebuild required")).toHaveLength(2);
  });
});

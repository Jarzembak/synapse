import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Job } from "../api";
import JobFailureDetails, {
  hasJobFailureDetails,
  parseJobDiagnostics,
} from "../components/JobFailureDetails";

function failedJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 42,
    project_id: 7,
    task: "repo_inventory",
    task_label: "Repository inventory",
    status: "error",
    progress: "reducing level 1, batch 2 of 3",
    error: "repository reduction exhausted adaptive subdivisions\nTraceback: internal detail",
    ...overrides,
  };
}

describe("JobFailureDetails", () => {
  it("presents the causal repository diagnostics before collapsed technical details", () => {
    const job = failedJob({
      diagnostics: {
        stage: "repository_reduce",
        effective_model: { provider: "ollama", model: "qwen3:8b" },
        context: {
          requested: 65536,
          effective: 40960,
          native: 40960,
          timeout_seconds: 1800,
          max_output_tokens: 3500,
        },
        reduction: {
          purpose: "repository_inventory",
          level: 1,
          batch: 2,
          batch_count: 3,
          items: 18,
          input_chars: 76120,
          subdivision_depth: 2,
        },
        cache: {
          leaf_maps_reused: 64,
          leaf_maps_new: 0,
          reductions_reused: 1,
          reductions_new: 2,
        },
        attempts: [
          {
            outcome: "timeout",
            level: 1,
            batch: 2,
            depth: 0,
            detail: "The original batch reached its deadline.",
          },
          {
            outcome: "subdivided",
            level: 1,
            batch: 2,
            depth: 1,
            detail: "Retried as two smaller batches.",
          },
        ],
        cause: "The smallest reduction batch still exceeded the model deadline.",
      },
    });

    render(<JobFailureDetails job={job} />);

    expect(screen.getByText("The smallest reduction batch still exceeded the model deadline."))
      .toBeInTheDocument();
    expect(screen.getByText("ollama/qwen3:8b")).toBeInTheDocument();
    expect(screen.getByText(/requested 65,536 tokens/)).toHaveTextContent(
      "effective 40,960 tokens",
    );
    expect(screen.getByText(/repository inventory; level 1; batch 2 of 3/))
      .toHaveTextContent("subdivision depth 2");
    expect(screen.getByText(/64 leaf maps reused/)).toHaveTextContent(
      "1 reductions reused",
    );
    expect(screen.getByText(/timeout \(level 1, batch 2, depth 0\)/))
      .toHaveTextContent("original batch reached its deadline");
    expect(screen.getByText(/subdivided \(level 1, batch 2, depth 1\)/))
      .toHaveTextContent("Retried as two smaller batches");
    expect(screen.getByText(
      "Later repository analysis jobs were skipped because repository inventory failed.",
    )).toBeInTheDocument();
    expect(screen.getByText("Technical error details").closest("details"))
      .not.toHaveAttribute("open");
  });

  it("parses JSON text returned by mixed-version job APIs", () => {
    const job = failedJob({
      task: "repo_architecture",
      diagnostics: JSON.stringify({
        stage: "repository_reduce",
        effective_model: { provider: "ollama", model: "qwen3.5:4b-q4_K_M" },
        reduction: { level: "2", batch: "1", batch_count: "2" },
        cause: "A child reduction could not be combined.",
      }),
    });

    render(<JobFailureDetails job={job} />);

    expect(screen.getByText("A child reduction could not be combined.")).toBeInTheDocument();
    expect(screen.getByText("ollama/qwen3.5:4b-q4_K_M")).toBeInTheDocument();
    expect(screen.getByText("level 2; batch 1 of 2")).toBeInTheDocument();
    expect(screen.queryByText(/Later repository analysis jobs were skipped/))
      .not.toBeInTheDocument();
  });

  it("shows resource admission and Ollama replacement diagnostics", () => {
    const job = failedJob({
      error: "model admission failed",
      diagnostics: {
        stage: "repository_reduce",
        context: {
          requested: 16384,
          effective: 16384,
          safety_assessment: {
            tier: "blocked",
            message: "The replacement model still exceeds the safe budget.",
            estimated_total_bytes: 5 * 1024 ** 3,
            resident_transition: {
              required: true,
              resident_models: ["mapper:latest"],
              replaced_models: ["mapper:latest"],
              reclaimable_ram_bytes: 1 * 1024 ** 3,
              reclaimable_vram_bytes: 5 * 1024 ** 3,
            },
          },
        },
        cause: "The replacement model still exceeds the safe budget.",
      },
    });

    render(<JobFailureDetails job={job} />);

    expect(screen.getByText(/blocked; The replacement model still exceeds/))
      .toHaveTextContent("estimated requirement 5 GiB");
    expect(screen.getByText(/replacement assessed for mapper:latest/))
      .toHaveTextContent("1 GiB RAM and 5 GiB VRAM reclaimable by Ollama");
  });

  it("parses and presents exact repository reduction stagnation diagnostics", () => {
    const diagnostics = parseJobDiagnostics(JSON.stringify({
      stage: "repository_reduce",
      stagnation: {
        reason: "all_multi_item_reductions_failed",
        level: "1",
        batch_input_limit_chars: "20000",
        writer_input_limit_chars: "64000",
        input_items: "11",
        output_items: "11",
        input_chars: "51052",
        output_chars: "51052",
        input_writer_chars: "66179",
        output_writer_chars: "66179",
        writer_overhead_chars: "15127",
        item_delta: "0",
        char_delta: "0",
        top_level_batches: "3",
        model_calls: "7",
        model_reductions_accepted: "0",
        accepted_reductions: "0",
        accepted_reductions_total: "0",
        cache_hits: "0",
        singleton_passthroughs: "11",
        subdivisions: "7",
        outcome_counts: { invalid_structure: "7" },
        evidence_id_count_before: "11",
        evidence_id_count_after: "11",
        evidence_preserved: true,
      },
      cause: "Repository reduction made no bounded progress.",
    }));

    expect(diagnostics?.stagnation).toMatchObject({
      reason: "all_multi_item_reductions_failed",
      level: 1,
      writer_input_limit_chars: 64000,
      input_writer_chars: 66179,
      output_writer_chars: 66179,
      writer_overhead_chars: 15127,
      item_delta: 0,
      char_delta: 0,
      model_calls: 7,
      model_reductions_accepted: 0,
      accepted_reductions_total: 0,
      cache_hits: 0,
      singleton_passthroughs: 11,
      subdivisions: 7,
      outcome_counts: { invalid_structure: 7 },
      evidence_preserved: true,
    });

    render(<JobFailureDetails job={failedJob({
      task: "repo_architecture",
      error: "Repository reduction made no bounded progress.",
      diagnostics,
    })} />);

    const summary = screen.getByText(/all multi item reductions failed at level 1/);
    expect(summary).toHaveTextContent(
      "writer base 66,179 to 66,179 characters against a 64,000-character limit",
    );
    expect(summary).toHaveTextContent(
      "fixed writer overhead 15,127 characters within the 64,000-character limit",
    );
    expect(summary).toHaveTextContent("items 11 to 11 (delta 0)");
    expect(summary).toHaveTextContent("evidence 51,052 to 51,052 characters (delta 0)");
    expect(summary).toHaveTextContent(
      "7 model calls; 0 model reductions accepted; 0 cached reductions reused",
    );
    expect(summary).toHaveTextContent("11 singleton passthroughs; 7 subdivisions");
    expect(summary).toHaveTextContent("evidence IDs preserved (11 to 11)");
  });

  it("shows when fixed repository writer overhead exceeds the budget", () => {
    render(<JobFailureDetails job={failedJob({
      task: "repo_inventory",
      error: "Repository fixed input exceeds the budget.",
      diagnostics: {
        stage: "repository_reduce",
        stagnation: {
          reason: "fixed_writer_overhead_exceeds_budget",
          level: 0,
          writer_input_limit_chars: 64000,
          evidence_context_chars: 2,
          output_writer_chars: 70117,
          writer_overhead_chars: 70115,
          evidence_id_count_before: 11,
          evidence_id_count_after: 11,
          evidence_preserved: true,
        },
        cause: "Repository fixed input exceeds the budget.",
      },
    })} />);

    const summary = screen.getByText(/fixed writer overhead exceeds budget at level 0/);
    expect(summary).toHaveTextContent(
      "writer base 70,117 characters against a 64,000-character limit",
    );
    expect(summary).toHaveTextContent(
      "fixed writer overhead 70,115 characters, exceeding the 64,000-character limit",
    );
    expect(summary).toHaveTextContent("evidence IDs preserved (11 to 11)");
  });

  it("uses the persisted accepted-reduction field as a compatibility fallback", () => {
    render(<JobFailureDetails job={failedJob({
      task: "repo_usage",
      error: "Repository reduction stalled.",
      diagnostics: {
        stagnation: {
          reason: "no_bounded_progress",
          accepted_reductions: 3,
          cache_hits: 2,
        },
      },
    })} />);

    const summary = screen.getByText(/no bounded progress/);
    expect(summary).toHaveTextContent(
      "2 cached reductions reused; 3 accepted reductions",
    );
  });

  it("retains useful output for old jobs and malformed diagnostic strings", () => {
    expect(parseJobDiagnostics("model returned an empty response")).toEqual({
      cause: "model returned an empty response",
    });
    expect(parseJobDiagnostics("{not-json")).toEqual({ cause: "{not-json" });
    expect(parseJobDiagnostics(17)).toBeNull();

    render(<JobFailureDetails job={failedJob({
      task: "repo_usage",
      diagnostics: undefined,
      error: "Traceback (most recent call last):\nworker frame\nTimeoutError: request expired",
    })} />);

    expect(screen.getByText("TimeoutError: request expired")).toBeInTheDocument();
    expect(screen.getByText("Technical error details").closest("details"))
      .not.toHaveAttribute("open");
  });

  it("does not treat the default empty diagnostic document as a failure", () => {
    expect(hasJobFailureDetails(failedJob({
      status: "done",
      error: "",
      diagnostics: "{}",
    }))).toBe(false);
    expect(hasJobFailureDetails(failedJob({
      status: "running",
      error: "",
      diagnostics: { stage: "repository_reduce" },
    }))).toBe(false);
    expect(hasJobFailureDetails(failedJob({
      error: "",
      diagnostics: JSON.stringify({ cause: "The reducer failed." }),
    }))).toBe(true);
  });
});

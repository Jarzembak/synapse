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

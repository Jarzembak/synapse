import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import PaperImport from "../pages/PaperImport";

describe("PaperImport", () => {
  it("starts with safe dense-paper defaults and supports independent audience selection", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <PaperImport />
      </MemoryRouter>,
    );

    expect(screen.getByText("Maximum 250 MiB and 500 pages.")).toBeInTheDocument();
    expect(screen.getByText(/250 MiB · 500 pages · 5 million extracted characters/)).toBeInTheDocument();

    const generalist = screen.getByRole("checkbox", { name: /Generalist/ });
    const practitioner = screen.getByRole("checkbox", { name: /Practitioner/ });
    const expert = screen.getByRole("checkbox", { name: /Expert/ });
    expect(generalist).toBeChecked();
    expect(practitioner).not.toBeChecked();
    expect(expert).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Local-only processing/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Analyze and draft audience plans/ })).toBeChecked();

    await user.click(practitioner);
    await user.click(expert);
    await user.click(generalist);

    expect(generalist).not.toBeChecked();
    expect(practitioner).toBeChecked();
    expect(expert).toBeChecked();
  });
});

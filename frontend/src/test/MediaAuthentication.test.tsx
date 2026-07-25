import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MediaAuthentication, {
  closeAuthenticationWindow,
} from "../components/MediaAuthentication";
import type { MediaAuthStatus } from "../api";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: apiMock };
});

function status(overrides: Partial<MediaAuthStatus> = {}): MediaAuthStatus {
  return {
    available: true,
    applicable: true,
    active: false,
    browser_url: null,
    cookies_present: false,
    project_id: 7,
    ...overrides,
  };
}

beforeEach(() => {
  closeAuthenticationWindow();
  apiMock.mockReset();
  vi.restoreAllMocks();
});

describe("MediaAuthentication", () => {
  it("opens the isolated browser and exposes explicit completion controls", async () => {
    const user = userEvent.setup();
    const replace = vi.fn();
    const focus = vi.fn();
    const close = vi.fn();
    const popup = {
      opener: window,
      document: {
        title: "",
        body: { textContent: "" },
      },
      location: { replace },
      focus,
      close,
    };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/projects/7/auth" && !options) return status();
      if (path === "/projects/7/auth/browser" && options?.method === "POST") {
        return status({
          active: true,
          browser_url: "/api/projects/7/auth/browser/view/session-token/",
        });
      }
      throw new Error(`unexpected API request: ${path}`);
    });

    render(<MediaAuthentication projectId={7} />);
    await user.click(await screen.findByRole("button", { name: "Sign in to source" }));

    expect(window.open).toHaveBeenCalled();
    expect(popup.opener).toBeNull();
    expect(replace).toHaveBeenCalledWith(
      new URL(
        "/api/projects/7/auth/browser/view/session-token/",
        window.location.origin,
      ).toString(),
    );
    expect(focus).toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Use this sign-in" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Save & start ingest" })).toBeVisible();
  });

  it("captures source access and continues with ingest", async () => {
    const user = userEvent.setup();
    const changed = vi.fn();
    apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/projects/7/auth" && !options) {
        return status({
          active: true,
          browser_url: "/api/projects/7/auth/browser/view/session-token/",
        });
      }
      if (
        path === "/projects/7/auth/browser/complete"
        && options?.method === "POST"
      ) {
        return status({
          cookies_present: true,
          captured_at: "2026-07-25T14:00:00+00:00",
          authenticated_host: "sans.zoom.us",
          cookie_count: 2,
        });
      }
      if (path === "/projects/7/run/ingest" && options?.method === "POST") {
        return { id: 91, status: "queued" };
      }
      throw new Error(`unexpected API request: ${path}`);
    });

    render(
      <MediaAuthentication projectId={7} onPipelineChanged={changed} />,
    );
    await user.click(
      await screen.findByRole("button", { name: "Save & start ingest" }),
    );

    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        "/projects/7/run/ingest",
        { method: "POST" },
      );
    });
    expect(changed).toHaveBeenCalled();
    expect(await screen.findByText("Source access saved and media ingest queued.")).toBeVisible();
  });

  it("keeps cookies.txt available when the browser sidecar is disabled", async () => {
    apiMock.mockResolvedValue(status({ available: false }));

    render(<MediaAuthentication projectId={7} />);

    expect(await screen.findByText(/interactive browser is unavailable/i)).toBeVisible();
    expect(screen.getByRole("button", { name: "Sign in to source" })).toBeDisabled();
    expect(screen.getByText("Advanced fallback: upload cookies.txt")).toBeVisible();
  });
});

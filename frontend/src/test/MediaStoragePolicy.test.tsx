import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import MediaStoragePolicy from "../components/MediaStoragePolicy";

const { apiMock } = vi.hoisted(() => ({ apiMock: vi.fn() }));

vi.mock("../api", async () => {
  const actual = await vi.importActual<typeof import("../api")>("../api");
  return { ...actual, api: apiMock };
});

vi.mock("../useEventSource", () => ({
  useEventSource: vi.fn(),
}));

const status = {
  policy: { mode: "keep_local", storage_target_id: null },
  target: null,
  summary: {
    eligible_objects: 2,
    total_bytes: 3 * 1024 ** 3,
    local_objects: 2,
    local_bytes: 3 * 1024 ** 3,
    verified_objects: 0,
    cloud_only_objects: 0,
    remote_objects: 0,
    restorable_objects: 0,
    pending_objects: 0,
    error_objects: 0,
    excluded_objects: 1,
  },
  objects: [],
};

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
  apiMock.mockReset();
  apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
    if (path === "/projects/7/media-storage" && !options) return status;
    if (path === "/projects/7/media-storage" && options?.method === "PUT") {
      return {
        ...status,
        policy: { mode: "cloud_primary", storage_target_id: 3 },
        target: { id: 3, provider: "s3", remote_base: "synapse" },
      };
    }
    if (path === "/projects/7/media-storage/sync" && options?.method === "POST") {
      return { id: 99, status: "queued" };
    }
    throw new Error(`unexpected API request: ${path}`);
  });
});

describe("MediaStoragePolicy", () => {
  it("defaults to keeping files local and explains exclusions", async () => {
    render(<MemoryRouter><MediaStoragePolicy projectId={7} /></MemoryRouter>);

    expect(await screen.findByRole("radio", { name: /Keep locally/ })).toBeChecked();
    expect(screen.getAllByText(/3.0 GiB/)).toHaveLength(2);
    expect(screen.getByText(/original paper PDF are never eligible/)).toBeInTheDocument();
  });

  it("enables cloud-primary per project and queues verified synchronization", async () => {
    const user = userEvent.setup();
    render(<MemoryRouter><MediaStoragePolicy projectId={7} /></MemoryRouter>);

    await user.click(await screen.findByRole("radio", { name: /Cloud-primary/ }));
    await waitFor(() => {
      const request = apiMock.mock.calls.find(([path, options]) =>
        path === "/projects/7/media-storage" && options?.method === "PUT");
      expect(JSON.parse(request![1].body as string)).toEqual({ mode: "cloud_primary" });
    });

    await user.click(screen.getByRole("button", { name: "Sync and verify media" }));
    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        "/projects/7/media-storage/sync",
        { method: "POST" },
      );
    });
  });

  it("queues restoration when a cloud-primary project switches back to keep-local", async () => {
    const user = userEvent.setup();
    const cloudOnly = {
      ...status,
      policy: { mode: "cloud_primary", storage_target_id: 3 },
      target: { id: 3, provider: "s3", remote_base: "synapse" },
      summary: {
        ...status.summary,
        local_objects: 1,
        local_bytes: 1024 ** 3,
        verified_objects: 1,
        cloud_only_objects: 1,
        remote_objects: 2,
        restorable_objects: 1,
      },
    };
    apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/projects/7/media-storage" && !options) return cloudOnly;
      if (path === "/projects/7/media-storage" && options?.method === "PUT") {
        return { ...cloudOnly, policy: { mode: "keep_local", storage_target_id: 3 } };
      }
      if (path === "/projects/7/media-storage/restore" && options?.method === "POST") {
        return { id: 101, status: "queued" };
      }
      if (path === "/projects/7/media-storage/purge" && options?.method === "POST") {
        return { id: 102, status: "queued" };
      }
      throw new Error(`unexpected API request: ${path}`);
    });
    render(<MemoryRouter><MediaStoragePolicy projectId={7} /></MemoryRouter>);

    await user.click(await screen.findByRole("radio", { name: /Keep locally/ }));

    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        "/projects/7/media-storage/restore",
        { method: "POST" },
      );
      expect(screen.getByText(/restoration has been queued/)).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: "Remove remote media copies" }));
    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        "/projects/7/media-storage/purge",
        { method: "POST" },
      );
    });
  });

  it("offers cleanup for a legacy cloud path without an authoritative object", async () => {
    const legacyOnly = {
      ...status,
      policy: { mode: "keep_local", storage_target_id: 3 },
      target: { id: 3, provider: "s3", remote_base: "synapse" },
    };
    apiMock.mockImplementation(async (path: string, options?: RequestInit) => {
      if (path === "/projects/7/media-storage" && !options) return legacyOnly;
      if (path === "/projects/7/media-storage/purge" && options?.method === "POST") {
        return { id: 103, status: "queued" };
      }
      throw new Error(`unexpected API request: ${path}`);
    });
    const user = userEvent.setup();
    render(<MemoryRouter><MediaStoragePolicy projectId={7} /></MemoryRouter>);

    await user.click(await screen.findByRole(
      "button", { name: "Remove remote media copies" },
    ));
    await waitFor(() => {
      expect(apiMock).toHaveBeenCalledWith(
        "/projects/7/media-storage/purge",
        { method: "POST" },
      );
    });
  });
});

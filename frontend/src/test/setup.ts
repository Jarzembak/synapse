import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => cleanup());

class EventSourceStub {
  onerror: ((event: Event) => void) | null = null;

  constructor(_url: string | URL) {}

  addEventListener() {}

  close() {}
}

Object.defineProperty(globalThis, "EventSource", {
  configurable: true,
  writable: true,
  value: EventSourceStub,
});

Object.defineProperty(globalThis, "confirm", {
  configurable: true,
  writable: true,
  value: vi.fn(() => true),
});

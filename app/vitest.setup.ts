// Vitest global setup — jest-dom matchers + Next mocks so chat components render
// in jsdom. Use renderWithProviders() from ./test-utils for components that read
// the TanStack Query cache. See library/TEST_PLAN.md.
import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// Components call usePathname/useRouter/useParams; stub them for jsdom.
vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({}),
  redirect: vi.fn(),
  notFound: vi.fn(),
}));

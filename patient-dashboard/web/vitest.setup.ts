/**
 * Vitest setup. `@testing-library/jest-dom` registers DOM matchers
 * (`toBeInTheDocument`, etc.) on `expect` for jsdom-environment tests.
 * Loading it under node is harmless — the matchers only fire when a
 * jsdom-rendered element is passed in.
 */
import "@testing-library/jest-dom/vitest";

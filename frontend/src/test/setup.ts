import "@testing-library/jest-dom/vitest";

// The suite drives the real API rather than a mock: the point of these tests is
// that the page a clinician sees agrees with the numbers the extractor built,
// and a mocked response could only ever agree with itself. The base URL comes
// from .env (VITE_API_BASE), the same place the app reads it.

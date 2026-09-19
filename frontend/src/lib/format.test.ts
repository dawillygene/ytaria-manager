import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, formatEta, formatSpeed } from "./format";

describe("format", () => {
  it("formats bytes", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(1536)).toBe("1.5 KB");
    expect(formatBytes(5 * 1024 ** 3)).toBe("5.0 GB");
    expect(formatBytes(null)).toBe("—");
  });
  it("formats durations, eta and speed", () => {
    expect(formatDuration(65)).toBe("1:05");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatEta(45)).toBe("45s left");
    expect(formatEta(600)).toBe("10 min left");
    expect(formatSpeed(2 * 1024 * 1024)).toBe("2.0 MB/s");
    expect(formatSpeed(null)).toBe("");
  });
});

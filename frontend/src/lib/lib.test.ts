import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { shouldRetry } from "../app/queryClient";
import { tokenStorage } from "../auth/tokenStorage";
import { ConfigError, loadConfig, parseApiBaseUrl } from "../config/env";
import { formatDuration, formatTimestamp, languageName } from "./format";
import { paramsFromSearch } from "./listParams";
import { pageWindow } from "./pagination";
import { safeRedirect } from "./redirect";
import { validateEmail, validateNewPassword, validateText, validateTitle } from "./validation";

describe("env config", () => {
  it("reads and normalises VITE_API_BASE_URL", () => {
    expect(parseApiBaseUrl(" http://localhost:8000/api/v1/ ")).toBe("http://localhost:8000/api/v1");
    expect(
      loadConfig({ VITE_API_BASE_URL: "https://api.example.com/api/v1" } as ImportMetaEnv),
    ).toEqual({
      apiBaseUrl: "https://api.example.com/api/v1",
    });
  });

  it("rejects a missing or invalid URL", () => {
    expect(() => parseApiBaseUrl(undefined)).toThrow(ConfigError);
    expect(() => parseApiBaseUrl("")).toThrow(/not set/);
    expect(() => parseApiBaseUrl("localhost:8000")).toThrow(ConfigError);
    expect(() => parseApiBaseUrl("ftp://host/api")).toThrow(/http/);
  });

  it("is configured for tests through the environment, not hardcoded", () => {
    expect(loadConfig().apiBaseUrl).toBe("http://api.test/api/v1");
  });
});

describe("tokenStorage", () => {
  it("stores, expires and clears the token", () => {
    tokenStorage.set("abc", 60, 1_000);
    expect(tokenStorage.get(30_000)).toBe("abc");
    expect(tokenStorage.get(61_000)).toBeNull();
    expect(window.localStorage.getItem(tokenStorage.key)).toBeNull(); // dropped once expired
    tokenStorage.set("abc", 60);
    tokenStorage.clear();
    expect(tokenStorage.get()).toBeNull();
  });

  it("ignores a corrupt stored value", () => {
    window.localStorage.setItem(tokenStorage.key, "{not json");
    expect(tokenStorage.get()).toBeNull();
  });
});

describe("format", () => {
  it("formats durations and timestamps", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(65_000)).toBe("1:05");
    expect(formatDuration(3_725_000)).toBe("1:02:05");
    expect(formatTimestamp(83_450)).toBe("1:23.4");
    expect(formatTimestamp(2_400)).toBe("0:02.4");
  });

  it("names languages", () => {
    expect(languageName("hi")).toBe("Hindi (hi)");
    expect(languageName("fr")).toBe("fr");
    expect(languageName(null)).toBe("—");
  });
});

describe("validation", () => {
  it("validates email and password like the backend", () => {
    expect(validateEmail("")).toBeDefined();
    expect(validateEmail("a@b")).toBeDefined();
    expect(validateEmail(" a@b.co ")).toBeUndefined();
    expect(validateNewPassword("1234567")).toBe("Use at least 8 characters.");
    expect(validateNewPassword("        ")).toBe("The password cannot be only spaces.");
    expect(validateNewPassword("x".repeat(129))).toBe("Use at most 128 characters.");
    expect(validateNewPassword("12345678")).toBeUndefined();
  });

  it("validates title and text", () => {
    expect(validateTitle("")).toBeUndefined();
    expect(validateTitle("  ", { required: true })).toBe("Enter a title.");
    expect(validateTitle("x".repeat(201))).toBeDefined();
    expect(validateText("   ")).toBeDefined();
    expect(validateText("ok")).toBeUndefined();
  });
});

describe("list params", () => {
  it("parses valid values and drops invalid ones", () => {
    expect(
      paramsFromSearch(new URLSearchParams("page=3&page_size=20&q=%20hi%20&status=completed")),
    ).toEqual({
      page: 3,
      page_size: 20,
      q: "hi",
      status: "completed",
    });
    expect(
      paramsFromSearch(new URLSearchParams("page=-1&page_size=1000&speech_mode=fr&source=x")),
    ).toEqual({ page: 1, page_size: 100 });
  });
});

describe("pagination window", () => {
  it("shows all pages when few, gaps when many", () => {
    expect(pageWindow(1, 3)).toEqual([1, 2, 3]);
    expect(pageWindow(5, 10)).toEqual([1, null, 4, 5, 6, null, 10]);
    expect(pageWindow(1, 10)).toEqual([1, 2, null, 10]);
  });
});

describe("safeRedirect", () => {
  it("allows only same-app paths", () => {
    expect(safeRedirect("/transcriptions/1?edit=1")).toBe("/transcriptions/1?edit=1");
    expect(safeRedirect("//evil.example")).toBe("/");
    expect(safeRedirect("https://evil.example")).toBe("/");
    expect(safeRedirect(undefined)).toBe("/");
  });
});

describe("query retry policy", () => {
  it("retries network and 5xx errors only, at most twice", () => {
    const network = new ApiError({ status: 0, code: "network_error", message: "x" });
    const server = new ApiError({ status: 503, code: "x", message: "x" });
    const notFound = new ApiError({ status: 404, code: "x", message: "x" });
    expect(shouldRetry(0, network)).toBe(true);
    expect(shouldRetry(1, server)).toBe(true);
    expect(shouldRetry(2, server)).toBe(false);
    expect(shouldRetry(0, notFound)).toBe(false);
  });
});

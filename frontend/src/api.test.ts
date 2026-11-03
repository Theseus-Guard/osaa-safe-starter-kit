import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// api.ts keeps the session state at module level. Each test loads a fresh copy.
type Api = typeof import("./api");

// The login and refresh responses carry tokens for API clients. The page must never keep them.
const TOKENS_1 = { access_token: "access-1", refresh_token: "refresh-1", expires_in: 900, password_change_required: false };
const TOKENS_2 = { access_token: "access-2", refresh_token: "refresh-2", expires_in: 900, password_change_required: false };
const ME = {
  id: "user-1",
  username: "example-user",
  role: "reporter",
  first_name: null,
  last_name: null,
  title: null,
  email: "user@example.com",
  password_change_required: false,
  timezone_setup_required: false,
};

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

let api: Api;
let fetchMock: ReturnType<typeof vi.fn>;

function headersOf(call: number): Headers {
  const init = fetchMock.mock.calls[call][1] as RequestInit | undefined;
  return new Headers(init?.headers);
}

function urlOf(call: number): string {
  return fetchMock.mock.calls[call][0] as string;
}

function credentialsOf(call: number): RequestCredentials | undefined {
  return (fetchMock.mock.calls[call][1] as RequestInit | undefined)?.credentials;
}

function setCookie(name: string, value: string): void {
  document.cookie = `${name}=${value}; path=/`;
}

function clearCookies(): void {
  for (const pair of document.cookie.split(";")) {
    const name = pair.split("=")[0].trim();
    if (name) document.cookie = `${name}=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/`;
  }
}

beforeEach(async () => {
  vi.resetModules();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  localStorage.clear();
  sessionStorage.clear();
  api = await import("./api");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  clearCookies();
});

describe("cookie session", () => {
  it("sends credentials and no authorization header on login", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1));

    await api.login("example-user", "example-password");

    expect(urlOf(0)).toBe("/api/v1/auth/token");
    expect(credentialsOf(0)).toBe("include");
    expect(headersOf(0).get("Authorization")).toBeNull();
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      username: "example-user",
      password: "example-password",
    });
  });

  it("sends credentials and no authorization header on a read after login", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1)).mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    const me = await api.me();

    expect(me).toEqual(ME);
    expect(credentialsOf(1)).toBe("include");
    expect(headersOf(1).get("Authorization")).toBeNull();
    expect(headersOf(1).get("X-CSRF-Token")).toBeNull();
  });

  it("sends credentials on every request, including readiness", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { status: "ready" })).mockResolvedValueOnce(jsonResponse(200, ME));

    await api.readiness();
    await api.me();

    expect(credentialsOf(0)).toBe("include");
    expect(credentialsOf(1)).toBe("include");
  });

  it("does not try to refresh when there is no session", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));

    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "not authenticated" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("maps a 400 validation body to http_400 when its detail is a list", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(400, {
        detail: [{ type: "missing", loc: ["body", "password"], msg: "Field required" }],
        error: {
          code: "validation_failed",
          message: "A field in the request is missing, has the wrong type, or is out of range.",
          request_id: "9b1e6c52-0c8f-4a57-8f3e-5d2a7f1c0e10",
          retryable: false,
          fields: [{ path: "password", problem: "missing" }],
        },
      }),
    );

    await expect(api.me()).rejects.toMatchObject({ status: 400, code: "http_400" });
  });

  it("reads a 413 body by its detail status", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(413, {
        detail: { status: "payload_too_large" },
        error: { code: "payload_too_large", message: "x", request_id: "r", retryable: false },
      }),
    );

    await expect(api.me()).rejects.toMatchObject({ status: 413, code: "payload_too_large" });
  });
});

describe("browser storage", () => {
  it("keeps no token in localStorage or sessionStorage", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(200, ME))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_2))
      .mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    await api.me();
    await api.me(); // a 401 and a refresh happen here

    for (const storage of [localStorage, sessionStorage]) {
      expect(storage.length).toBe(0);
    }
  });
});

describe("CSRF token on state-changing requests", () => {
  it("sends the safe_csrf cookie value as X-CSRF-Token on an unsafe request", async () => {
    setCookie("safe_csrf", "csrf-value-1");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ME));

    await api.changePassword("example-old-password", "example-new-password");

    expect(urlOf(0)).toBe("/api/v1/auth/password");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(headersOf(0).get("X-CSRF-Token")).toBe("csrf-value-1");
    expect(credentialsOf(0)).toBe("include");
  });

  it("sends the CSRF header on PUT, PATCH and DELETE too", async () => {
    setCookie("safe_csrf", "csrf-value-2");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.updateSystemTimezone({ timezone: "UTC", confirm: true, current: null });
    await api.updateProfile({ title: "Analyst" });
    await api.deleteUser("user-1");

    for (const call of [0, 1, 2]) expect(headersOf(call).get("X-CSRF-Token")).toBe("csrf-value-2");
  });

  it("does not send the CSRF header on a read", async () => {
    setCookie("safe_csrf", "csrf-value-3");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ME));

    await api.me();

    expect(headersOf(0).get("X-CSRF-Token")).toBeNull();
  });

  it("sends no CSRF header when the cookie is absent", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1));

    await api.login("example-user", "example-password");

    expect(headersOf(0).get("X-CSRF-Token")).toBeNull();
  });
});

describe("401 handling", () => {
  it("refreshes once with the cookie and retries the request", async () => {
    setCookie("safe_csrf", "csrf-value-4");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    const me = await api.me();

    expect(me).toEqual(ME);
    expect(urlOf(2)).toBe("/api/v1/auth/refresh");
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe("POST");
    expect((fetchMock.mock.calls[2][1] as RequestInit).body).toBeUndefined();
    expect(credentialsOf(2)).toBe("include");
    expect(headersOf(2).get("X-CSRF-Token")).toBe("csrf-value-4");
    expect(urlOf(3)).toBe("/api/v1/auth/me");
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("shares one refresh between concurrent 401s", async () => {
    let refreshed = false;
    let unauthorized = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });

    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/v1/auth/token") return jsonResponse(200, TOKENS_1);
      if (url === "/api/v1/auth/refresh") {
        // Hold the refresh until both 401s have arrived, and let their promise chains settle first.
        await gate;
        refreshed = true;
        return new Response(null, { status: 200 });
      }
      if (!refreshed) {
        unauthorized += 1;
        if (unauthorized === 2) setTimeout(release, 0);
        return jsonResponse(401, { detail: "token expired" });
      }
      return jsonResponse(200, { url });
    });

    await api.login("example-user", "example-password");
    const [first, second] = await Promise.all([api.me(), api.listCases(10)]);

    expect(first).toEqual({ url: "/api/v1/auth/me" });
    expect(second).toEqual({ url: "/api/v1/cases?limit=10" });
    const refreshes = fetchMock.mock.calls.filter(([url]) => url === "/api/v1/auth/refresh");
    expect(refreshes).toHaveLength(1);
  });

  it("ends the session and notifies the listener when the refresh fails", async () => {
    const listener = vi.fn();
    api.onSessionEnded(listener);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

    await api.login("example-user", "example-password");
    await expect(api.me()).rejects.toBeInstanceOf(api.ApiError);

    expect(listener).toHaveBeenCalledTimes(1);

    // The session is gone, so the next request is not refreshed again.
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));
    await expect(api.me()).rejects.toMatchObject({ code: "not authenticated" });
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("returns the server error when the retried request is still rejected, after one refresh", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(403, { detail: "forbidden" }));

    await api.login("example-user", "example-password");

    await expect(api.me()).rejects.toMatchObject({ status: 403, code: "forbidden" });
    const refreshes = fetchMock.mock.calls.filter(([url]) => url === "/api/v1/auth/refresh");
    expect(refreshes).toHaveLength(1);
  });

  it("clears the session after logout even when the logout request fails", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));

    await api.login("example-user", "example-password");
    await expect(api.logout()).rejects.toBeInstanceOf(TypeError);
    await expect(api.me()).rejects.toMatchObject({ code: "not authenticated" });

    // The session is cleared locally, so the 401 does not start a refresh.
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(urlOf(2)).toBe("/api/v1/auth/me");
  });

  it("sends the logout without a body, so the server reads the refresh cookie", async () => {
    setCookie("safe_csrf", "csrf-value-5");
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1)).mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.login("example-user", "example-password");
    await api.logout();

    expect(urlOf(1)).toBe("/api/v1/auth/logout");
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBeUndefined();
    expect(headersOf(1).get("X-CSRF-Token")).toBe("csrf-value-5");
    expect(credentialsOf(1)).toBe("include");
  });
});

describe("renewal before the refresh token expires", () => {
  const FIVE_MINUTES = 5 * 60 * 1000;

  it("renews the cookie pair before a request made after five minutes", async () => {
    vi.useFakeTimers();
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    vi.advanceTimersByTime(FIVE_MINUTES + 1);
    await api.me();

    expect(urlOf(1)).toBe("/api/v1/auth/refresh");
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBeUndefined();
    expect(urlOf(2)).toBe("/api/v1/auth/me");
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("does not renew a pair that was issued less than five minutes ago", async () => {
    vi.useFakeTimers();
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1)).mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    vi.advanceTimersByTime(FIVE_MINUTES - 1000);
    await api.me();

    expect(urlOf(1)).toBe("/api/v1/auth/me");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("ends the session on the request when the refresh token has already expired", async () => {
    vi.useFakeTimers();
    const listener = vi.fn();
    api.onSessionEnded(listener);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

    await api.login("example-user", "example-password");
    vi.advanceTimersByTime(FIVE_MINUTES + 1);

    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "session_expired" });
    expect(listener).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("responses and error mapping", () => {
  it("returns undefined for a 204 response and encodes the path segment", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(api.deleteUser("user 1")).resolves.toBeUndefined();
    expect(urlOf(0)).toBe("/api/v1/users/user%201");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("DELETE");
  });

  it("maps a string detail to the error code", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(409, { detail: "username already exists" }));

    await expect(
      api.createUser({ username: "example-user", role: "reporter", temporary_password: "example-temporary", email: "user@example.com" }),
    ).rejects.toMatchObject({ status: 409, code: "username already exists" });
  });

  it("maps an object detail with a status to the error code", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: { status: "setup_required" } }));

    await expect(api.getSystemTimezone()).rejects.toMatchObject({ status: 503, code: "setup_required" });
  });

  it("keeps the HTTP status when the error body is not JSON", async () => {
    fetchMock.mockResolvedValueOnce(new Response("Bad gateway", { status: 502 }));

    await expect(api.listRoles()).rejects.toMatchObject({ status: 502, code: "http_502" });
  });

  it("keeps the HTTP status when the error body is empty", async () => {
    fetchMock.mockResolvedValueOnce(new Response("", { status: 422 }));

    await expect(api.listRoles()).rejects.toMatchObject({ status: 422, code: "http_422" });
  });

  it("keeps the HTTP status when the object detail has no status field", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(500, { detail: { message: "unexpected" } }));
    fetchMock.mockResolvedValueOnce(jsonResponse(500, { detail: null }));

    await expect(api.listRoles()).rejects.toMatchObject({ code: "http_500" });
    await expect(api.listRoles()).rejects.toMatchObject({ code: "http_500" });
  });

  it("lets a network failure reject with a TypeError", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("Failed to fetch"));

    await expect(api.listPermissions()).rejects.toBeInstanceOf(TypeError);
  });

  it("returns the raw body for a blob request", async () => {
    vi.useFakeTimers();
    const createObjectURL = vi.fn(() => "blob:example");
    const revokeObjectURL = vi.fn();
    Object.assign(URL, { createObjectURL, revokeObjectURL });
    let downloadName = "";
    let downloadHref = "";
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloadName = this.download;
      downloadHref = this.href;
    });
    fetchMock.mockResolvedValueOnce(new Response("col1,col2", { status: 200 }));

    await api.downloadPrivacyExport("csv");
    vi.advanceTimersByTime(1000);

    expect(urlOf(0)).toBe("/api/v1/privacy/inventory/export?format=csv");
    expect(downloadName).toBe("privacy-inventory.csv");
    expect(downloadHref).toBe("blob:example");
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:example");
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    vi.restoreAllMocks();
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });
});

describe("readiness", () => {
  it("reports ready for a 200 response", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { status: "ready" }));

    await expect(api.readiness()).resolves.toBe("ready");
  });

  it("reports setup_required from the detail status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(503, { detail: { status: "setup_required" } }));

    await expect(api.readiness()).resolves.toBe("setup_required");
  });

  it("reports unavailable for any other failure", async () => {
    fetchMock.mockResolvedValueOnce(new Response("upstream down", { status: 503 }));

    await expect(api.readiness()).resolves.toBe("unavailable");
  });
});

describe("endpoints", () => {
  const cases: [string, (a: Api) => Promise<unknown>, string, string][] = [
    ["getProfile", (a) => a.getProfile(), "GET", "/api/v1/auth/profile"],
    ["updateProfile", (a) => a.updateProfile({ first_name: "Ada" }), "PATCH", "/api/v1/auth/profile"],
    ["updateUserProfile", (a) => a.updateUserProfile("user-2", { title: "" }), "PATCH", "/api/v1/users/user-2/profile"],
    ["listUsers", (a) => a.listUsers(10, "cursor-1"), "GET", "/api/v1/users?limit=10&cursor=cursor-1"],
    ["updateUser", (a) => a.updateUser("user-2", { is_active: false }, 5), "PATCH", "/api/v1/users/user-2"],
    ["resetUserPassword", (a) => a.resetUserPassword("user-2", "example-temporary"), "POST", "/api/v1/users/user-2/password"],
    ["listRoles", (a) => a.listRoles(), "GET", "/api/v1/rbac/roles"],
    ["listPermissions", (a) => a.listPermissions(), "GET", "/api/v1/rbac/permissions"],
    ["updateRolePermissions", (a) => a.updateRolePermissions("auditor", ["cases:read"]), "PUT", "/api/v1/rbac/roles/auditor/permissions"],
    ["listCases", (a) => a.listCases(5), "GET", "/api/v1/cases?limit=5"],
    ["listCases with cursor", (a) => a.listCases(5, "opaque-token"), "GET", "/api/v1/cases?limit=5&cursor=opaque-token"],
    ["listCases encodes cursor", (a) => a.listCases(5, "a+b/c="), "GET", "/api/v1/cases?limit=5&cursor=a%2Bb%2Fc%3D"],
    ["listCases with q", (a) => a.listCases(8, undefined, "SAFE-2026"), "GET", "/api/v1/cases?limit=8&q=SAFE-2026"],
    ["listCases encodes q", (a) => a.listCases(8, undefined, "a b&c"), "GET", "/api/v1/cases?limit=8&q=a%20b%26c"],
    ["listCases omits blank q", (a) => a.listCases(8, undefined, "   "), "GET", "/api/v1/cases?limit=8"],
    ["listCases with cursor, q and status", (a) => a.listCases(8, "next", "SAFE", "closed"), "GET", "/api/v1/cases?limit=8&cursor=next&q=SAFE&status=closed"],
    ["getCase", (a) => a.getCase("case-1"), "GET", "/api/v1/cases/case-1"],
    ["updateCaseTitle", (a) => a.updateCaseTitle("case-1", "New title", 4), "PATCH", "/api/v1/cases/case-1"],
    ["getSystemTimezone", (a) => a.getSystemTimezone(), "GET", "/api/v1/system/timezone"],
    [
      "updateSystemTimezone",
      (a) => a.updateSystemTimezone({ timezone: "UTC", confirm: true, current: null }),
      "PUT",
      "/api/v1/system/timezone",
    ],
    ["getPrivacyInventory", (a) => a.getPrivacyInventory(), "GET", "/api/v1/privacy/inventory"],
  ];

  it.each(cases)("%s uses the expected method and path", async (_name, call, method, url) => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await call(api);

    expect(urlOf(0)).toBe(url);
    expect((fetchMock.mock.calls[0][1] as RequestInit | undefined)?.method ?? "GET").toBe(method);
  });

  it("changes the password and clears the session", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));

    await api.login("example-user", "example-old-password");
    await api.changePassword("example-old-password", "example-new-password");
    await expect(api.me()).rejects.toMatchObject({ code: "not authenticated" });

    expect(urlOf(1)).toBe("/api/v1/auth/password");
  });

  it("creates a case with the UTC awareness time", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.createCase({ title: "Example case", awareness_time: "2026-10-31T06:00:00.000Z" });

    expect(urlOf(0)).toBe("/api/v1/cases");
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      title: "Example case",
      awareness_time: "2026-10-31T06:00:00.000Z",
    });
  });

  it("creates a report at the reporter intake with the contact", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.createReport({
      title: "Example case",
      awareness_time: "2026-10-31T06:00:00.000Z",
      contact: "reporter@example.com",
    });

    expect(urlOf(0)).toBe("/api/v1/reports");
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe("POST");
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      title: "Example case",
      awareness_time: "2026-10-31T06:00:00.000Z",
      contact: "reporter@example.com",
    });
  });

  it("sends a null contact to the reporter intake when none is given", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.createReport({ title: "Example case", awareness_time: "2026-10-31T06:00:00.000Z", contact: null });

    expect(urlOf(0)).toBe("/api/v1/reports");
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      title: "Example case",
      awareness_time: "2026-10-31T06:00:00.000Z",
      contact: null,
    });
  });
});

describe("case and evidence endpoints", () => {
  function bodyOf(call: number): unknown {
    return JSON.parse((fetchMock.mock.calls[call][1] as RequestInit).body as string);
  }

  function methodOf(call: number): string {
    return (fetchMock.mock.calls[call][1] as RequestInit | undefined)?.method ?? "GET";
  }

  it("closes a case with the reason", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.closeCase("case/1", "Handled");

    expect(urlOf(0)).toBe("/api/v1/cases/case%2F1/close");
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({ reason: "Handled" });
  });

  it("reopens a case with the reason", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.reopenCase("case/1", "New evidence");

    expect(urlOf(0)).toBe("/api/v1/cases/case%2F1/reopen");
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({ reason: "New evidence" });
  });

  it("opens a review round with a new Idempotency-Key", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

    await api.openReviewRound("case/1");

    expect(urlOf(0)).toBe("/api/v1/reviews");
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({ case_id: "case/1" });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("sets one review layer with its reason and sends the round revision as If-Match", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.setReviewLayer(
      "round/1",
      "supply-chain",
      { outcome: "fail", reason: "Missing SBOM", evidence_ids: [] },
      4,
    );

    expect(urlOf(0)).toBe("/api/v1/reviews/round%2F1/layers/supply-chain");
    expect(methodOf(0)).toBe("PATCH");
    expect(bodyOf(0)).toEqual({ outcome: "fail", reason: "Missing SBOM", evidence_ids: [] });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(init.headers).get("If-Match")).toBe('"4"');
    expect(new Headers(init.headers).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("reads the stored current revision from a precondition failure", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(412, { detail: { status: "precondition_failed", current_revision: 7 } }),
    );

    const error = await api.setReviewLayer("round-1", "model", { outcome: "pass" }, 3).catch(
      (caught: unknown) => caught,
    );

    expect(error).toBeInstanceOf(api.ApiError);
    expect(error).toMatchObject({ status: 412, code: "precondition_failed", currentRevision: 7 });
  });

  it("sends the reason only for a not-applicable deadline", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {})).mockResolvedValueOnce(jsonResponse(200, {}));

    await api.updateDeadline("case-1", "task-1", "done", undefined, 2);
    await api.updateDeadline("case-1", "task-1", "not_applicable", "Does not apply", 3);

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/deadlines/task-1");
    expect(methodOf(0)).toBe("PATCH");
    expect(bodyOf(0)).toEqual({ status: "done", reason: undefined });
    expect(bodyOf(1)).toEqual({ status: "not_applicable", reason: "Does not apply" });
    expect(headersOf(0).get("If-Match")).toBe('"2"');
    expect(headersOf(1).get("If-Match")).toBe('"3"');
  });

  it("sends the legal duty and account revisions as If-Match", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {})).mockResolvedValueOnce(jsonResponse(200, {}));

    await api.updateLegalDuty("case-1", "duty-1", { status: "done" }, 4);
    await api.updateUser("user-2", { is_active: false }, 5);

    expect(headersOf(0).get("If-Match")).toBe('"4"');
    expect(headersOf(1).get("If-Match")).toBe('"5"');
  });

  it("restores a deadline task with a POST to its restore route and no body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.restoreDeadline("case-1", "task-1");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/deadlines/task-1/restore");
    expect(methodOf(0)).toBe("POST");
  });

  it("lists and removes assignments", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [])).mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.listAssignments("case-1");
    await api.removeAssignment("case-1", "user-2");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/assignments");
    expect(urlOf(1)).toBe("/api/v1/cases/case-1/assignments/user-2");
    expect(methodOf(1)).toBe("DELETE");
  });

  it("adds an assignment and lists the users it may be given to", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [])).mockResolvedValueOnce(jsonResponse(201, {}));

    await api.listAssignableUsers("case-1");
    await api.addAssignment("case-1", "user-2");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/assignable-users");
    expect(methodOf(0)).toBe("GET");
    expect(urlOf(1)).toBe("/api/v1/cases/case-1/assignments");
    expect(methodOf(1)).toBe("POST");
    expect(bodyOf(1)).toEqual({ user_id: "user-2" });
  });

  it("lists the links of a case", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));

    await api.listCaseLinks("case-1");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/links");
    expect(methodOf(0)).toBe("GET");
  });

  it("adds a link with its type, reason, visibility checkbox and main case in the body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

    await api.addCaseLink("case-1", {
      linked_case_id: "case-2",
      link_type: "root_sub",
      reason: "Root of the analysis",
      share_visibility: true,
      primary_case_id: "case-2",
    });

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/links");
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({
      linked_case_id: "case-2",
      link_type: "root_sub",
      reason: "Root of the analysis",
      share_visibility: true,
      primary_case_id: "case-2",
    });
  });

  it("sends no main case for a related link", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

    await api.addCaseLink("case-1", {
      linked_case_id: "case-2",
      link_type: "related",
      reason: "Same incident",
      share_visibility: false,
    });

    expect(bodyOf(0)).toEqual({
      linked_case_id: "case-2",
      link_type: "related",
      reason: "Same incident",
      share_visibility: false,
    });
  });

  it("removes a link by the linked case ID and encodes the segment", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.removeCaseLink("case-1", "case 2/x");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/links/case%202%2Fx");
    expect(methodOf(0)).toBe("DELETE");
  });

  it("reads the reporter contact of a case", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { case_id: "case/1", contact: "example@example.com" }));

    await expect(api.getReporterContact("case/1")).resolves.toBe("example@example.com");

    expect(urlOf(0)).toBe("/api/v1/reports/case%2F1/contact");
    expect(methodOf(0)).toBe("GET");
  });

  it("resolves to null when the case has no contact recorded", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { case_id: "case-1", contact: null }));

    await expect(api.getReporterContact("case-1")).resolves.toBeNull();
  });

  it("resolves to null when the case was not opened through the reporter channel", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: "reporter contact not found" }));

    await expect(api.getReporterContact("case-1")).resolves.toBeNull();
  });

  it("rethrows a case that is not visible instead of reporting no contact", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(404, { detail: "case not found" }));

    await expect(api.getReporterContact("case-1")).rejects.toMatchObject({ status: 404, code: "case not found" });
  });

  it("reads the stable code and the current version from a classification version conflict", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse(409, { detail: { status: "classification_version_conflict", current_version: 3 } }),
    );

    await expect(api.updateClassification("case-1", { expected_version: 1 } as never)).rejects.toMatchObject({
      status: 409,
      code: "classification_version_conflict",
      currentVersion: 3,
    });
  });

  it("leaves the current version empty for a 409 whose detail is only text", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(409, { detail: "case is closed" }));

    await expect(api.updateClassification("case-1", { expected_version: 1 } as never)).rejects.toMatchObject({
      code: "case is closed",
      currentVersion: null,
    });
  });

  it("lists the review rounds of a case", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, []));

    await api.listReviewRounds("case-1");

    expect(urlOf(0)).toBe("/api/v1/reviews?case_id=case-1");
    expect(methodOf(0)).toBe("GET");
  });

  it("requests one page of history", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { items: [], next_cursor: null }));

    await api.getCaseHistory("case-1", 20, "cursor-2");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/history?limit=20&cursor=cursor-2");
  });

  it("requests one page of evidence and the custody log of one item", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { items: [], next_cursor: null })).mockResolvedValueOnce(
      jsonResponse(200, []),
    );

    await api.listEvidence("case-1", 20);
    await api.listCustodyEvents("evidence-1");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/evidence?limit=20");
    expect(urlOf(1)).toBe("/api/v1/evidence/evidence-1/custody");
  });

  it("uploads the file as multipart without a JSON content type", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));
    const file = new File(["example text"], "notes.txt", { type: "text/plain" });

    await api.uploadEvidence("case-1", file);

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(urlOf(0)).toBe("/api/v1/cases/case-1/evidence");
    expect(methodOf(0)).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect((init.body as FormData).get("file")).toBeInstanceOf(File);
    expect(new Headers(init.headers).get("Content-Type")).toBeNull();
  });

  it("returns the evidence bytes as a blob", async () => {
    fetchMock.mockResolvedValueOnce(new Response("example bytes", { status: 200 }));

    const blob = await api.downloadEvidence("evidence-1");

    expect(urlOf(0)).toBe("/api/v1/evidence/evidence-1/download");
    expect(blob).toBeInstanceOf(Blob);
    expect(await blob.text()).toBe("example bytes");
  });
});

describe("start-up session restore", () => {
  it("loads the session from a valid cookie without refreshing", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ME));

    await expect(api.restoreSession()).resolves.toEqual(ME);

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(urlOf(0)).toBe("/api/v1/auth/me");
    expect(credentialsOf(0)).toBe("include");
    expect(headersOf(0).get("Authorization")).toBeNull();
  });

  it("refreshes once with the CSRF header after a 401, then loads the session", async () => {
    setCookie("safe_csrf", "csrf-value-5");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(200, ME));

    await expect(api.restoreSession()).resolves.toEqual(ME);

    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(urlOf(1)).toBe("/api/v1/auth/refresh");
    expect((fetchMock.mock.calls[1][1] as RequestInit).method).toBe("POST");
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBeUndefined();
    expect(credentialsOf(1)).toBe("include");
    expect(headersOf(1).get("X-CSRF-Token")).toBe("csrf-value-5");
    expect(urlOf(2)).toBe("/api/v1/auth/me");
    expect(credentialsOf(2)).toBe("include");
  });

  it("resolves to null without a second refresh when the refresh fails", async () => {
    const listener = vi.fn();
    api.onSessionEnded(listener);
    setCookie("safe_csrf", "csrf-value-6");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid credentials" }));

    await expect(api.restoreSession()).resolves.toBeNull();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(urlOf(1)).toBe("/api/v1/auth/refresh");
    expect(listener).not.toHaveBeenCalled();
  });

  it("resolves to null when the refresh is refused for a missing CSRF header", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(403, { detail: "csrf_token_invalid" }));

    await expect(api.restoreSession()).resolves.toBeNull();

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(headersOf(1).get("X-CSRF-Token")).toBeNull();
  });

  it("resolves to null when /auth/me is still rejected after the refresh", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }));

    await expect(api.restoreSession()).resolves.toBeNull();

    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("shares one refresh between concurrent start-up checks", async () => {
    let refreshed = false;
    let unauthorized = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });

    fetchMock.mockImplementation(async (url: string) => {
      if (url === "/api/v1/auth/refresh") {
        // Hold the refresh until both start-up checks have seen a 401.
        await gate;
        refreshed = true;
        return new Response(null, { status: 200 });
      }
      if (!refreshed) {
        unauthorized += 1;
        if (unauthorized === 2) setTimeout(release, 0);
        return jsonResponse(401, { detail: "token expired" });
      }
      return jsonResponse(200, ME);
    });

    const [first, second] = await Promise.all([api.restoreSession(), api.restoreSession()]);

    expect(first).toEqual(ME);
    expect(second).toEqual(ME);
    const refreshes = fetchMock.mock.calls.filter(([url]) => url === "/api/v1/auth/refresh");
    expect(refreshes).toHaveLength(1);
  });

  it("returns the forced password change flag so the caller can show the change page", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { ...ME, password_change_required: true }));

    await expect(api.restoreSession()).resolves.toMatchObject({ password_change_required: true });
  });
});

describe("disclosure export and external notice actions", () => {
  const CASE = "case/1";
  const VERSION = "version/2";
  const NOTICE = "notice/3";
  const BASE = "/api/v1/cases/case%2F1/disclosures/version%2F2/notices/notice%2F3";

  function bodyOf(call: number): unknown {
    return JSON.parse((fetchMock.mock.calls[call][1] as RequestInit).body as string);
  }

  function methodOf(call: number): string {
    return (fetchMock.mock.calls[call][1] as RequestInit | undefined)?.method ?? "GET";
  }

  it("exports one disclosure version from its export route as a blob", async () => {
    fetchMock.mockResolvedValueOnce(new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));

    const blob = await api.exportDisclosure(CASE, VERSION);

    expect(blob).toBeInstanceOf(Blob);
    expect(urlOf(0)).toBe("/api/v1/cases/case%2F1/disclosures/version%2F2/export");
    expect(methodOf(0)).toBe("GET");
  });

  it("records a received receipt with its time and a new Idempotency-Key", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.recordNoticeReceipt(CASE, VERSION, NOTICE, {
      outcome: "received",
      received_at: "2026-11-02T10:00:00Z",
      reason_code: null,
      reason: null,
    });

    expect(urlOf(0)).toBe(`${BASE}/receipt`);
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({
      outcome: "received",
      received_at: "2026-11-02T10:00:00Z",
      reason_code: null,
      reason: null,
    });
    expect(headersOf(0).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("records an unknown receipt with its reason code", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.recordNoticeReceipt(CASE, VERSION, NOTICE, {
      outcome: "unknown",
      received_at: null,
      reason_code: "timeout",
      reason: "No answer in five days",
    });

    expect(urlOf(0)).toBe(`${BASE}/receipt`);
    expect(bodyOf(0)).toEqual({
      outcome: "unknown",
      received_at: null,
      reason_code: "timeout",
      reason: "No answer in five days",
    });
  });

  it("marks a follow-up as required, and as done, each with a new Idempotency-Key", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {})).mockResolvedValueOnce(jsonResponse(200, {}));

    await api.recordNoticeFollowUp(CASE, VERSION, NOTICE, { action: "require", reason: "Call the CERT" });
    await api.recordNoticeFollowUp(CASE, VERSION, NOTICE, { action: "complete", reason: "Reached by phone" });

    expect(urlOf(0)).toBe(`${BASE}/follow-up`);
    expect(bodyOf(0)).toEqual({ action: "require", reason: "Call the CERT" });
    expect(urlOf(1)).toBe(`${BASE}/follow-up`);
    expect(bodyOf(1)).toEqual({ action: "complete", reason: "Reached by phone" });
    const first = headersOf(0).get("Idempotency-Key");
    const second = headersOf(1).get("Idempotency-Key");
    expect(first).toMatch(/^[0-9a-f-]{36}$/);
    expect(second).toMatch(/^[0-9a-f-]{36}$/);
    expect(first).not.toBe(second);
  });

  it("cancels a notice with its reason and a new Idempotency-Key", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {}));

    await api.cancelNotice(CASE, VERSION, NOTICE, { reason: "Sent to the wrong address" });

    expect(urlOf(0)).toBe(`${BASE}/cancel`);
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({ reason: "Sent to the wrong address" });
    expect(headersOf(0).get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });
});

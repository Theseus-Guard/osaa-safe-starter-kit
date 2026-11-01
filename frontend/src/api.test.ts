import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// api.ts keeps the session and the refresh state at module level. Each test loads a fresh copy.
type Api = typeof import("./api");

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

function authorizationOf(call: number): string | null {
  const init = fetchMock.mock.calls[call][1] as RequestInit | undefined;
  return new Headers(init?.headers).get("Authorization");
}

function urlOf(call: number): string {
  return fetchMock.mock.calls[call][0] as string;
}

beforeEach(async () => {
  vi.resetModules();
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  api = await import("./api");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("bearer authentication", () => {
  it("does not send an authorization header on login", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1));

    await api.login("example-user", "example-password");

    expect(urlOf(0)).toBe("/api/v1/auth/token");
    expect(authorizationOf(0)).toBeNull();
    expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({
      username: "example-user",
      password: "example-password",
    });
  });

  it("sends the stored access token as a bearer header after login", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, TOKENS_1)).mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    const me = await api.me();

    expect(me).toEqual(ME);
    expect(authorizationOf(1)).toBe("Bearer access-1");
  });

  it("does not try to refresh when there is no session", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));

    await expect(api.me()).rejects.toMatchObject({ status: 401, code: "not authenticated" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("401 handling", () => {
  it("refreshes once and retries the request with the new access token", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_2))
      .mockResolvedValueOnce(jsonResponse(200, ME));

    await api.login("example-user", "example-password");
    const me = await api.me();

    expect(me).toEqual(ME);
    expect(urlOf(2)).toBe("/api/v1/auth/refresh");
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe("POST");
    expect(JSON.parse((fetchMock.mock.calls[2][1] as RequestInit).body as string)).toEqual({
      refresh_token: "refresh-1",
    });
    expect(authorizationOf(3)).toBe("Bearer access-2");
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("shares one refresh between concurrent 401s", async () => {
    let unauthorized = 0;
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });

    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/auth/token") return jsonResponse(200, TOKENS_1);
      if (url === "/api/v1/auth/refresh") {
        // Hold the refresh until both 401s have arrived, and let their promise chains settle first.
        await gate;
        return jsonResponse(200, TOKENS_2);
      }
      const authorization = new Headers(init?.headers).get("Authorization");
      if (authorization === "Bearer access-1") {
        unauthorized += 1;
        if (unauthorized === 2) setTimeout(release, 0);
        return jsonResponse(401, { detail: "token expired" });
      }
      if (authorization === "Bearer access-2") return jsonResponse(200, { url });
      throw new Error(`unexpected request ${url} with ${authorization}`);
    });

    await api.login("example-user", "example-password");
    const [first, second] = await Promise.all([api.me(), api.listCases(10, 0)]);

    expect(first).toEqual({ url: "/api/v1/auth/me" });
    expect(second).toEqual({ url: "/api/v1/cases?limit=10&offset=0" });
    const refreshes = fetchMock.mock.calls.filter(([url]) => url === "/api/v1/auth/refresh");
    expect(refreshes).toHaveLength(1);
  });

  it("ends the session and notifies the listener when the refresh fails", async () => {
    const listener = vi.fn();
    api.onSessionEnded(listener);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "invalid refresh token" }));

    await api.login("example-user", "example-password");
    await expect(api.me()).rejects.toBeInstanceOf(api.ApiError);

    expect(listener).toHaveBeenCalledTimes(1);

    // The session is gone, so the next request goes out without a bearer header.
    fetchMock.mockResolvedValueOnce(jsonResponse(401, { detail: "not authenticated" }));
    await expect(api.me()).rejects.toMatchObject({ code: "not authenticated" });
    expect(authorizationOf(fetchMock.mock.calls.length - 1)).toBeNull();
  });

  it("returns the server error when the retried request is still rejected, after one refresh", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_1))
      .mockResolvedValueOnce(jsonResponse(401, { detail: "token expired" }))
      .mockResolvedValueOnce(jsonResponse(200, TOKENS_2))
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

    expect(authorizationOf(2)).toBeNull();
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
    ["listUsers", (a) => a.listUsers(10, 20), "GET", "/api/v1/users?limit=10&offset=20"],
    ["updateUser", (a) => a.updateUser("user-2", { is_active: false }), "PATCH", "/api/v1/users/user-2"],
    ["resetUserPassword", (a) => a.resetUserPassword("user-2", "example-temporary"), "POST", "/api/v1/users/user-2/password"],
    ["listRoles", (a) => a.listRoles(), "GET", "/api/v1/rbac/roles"],
    ["listPermissions", (a) => a.listPermissions(), "GET", "/api/v1/rbac/permissions"],
    ["updateRolePermissions", (a) => a.updateRolePermissions("auditor", ["cases:read"]), "PUT", "/api/v1/rbac/roles/auditor/permissions"],
    ["listCases", (a) => a.listCases(5, 0), "GET", "/api/v1/cases?limit=5&offset=0"],
    ["listCases with q", (a) => a.listCases(8, 0, "SAFE-2026"), "GET", "/api/v1/cases?limit=8&offset=0&q=SAFE-2026"],
    ["listCases encodes q", (a) => a.listCases(8, 0, "a b&c"), "GET", "/api/v1/cases?limit=8&offset=0&q=a%20b%26c"],
    ["listCases omits blank q", (a) => a.listCases(8, 0, "   "), "GET", "/api/v1/cases?limit=8&offset=0"],
    ["getCase", (a) => a.getCase("case-1"), "GET", "/api/v1/cases/case-1"],
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
    expect(authorizationOf(2)).toBeNull();
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

  it("records a review with the outcome and the note", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(201, {}));

    await api.createReview("case/1", { outcome: "insufficient_evidence", note: null });

    expect(urlOf(0)).toBe("/api/v1/cases/case%2F1/reviews");
    expect(methodOf(0)).toBe("POST");
    expect(bodyOf(0)).toEqual({ outcome: "insufficient_evidence", note: null });
  });

  it("sends the reason only for a not-applicable deadline", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, {})).mockResolvedValueOnce(jsonResponse(200, {}));

    await api.updateDeadline("case-1", "task-1", "done");
    await api.updateDeadline("case-1", "task-1", "not_applicable", "Does not apply");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/deadlines/task-1");
    expect(methodOf(0)).toBe("PATCH");
    expect(bodyOf(0)).toEqual({ status: "done" });
    expect(bodyOf(1)).toEqual({ status: "not_applicable", reason: "Does not apply" });
  });

  it("lists and removes assignments", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, [])).mockResolvedValueOnce(new Response(null, { status: 204 }));

    await api.listAssignments("case-1");
    await api.removeAssignment("case-1", "user-2");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/assignments");
    expect(urlOf(1)).toBe("/api/v1/cases/case-1/assignments/user-2");
    expect(methodOf(1)).toBe("DELETE");
  });

  it("requests one page of history", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { items: [], total: 0, limit: 20, offset: 40 }));

    await api.getCaseHistory("case-1", 20, 40);

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/history?limit=20&offset=40");
  });

  it("requests one page of evidence and the custody log of one item", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { items: [], total: 0, limit: 20, offset: 0 })).mockResolvedValueOnce(
      jsonResponse(200, []),
    );

    await api.listEvidence("case-1", 20, 0);
    await api.listCustodyEvents("evidence-1");

    expect(urlOf(0)).toBe("/api/v1/cases/case-1/evidence?limit=20&offset=0");
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

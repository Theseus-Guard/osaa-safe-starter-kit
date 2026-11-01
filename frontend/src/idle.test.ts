import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { IDLE_LIMIT_MS, IDLE_WARNING_MS, watchIdle } from "./idle";

const MINUTE = 60 * 1000;

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("inactivity watch", () => {
  it("warns five minutes before the limit and then expires", () => {
    const onWarning = vi.fn();
    const onExpired = vi.fn();
    watchIdle({ onWarning, onExpired });

    vi.advanceTimersByTime(IDLE_LIMIT_MS - IDLE_WARNING_MS - MINUTE);
    expect(onWarning).not.toHaveBeenCalled();

    vi.advanceTimersByTime(2 * MINUTE);
    expect(onWarning).toHaveBeenCalledTimes(1);
    expect(onWarning).toHaveBeenLastCalledWith(true);
    expect(onExpired).not.toHaveBeenCalled();

    vi.advanceTimersByTime(IDLE_WARNING_MS);
    expect(onExpired).toHaveBeenCalledTimes(1);
  });

  it("clears the warning as soon as the user is active again", () => {
    const onWarning = vi.fn();
    const onExpired = vi.fn();
    watchIdle({ onWarning, onExpired });

    vi.advanceTimersByTime(IDLE_LIMIT_MS - IDLE_WARNING_MS + MINUTE);
    expect(onWarning).toHaveBeenLastCalledWith(true);

    window.dispatchEvent(new Event("pointerdown"));
    expect(onWarning).toHaveBeenLastCalledWith(false);
    expect(onWarning).toHaveBeenCalledTimes(2);
  });

  it("does not expire a session that keeps getting activity", () => {
    const onExpired = vi.fn();
    watchIdle({ onWarning: vi.fn(), onExpired });

    for (let elapsed = 0; elapsed < 2 * IDLE_LIMIT_MS; elapsed += 10 * MINUTE) {
      vi.advanceTimersByTime(10 * MINUTE);
      window.dispatchEvent(new Event("keydown"));
    }
    expect(onExpired).not.toHaveBeenCalled();
  });

  it("stops checking after it has expired the session", () => {
    const onWarning = vi.fn();
    const onExpired = vi.fn();
    watchIdle({ onWarning, onExpired });

    vi.advanceTimersByTime(IDLE_LIMIT_MS + MINUTE);
    const warnings = onWarning.mock.calls.length;
    vi.advanceTimersByTime(IDLE_LIMIT_MS);
    expect(onExpired).toHaveBeenCalledTimes(1);
    expect(onWarning).toHaveBeenCalledTimes(warnings);
  });

  it("does nothing after the returned stop function is called", () => {
    const onExpired = vi.fn();
    const stop = watchIdle({ onWarning: vi.fn(), onExpired });

    stop();
    vi.advanceTimersByTime(2 * IDLE_LIMIT_MS);
    expect(onExpired).not.toHaveBeenCalled();
  });
});

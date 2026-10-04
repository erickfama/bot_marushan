import { describe, expect, it } from "vitest";

import { duration } from "./App";

describe("duration", () => {
  it("formats milliseconds for the player timeline", () => {
    expect(duration(0)).toBe("0:00");
    expect(duration(65_999)).toBe("1:05");
    expect(duration(3_661_000)).toBe("61:01");
  });

  it("does not display negative time", () => {
    expect(duration(-1_000)).toBe("0:00");
  });
});

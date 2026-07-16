import { strict as assert } from "node:assert";
import { describe, it } from "node:test";

import { scopeCanAuthenticateToMcp } from "./auth-scopes.js";

describe("MCP authentication scopes", () => {
  it("WHY: a sealed-executor key is not an MCP tool principal", () => {
    assert.equal(scopeCanAuthenticateToMcp("sealed_executor"), false);
  });

  it("preserves existing user, agent, system, and connector principals", () => {
    for (const scope of ["user", "agent", "system", "connector"])
      assert.equal(scopeCanAuthenticateToMcp(scope), true);
  });

  it("rejects unknown scopes", () => {
    assert.equal(scopeCanAuthenticateToMcp("future"), false);
  });
});

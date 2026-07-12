/**
 * Tests for #1101 — operator-queue agent-scope post-filter.
 *
 * Pins `filterQueueItemsForAgentScope` so a future edit can't silently widen
 * what an agent-scoped key sees in a broad `list_operator_queue` call (the
 * load-bearing gate: the backend filters to the KEY OWNER's accessible agents,
 * so the MCP layer is the only place agent_permissions are enforced).
 *
 * Runner: built-in `node:test`. No new devDependency. Run via:
 *   node --import tsx --test src/*.test.ts
 */
import { describe, it } from "node:test";
import { strict as assert } from "node:assert";

import {
  filterQueueItemsForAgentScope,
  createOperatorQueueTools,
} from "./tools/operator_queue.js";
import type { TrinityClient } from "./client.js";

type Item = { id: string; agent_name: string };

const items: Item[] = [
  { id: "a", agent_name: "self" },
  { id: "b", agent_name: "friend" },
  { id: "c", agent_name: "stranger" },
  { id: "d", agent_name: "self" },
];

describe("#1101 filterQueueItemsForAgentScope", () => {
  it("keeps only items whose agent is in the allowed set (self + permitted)", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "friend"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "b", "d"]);
  });

  it("a self-only allowed set keeps just the caller's own items", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "d"]);
  });

  it("drops every item whose agent is not permitted", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["nobody"]));
    assert.deepEqual(out, []);
  });

  it("an empty allowed set drops everything", () => {
    assert.deepEqual(filterQueueItemsForAgentScope(items, new Set<string>()), []);
  });

  it("an empty item list returns empty", () => {
    assert.deepEqual(filterQueueItemsForAgentScope([], new Set(["self"])), []);
  });

  it("is a pure filter — does not mutate the input array", () => {
    const snapshot = items.map((i) => ({ ...i }));
    filterQueueItemsForAgentScope(items, new Set(["self"]));
    assert.deepEqual(items, snapshot);
  });

  it("is order-preserving", () => {
    const out = filterQueueItemsForAgentScope(items, new Set(["self", "stranger"]));
    assert.deepEqual(out.map((i) => i.id), ["a", "c", "d"]);
  });
});

// ---------------------------------------------------------------------------
// #1104 — respond_to_operator_queue access gate + proxy behavior.
// Builds the tool with requireApiKey=false so getClient() returns our fake
// client directly. The crux being pinned: an agent-scoped key may NOT resolve
// a non-permitted agent's item, and on denial the write is never attempted.
// ---------------------------------------------------------------------------

function makeRespondTool(fake: Partial<TrinityClient>) {
  const tools = createOperatorQueueTools(fake as unknown as TrinityClient, false);
  return tools.respondToOperatorQueue;
}

const agentCtx = (agentName: string) => ({
  session: { scope: "agent", agentName } as any,
});

describe("#1104 respond_to_operator_queue", () => {
  it("denies an agent-scoped key resolving a non-permitted agent's item — and never writes", async () => {
    let responded = false;
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "stranger" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async () => {
        responded = true;
        return {} as any;
      },
    });

    const out = JSON.parse(
      await tool.execute(
        { item_id: "x", response: "approve" },
        agentCtx("self"),
      ),
    );

    assert.equal(out.error, "Access denied");
    assert.equal(responded, false, "respond must not be called when access is denied");
  });

  it("allows an agent to resolve its own item and proxies the response", async () => {
    const calls: Array<{ id: string; body: any }> = [];
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "self" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async (id: string, body: any) => {
        calls.push({ id, body });
        return { id, status: "responded", agent_name: "self" } as any;
      },
    });

    const out = JSON.parse(
      await tool.execute(
        { item_id: "item1", response: "approve", response_text: "ok" },
        agentCtx("self"),
      ),
    );

    assert.equal(out.status, "responded");
    assert.deepEqual(calls, [
      { id: "item1", body: { response: "approve", response_text: "ok" } },
    ]);
  });

  it("allows resolving a permitted (non-self) agent's item", async () => {
    let responded = false;
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "friend" }) as any,
      getPermittedAgents: async () => ["friend"],
      respondToOperatorQueueItem: async () => {
        responded = true;
        return { status: "responded" } as any;
      },
    });

    const out = JSON.parse(
      await tool.execute({ item_id: "y", response: "deny" }, agentCtx("self")),
    );

    assert.equal(out.status, "responded");
    assert.equal(responded, true);
  });

  it("surfaces a backend 400 (non-pending item) as a structured error, not a throw", async () => {
    const tool = makeRespondTool({
      getOperatorQueueItem: async () => ({ agent_name: "self" }) as any,
      getPermittedAgents: async () => [],
      respondToOperatorQueueItem: async () => {
        throw new Error("API error (400): Cannot respond to item with status 'responded'");
      },
    });

    const out = JSON.parse(
      await tool.execute({ item_id: "z", response: "approve" }, agentCtx("self")),
    );

    assert.match(out.error, /400/);
    assert.match(out.error, /Cannot respond/);
  });
});

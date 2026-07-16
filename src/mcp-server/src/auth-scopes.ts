const MCP_AUTH_SCOPES = new Set(["user", "agent", "system", "connector"]);

export function scopeCanAuthenticateToMcp(scope: unknown): boolean {
  return typeof scope === "string" && MCP_AUTH_SCOPES.has(scope);
}

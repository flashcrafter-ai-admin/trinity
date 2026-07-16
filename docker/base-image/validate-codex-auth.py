#!/usr/bin/python3
"""Validate the exact ChatGPT subscription auth shape from standard input."""

from __future__ import annotations

import json
import sys


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def fail() -> None:
    raise SystemExit("invalid Codex ChatGPT subscription auth")


payload = sys.stdin.buffer.read(262_145)
if not 1 <= len(payload) <= 262_144:
    fail()
try:
    auth = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object)
except (UnicodeDecodeError, ValueError, TypeError):
    fail()
if not isinstance(auth, dict) or set(auth) - {
    "OPENAI_API_KEY",
    "auth_mode",
    "last_refresh",
    "tokens",
}:
    fail()
if auth.get("OPENAI_API_KEY") is not None or auth.get("auth_mode") != "chatgpt":
    fail()
if auth.get("last_refresh") is not None and not isinstance(auth["last_refresh"], str):
    fail()
tokens = auth.get("tokens")
allowed_tokens = {"access_token", "account_id", "id_token", "refresh_token"}
if not isinstance(tokens, dict) or set(tokens) - allowed_tokens:
    fail()
for required in ("access_token", "refresh_token"):
    if not isinstance(tokens.get(required), str) or not tokens[required].strip():
        fail()
for optional in ("account_id", "id_token"):
    if tokens.get(optional) is not None and (
        not isinstance(tokens[optional], str) or not tokens[optional].strip()
    ):
        fail()

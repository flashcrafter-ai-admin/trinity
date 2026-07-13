"""
Slack HTTP webhook transport — inbound POST from Slack.

Requires:
- Public URL (Tailscale Funnel, ngrok, production domain)
- Signing Secret for request verification
- Event Subscriptions configured in Slack App

Fallback transport for production deployments.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional, Tuple

from fastapi import Request

from adapters.transports.base import ChannelTransport
from services.deployment_lock_service import spawn_governed_mutation

logger = logging.getLogger(__name__)


class SlackWebhookTransport(ChannelTransport):
    """Slack HTTP webhooks — inbound POST, needs public URL."""

    def __init__(self, signing_secret: str, adapter, router):
        super().__init__(adapter, router)
        self.signing_secret = signing_secret

    async def start(self) -> None:
        """No-op: webhook transport is passive (FastAPI endpoint handles requests)."""
        self._running = True
        logger.info("Slack webhook transport ready")

    async def stop(self) -> None:
        """No-op: nothing to clean up."""
        self._running = False

    async def handle_http_request(self, request: Request) -> dict:
        """
        Called by the FastAPI endpoint when Slack sends a webhook.

        Returns a dict to be returned as the HTTP response.
        Always returns 200 to Slack (errors logged internally).
        """
        body = await request.body()

        # Verify signature
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")

        is_valid, error = self._verify_signature(timestamp, body, signature)
        if not is_valid:
            logger.warning(f"Slack signature verification failed: {error}")
            return {"ok": False, "challenge": None}

        # Parse event
        try:
            event_data = json.loads(body)
        except Exception as e:
            logger.error(f"Failed to parse Slack event: {e}")
            return {"ok": False, "challenge": None}

        event_type = event_data.get("type")

        # Handle URL verification challenge
        if event_type == "url_verification":
            challenge = event_data.get("challenge")
            logger.info("Slack URL verification challenge received")
            return {"ok": True, "challenge": challenge}

        # Handle event callback — process async, return 200 immediately
        if event_type == "event_callback":
            spawn_governed_mutation(self.on_event(event_data))

        return {"ok": True, "challenge": None}

    def _verify_signature(
        self,
        timestamp: str,
        body: bytes,
        signature: str
    ) -> Tuple[bool, Optional[str]]:
        """Verify that a request came from Slack using the signing secret."""
        if not self.signing_secret:
            return False, "Slack Signing Secret not configured"

        # Reject requests older than 5 minutes (prevent replay attacks)
        try:
            request_timestamp = int(timestamp)
            if abs(time.time() - request_timestamp) > 60 * 5:
                return False, "Request timestamp too old"
        except (ValueError, TypeError):
            return False, "Invalid timestamp"

        # Compute expected signature
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        expected_signature = 'v0=' + hmac.new(
            self.signing_secret.encode('utf-8'),
            sig_basestring.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        # Constant-time comparison
        if not hmac.compare_digest(expected_signature, signature):
            return False, "Invalid signature"

        return True, None

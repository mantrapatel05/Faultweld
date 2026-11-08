"""Execution and independent verification boundary for Faultweld Lite."""

from app.gateway.gateway import Gateway, GatewayResult
from app.gateway.verifier import VerifierConfig, verify

__all__ = ["Gateway", "GatewayResult", "VerifierConfig", "verify"]

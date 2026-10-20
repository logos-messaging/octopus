"""GitHub integration: App auth, webhook verification, JIT runner configs."""

from .app import GitHubApiError, GitHubApp
from .trust import TrustInputs, resolve_trust_inputs
from .webhooks import (
    DeliveryLog,
    PayloadError,
    WebhookReceiver,
    WebhookResponse,
    WorkflowJobEvent,
    parse_workflow_job,
    verify_signature,
)

__all__ = [
    "DeliveryLog",
    "GitHubApiError",
    "GitHubApp",
    "PayloadError",
    "TrustInputs",
    "WebhookReceiver",
    "WebhookResponse",
    "WorkflowJobEvent",
    "parse_workflow_job",
    "resolve_trust_inputs",
    "verify_signature",
]

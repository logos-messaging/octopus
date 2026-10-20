"""GitHub integration: App auth, webhook verification, JIT runner configs."""

from .app import GitHubApiError, GitHubApp, GitHubClient
from .runners import (
    JitRunner,
    JitRunnerError,
    JitRunners,
    RegisteredRunner,
    make_jit_provider,
    runner_name,
)
from .token import ResolvedToken, TokenClient, TokenError, resolve_token
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
    "GitHubClient",
    "JitRunner",
    "JitRunnerError",
    "JitRunners",
    "PayloadError",
    "RegisteredRunner",
    "ResolvedToken",
    "TokenClient",
    "TokenError",
    "TrustInputs",
    "WebhookReceiver",
    "WebhookResponse",
    "WorkflowJobEvent",
    "make_jit_provider",
    "parse_workflow_job",
    "resolve_token",
    "resolve_trust_inputs",
    "runner_name",
    "verify_signature",
]

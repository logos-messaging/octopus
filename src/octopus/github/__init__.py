"""GitHub integration: App auth, webhook verification, JIT runner configs."""

from .app import GitHubApiError, GitHubApp
from .runners import (
    JitRunner,
    JitRunnerError,
    JitRunners,
    RegisteredRunner,
    make_jit_provider,
    runner_name,
)
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
    "JitRunner",
    "JitRunnerError",
    "JitRunners",
    "PayloadError",
    "RegisteredRunner",
    "TrustInputs",
    "WebhookReceiver",
    "WebhookResponse",
    "WorkflowJobEvent",
    "make_jit_provider",
    "parse_workflow_job",
    "resolve_trust_inputs",
    "runner_name",
    "verify_signature",
]

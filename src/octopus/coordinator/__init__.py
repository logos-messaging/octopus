"""Coordinator: runs on VPS hosts. Receives GitHub webhooks, schedules jobs onto
runners, tracks the job lifecycle and serves logs."""

from .cluster import ClusterConfig, CoordinatorCluster, to_coord_job, to_job_context
from .logs import LogCollector, LogState, LogStore
from .offers import OfferOutcome, OfferResult, OfferSender
from .registry import Entry, MemoryStore, Registry, RegistryStore
from .scheduler import JitConfigError, LogSink, PendingJob, Scheduler, SchedulerConfig

__all__ = [
    "ClusterConfig",
    "CoordinatorCluster",
    "Entry",
    "JitConfigError",
    "LogCollector",
    "LogSink",
    "LogState",
    "LogStore",
    "MemoryStore",
    "OfferOutcome",
    "OfferResult",
    "OfferSender",
    "PendingJob",
    "Registry",
    "RegistryStore",
    "Scheduler",
    "SchedulerConfig",
    "to_coord_job",
    "to_job_context",
]

# Multi-coordinator coordination and failover
**Labels:** coordinator, transport

## Tasks
- Decide webhook ownership: GitHub delivers to one URL; document/implement the two options (load balancer in front of coordinators, or one active coordinator plus standby) and pick one.
- Gossip on `COORD_TOPIC`: heartbeats, job assignment claims (so two coordinators never assign the same job), takeover when a coordinator dies mid-offer.
- Registry/state convergence between coordinators after a partition.

## Acceptance
- Killing the active coordinator mid-run does not lose or double-assign any queued job (integration test).

## Depends on
02, 08, 09.

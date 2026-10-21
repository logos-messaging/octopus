# Integration harness

An in-process cluster for testing CI coordination end to end: N coordinators and M runners
on one `MemoryBus`, with real scheduler, registry, encrypted job channel, trust policy, job
store, log streaming, webhook receiver and `RunnerAgent`. Faked: GitHub (JIT configs) and the
`actions/runner` process (`FakeManager`).

```bash
pytest tests/test_integration_scenarios.py
```

```python
cluster = Cluster(tmp_path, coordinators=1)
cluster.add_runner("a", Behavior(outcome="hang"))   # "ok" | "fail" | "hang"
cluster.coordinator.deliver(1)                      # signed workflow_job.queued webhook
cluster.tick()                                      # one scheduler pass, by hand
wait_until(lambda: ..., what="...")                 # runners run on their own threads
cluster.clock.advance(3600)                         # expire presence without sleeping
```

- The coordinator is never started on a thread: `cluster.tick()` is the only driver, so
  scenarios are deterministic. Runner jobs run on `RunnerAgent`'s threads.
- `FakeClock` is real time plus an offset (runners validate offer expiry on the real clock).
- `LossyTransport` drops, duplicates or delays a node's channel sends; `mute = True` loses
  them all (a runner that receives offers but whose acks never arrive).
- Assert on `coordinator.job(id)` (state and `reason`) and `coordinator.store.timeline(id)`:
  that is what `octopus explain` prints.

Not covered here, and tested elsewhere:

- a real `liblogosdelivery` round trip: the `needs_lib` tests (`tests/test_node_connectivity.py`,
  `tests/test_channel_integration.py`); CI job `needs_lib` runs them on Linux and macOS.
- the NAT relay path: `tests/nat/` runs two runners on separate internal networks that can only
  reach each other through the coordinator. CI job `nat` runs it; to run it by hand with a Linux
  `liblogosdelivery.so`:

  ```bash
  LIB=path/to/liblogosdelivery.so docker compose -f tests/nat/docker-compose.yml up -d --build
  docker wait "$(docker compose -f tests/nat/docker-compose.yml ps -aq runner_b)"   # 0 = relayed
  docker compose -f tests/nat/docker-compose.yml down -v
  ```

Coordinator failover has in-process scenarios here (`test_coordinator_failover_*`).

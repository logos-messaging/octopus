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

Not covered here: a real `liblogosdelivery` round trip (see the `needs_lib` tests in
`tests/test_channel_integration.py`), the docker-compose NAT test (`tests/nat/`), and
coordinator failover (#17).

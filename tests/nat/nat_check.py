"""Runs inside the compose containers. Exit code 0 means the relay path works.

coordinator: starts a node and stays up, writing its multiaddr to /shared/entry.
sender/receiver: wait for the entry file, start a runner node and exchange one message;
the sender keeps publishing until the receiver confirms through /shared/done.
"""

import base64
import json
import os
import sys
import time

from octopus.transport import config
from octopus.transport.logos import _load_bindings

ld = _load_bindings()
TOPIC = "/octopus/1/nat-check/proto"
ROLE = os.environ["ROLE"]
ENTRY_FILE = "/shared/entry"
DONE_FILE = "/shared/done"


def run_topic(entry: str) -> tuple[str, bytes]:
    """The nodes also join the public network, so use a topic and payload of this run only."""
    run = entry.rsplit("/", 1)[-1][-12:]
    return f"/octopus/1/nat-check-{run}/proto", f"via-relay {run}".encode()


def run_coordinator() -> None:
    node = ld.NodeWrapper.create_and_start(
        config.coordinator_config(os.environ["OCTOPUS_PUBLIC_IP"])
    ).unwrap()
    addrs = node.get_node_info("MyMultiaddresses").unwrap().split(",")
    entry = next(a for a in addrs if "/tcp/" in a)
    # The relay node must be subscribed to the topic to forward it between the runners.
    node.subscribe_content_topic(run_topic(entry)[0]).unwrap()
    os.makedirs("/shared", exist_ok=True)
    with open(ENTRY_FILE, "w") as f:
        f.write(entry)
    time.sleep(10**6)


def wait_entry() -> str:
    for _ in range(120):
        if os.path.exists(ENTRY_FILE):
            with open(ENTRY_FILE) as f:
                return f.read()
        time.sleep(1)
    sys.exit("coordinator entry node never appeared")


def say(msg: str) -> None:
    print(f"NATCHECK {ROLE}: {msg}", flush=True)


def run_runner() -> None:
    entry = wait_entry()
    topic, expected = run_topic(entry)
    got = []

    def on_event(ret, msg):
        e = json.loads(msg)
        if e.get("eventType") == "message_received":
            got.append(base64.b64decode(e["message"]["payload"]))

    node = ld.NodeWrapper.create_and_start(
        config.runner_config([entry]), event_cb=on_event
    ).unwrap()
    node.subscribe_content_topic(topic).unwrap()
    say(f"started, entry: {entry}")
    time.sleep(10)
    if ROLE == "sender":
        payload = base64.b64encode(expected).decode()
        # The relay mesh can take a while to form: keep publishing until the receiver is done.
        for attempt in range(1, 31):
            if os.path.exists(DONE_FILE):
                say(f"receiver confirmed after {attempt - 1} publishes")
                return
            node.send_message(
                {"contentTopic": topic, "payload": payload, "ephemeral": False}
            ).unwrap()
            say(f"published, attempt: {attempt}")
            time.sleep(3)
        sys.exit("receiver never confirmed")
    deadline = time.time() + 120
    while expected not in got and time.time() < deadline:
        time.sleep(0.5)
    ok = expected in got
    say(f"received: {ok}, messages seen: {len(got)}")
    if ok:
        open(DONE_FILE, "w").close()
    # Interpreter teardown segfaults in liblogosdelivery, which would hide the result.
    os._exit(0 if ok else 1)


(run_coordinator if ROLE == "coordinator" else run_runner)()

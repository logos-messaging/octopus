"""Runs inside the compose containers. Exit code 0 means the relay path works.

coordinator: starts a node and stays up, writing its multiaddr to /shared/entry.
sender/receiver: wait for the entry file, start a runner node and exchange one message.
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


def run_coordinator() -> None:
    node = ld.NodeWrapper.create_and_start(
        config.coordinator_config(os.environ["OCTOPUS_PUBLIC_IP"])
    ).unwrap()
    addrs = node.get_node_info("MyMultiaddresses").unwrap().split(",")
    os.makedirs("/shared", exist_ok=True)
    with open(ENTRY_FILE, "w") as f:
        f.write(next(a for a in addrs if "/tcp/" in a))
    time.sleep(10**6)


def wait_entry() -> str:
    for _ in range(120):
        if os.path.exists(ENTRY_FILE):
            with open(ENTRY_FILE) as f:
                return f.read()
        time.sleep(1)
    sys.exit("coordinator entry node never appeared")


def run_runner() -> None:
    got = []

    def on_event(ret, msg):
        e = json.loads(msg)
        if e.get("eventType") == "message_received":
            got.append(base64.b64decode(e["message"]["payload"]))

    node = ld.NodeWrapper.create_and_start(
        config.runner_config([wait_entry()]), event_cb=on_event
    ).unwrap()
    node.subscribe_content_topic(TOPIC).unwrap()
    time.sleep(10)
    if ROLE == "sender":
        payload = base64.b64encode(b"via-relay").decode()
        node.send_message({"contentTopic": TOPIC, "payload": payload, "ephemeral": False}).unwrap()
        return
    deadline = time.time() + 60
    while not got and time.time() < deadline:
        time.sleep(0.5)
    sys.exit(0 if got == [b"via-relay"] else 1)


(run_coordinator if ROLE == "coordinator" else run_runner)()

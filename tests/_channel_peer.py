"""Second node for test_channel_integration: own process, so own working dir (SDS state)."""

import base64
import json
import sys
import time

from octopus.crypto import PairCipher
from octopus.transport.logos import LogosConfig, LogosTransport

args = json.loads(sys.argv[1])
node = LogosTransport(LogosConfig(entry_nodes=[args["entry"]], overrides=args["overrides"]))
node.start()
try:
    cipher = PairCipher(base64.b64decode(args["key"]))
    node.open_channel(
        args["channel"],
        args["topic"],
        "bob",
        cipher,
        lambda sender, payload: print("GOT", sender, payload.decode(), flush=True),
    )
    print("READY", flush=True)
    time.sleep(args["lifetime"])
finally:
    node.stop()

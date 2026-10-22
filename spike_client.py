"""Phase 0 spike, hosted side: install time is measured by the workflow; this measures the rest."""

import json
import os
import sys
import time
import uuid

from octopus.crypto import (
    PairCipher,
    decode_public_key,
    derive_channel_key,
    encode_public_key,
    generate_private_key,
)
from octopus.transport.config import runner_config
from octopus.transport.logos import LogosTransport

TOPIC = "/octopus/1/spike/proto"
mode = os.environ.get("SPIKE_MODE", "Edge")
sid = uuid.uuid4().hex[:8]
t0 = time.monotonic()
result = {"mode": mode, "id": sid}


def log(msg):
    print(f"[{time.monotonic() - t0:7.1f}s] {msg}", flush=True)


key = generate_private_key()
pub = encode_public_key(key.public_key())
cfg = {**runner_config(entry_nodes=[]), "mode": mode}
transport = LogosTransport(cfg)
pong = {}
chan_echo = {}

try:
    t = time.monotonic()
    transport.start()
    result["node_start_s"] = round(time.monotonic() - t, 2)
    log(f"node started in {result['node_start_s']}s")
    transport.subscribe(
        TOPIC,
        lambda _t, p: (lambda m: pong.setdefault("msg", (time.monotonic(), m)) if m.get("t") == "pong" and m.get("id") == sid else None)(json.loads(p)),
    )
    t = time.monotonic()
    connected = transport.wait_connected(120)
    result["connected"] = bool(connected)
    result["connect_s"] = round(time.monotonic() - t, 2)
    log(f"connected={connected} after {result['connect_s']}s")

    # topic round trip: ping every 2 s until the laptop answers
    first_ping = time.monotonic()
    deadline = first_ping + float(os.environ.get("SPIKE_WAIT", "150"))
    seq = 0
    while "msg" not in pong and time.monotonic() < deadline:
        seq += 1
        transport.publish(TOPIC, json.dumps({"t": "ping", "id": sid, "seq": seq, "mode": mode, "pub": pub}).encode())
        time.sleep(2)
    if "msg" in pong:
        when, msg = pong["msg"]
        result["topic_first_pong_s"] = round(when - first_ping, 2)
        log(f"topic round trip ok: first pong {result['topic_first_pong_s']}s after the first ping (ping {seq})")
        # channel round trip
        chan = f"octopus-spike-{sid}"
        cipher = PairCipher(derive_channel_key(key, decode_public_key(msg["pub"]), chan))
        transport.open_channel(
            chan,
            f"/octopus/1/spike-{sid}/proto",
            "hosted",
            cipher,
            lambda _s, data: chan_echo.setdefault("t", time.monotonic()) if data.startswith(b"chan-echo:") else None,
        )
        start = time.monotonic()
        tries = 0
        while "t" not in chan_echo and time.monotonic() - start < 60:
            tries += 1
            transport.send_on_channel(chan, b"hello-%d" % tries)
            time.sleep(2)
        if "t" in chan_echo:
            result["channel_first_echo_s"] = round(chan_echo["t"] - start, 2)
            log(f"channel round trip ok: {result['channel_first_echo_s']}s after the first send ({tries} sends)")
        else:
            log("channel round trip FAILED: no echo in 60s")
    else:
        log("topic round trip FAILED: no pong")
except Exception as e:  # report, do not hide, what failed
    result["error"] = f"{type(e).__name__}: {e}"
    log(f"error: {result['error']}")
finally:
    try:
        transport.stop()
    except Exception:
        pass
    result["total_s"] = round(time.monotonic() - t0, 2)
    print("SPIKE_RESULT " + json.dumps(result), flush=True)
sys.exit(0)

# Reliable job channel with end-to-end encryption
**Labels:** transport, security

## Context
The JIT runner config is a secret that grants a runner registration. It must reach only the chosen runner, with acks and retries.

## Tasks
- Define key management: each runner has a keypair; the public key is announced in `Presence` (add a field) and pinned by the coordinator after first sight (or via `octopus runner allow-coordinator`).
- Envelope encryption for `JobOffer.jit_config_enc` (e.g. libsodium sealed box / X25519 + XChaCha20-Poly1305); replay protection via `job_id` + expiry.
- Use logos-delivery reliable channels (`channel_create/send`, with the encrypt/decrypt callbacks) for `jobs-<runnerId>`, or message-level acks if the channel API is not yet usable from Python (document the decision).
- Ensure secrets never appear in logs or exceptions.

## Acceptance
- A different runner cannot decrypt an offer; a tampered offer is rejected; an expired offer is rejected.

## Tests
Crypto unit tests; end-to-end offer/ack round trip over `MemoryTransport`.

## Depends on
01, 02.

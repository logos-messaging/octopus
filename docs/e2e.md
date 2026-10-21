# End-to-end test against a real GitHub repo

`tests/e2e/` runs Octopus against a real GitHub repo. The `E2E nightly` workflow
(`.github/workflows/e2e.yml`) runs it every night and on demand.

## What it does

1. `octopus github setup` adds a webhook to the e2e repo and writes a `coordinator.env`.
2. `octopus coordinator start` and `octopus runner start` run on the same machine, with the
   real liblogosdelivery. The runner connects to the coordinator as its entry node.
3. The test pushes a new branch `octopus-e2e/pass/<time>-<pid>`. The push starts the
   workflow `octopus-e2e-pass` (job `pass`) on `runs-on: [self-hosted, octopus]`. When
   that job is complete, the test pushes `octopus-e2e/fail/<time>-<pid>`. That starts
   `octopus-e2e-fail` (job `fail`, a step exits with code 3).
4. The test makes sure that:
   - GitHub completes `pass` with `success` and `fail` with `failure`.
   - `octopus explain` shows `pass` as `finished` with no failure, and `fail` as `failed`
     with reason `job-failed`, the detail `job fail completed with result: Failed` and
     the step `fail`.
   - `octopus logs` has the runner output of `pass`, from `Running job: pass` to its result.
   - The two JIT runners are no longer registered on the repo.
5. At the end, the test stops the processes, deletes the branches, and runs
   `octopus github teardown`.

The jobs run one after the other. GitHub gives a queued job to any idle runner with its
labels, so two jobs at the same time can each take the JIT runner that Octopus made for
the other job (#58). A job that waits too long for a free runner also fails (#57).

A failed scheduled run adds a comment to the open issue `E2E nightly failed`, or opens it.
The coordinator and runner logs are in the `e2e-logs` artifact of the run.

## How the webhook gets to the coordinator

A CI machine has no public address, so GitHub cannot send the webhook to it. The webhook
URL is `https://octopus-e2e.invalid/github/webhook`, which does not resolve. GitHub records
each delivery that fails, and the deliveries API gives its headers and payload. The relay
(`tests/e2e/relay.py`) reads the new `workflow_job` deliveries every 2 seconds and sends
them to the local webhook endpoint.

GitHub signs the compact JSON form of the payload. The relay makes the same bytes again, so
the coordinator checks the signature that GitHub made. If the signature does not agree, the
relay signs the body with the secret from `coordinator.env` and counts it as re-signed.
Thus the test does not include the HTTPS connection from GitHub to a public coordinator.

## Set up the e2e repo (one time)

Do these steps as an admin of the e2e repo and of this repo.

1. Make a private repo for the test only, for example `logos-messaging/octopus-e2e`.
   Its jobs run on the machine of the nightly job, so do not use a repo with other work.
2. Copy `tests/e2e/workflows/*.yml` to `.github/workflows/` on its default branch.
   The test compares the files and stops if they are different. Copy them again when
   they change here. The token cannot do this, because it has no Workflows permission.
3. Make a fine-grained token that can only access the e2e repo, with these permissions:

   | Permission | Access | The test uses it to |
   |---|---|---|
   | Administration | read and write | make JIT runners, and list and remove runners |
   | Webhooks | read and write | add and remove the webhook, read its deliveries |
   | Contents | read and write | make the trigger branch, read the workflow files |
   | Actions | read and write | read runs and jobs, cancel runs that are left |
   | Metadata | read | read the repo and the permission of the sender |

   The owner of the token must have write access to the e2e repo. The coordinator trusts
   the sender of the push only if the sender can push to the repo.
4. In this repo, go to Settings, then Secrets and variables, then Actions:
   - Add the variable `OCTOPUS_E2E_REPO` with the name of the e2e repo (owner/name).
   - Add the secret `OCTOPUS_E2E_TOKEN` with the token.

Until the variable is set, the nightly job does not run. When the variable is set and the
secret is missing, the job fails.

## Run it on your machine

You need the native library (see "Develop" in the README) and a repo of your own, set up
as above. A classic token or the `gh` login also works if it has the `repo` scope.

```bash
pip install -e '.[dev,logos]'
OCTOPUS_E2E_REPO=you/octopus-e2e OCTOPUS_E2E_TOKEN=$(gh auth token) pytest -m e2e -v
```

Optional settings:
- `OCTOPUS_E2E_DATA_DIR` is the runner data dir. Use the same dir each time, so that the
  runner does not download `actions/runner` again.
- `OCTOPUS_E2E_ARTIFACTS` is the dir for `coordinator.log` and `runner.log`.

Without `OCTOPUS_E2E_REPO` or `OCTOPUS_E2E_TOKEN`, the test is skipped. With
`OCTOPUS_E2E_REQUIRED=1` (the nightly job sets it), the test fails instead.

## Limits

- The nightly job runs on Linux only.
- Two e2e runs must not use the same repo at the same time. The workflow has a
  concurrency group for this.
- A local run took 2 to 8 minutes. The first run takes some minutes more, because the
  runner downloads `actions/runner`.

# GitHub payload fixtures

Real payloads recorded by GitHub, unmodified. Tests load them and vary single fields.

| File | Source |
|---|---|
| `workflow_job.*.json`, `ping.json` | webhook deliveries from [octokit/webhooks](https://github.com/octokit/webhooks/tree/main/payload-examples/api.github.com) (`workflow_job/*`, `ping/with-app_id`), MIT |
| `workflow_run.json` | `GET /repos/{owner}/{repo}/actions/runs/{run_id}` example from [github/rest-api-description](https://github.com/github/rest-api-description) (`components.examples.workflow-run`), MIT |

# Demo

The demo runs the application on this machine with synthetic data. The default stack from `compose.yaml` runs in production mode and does not run the demo. The demo is an overlay: `scripts/demo.sh` starts it with `compose.demo.yaml`, which sets demo mode and the synthetic dataset for `api` and `worker`. It has its own Compose project, `safe-demo`. It does not share containers, networks or volumes with the local stack from the [Quick start](./quick-start.md). It listens on `127.0.0.1` only. Only one stack can use `127.0.0.1:8443` at a time, so stop the other stack first.

Do not use the demo for real data. Its credentials are for the demo only.

## Requirements

- Docker with Compose v2.
- `curl`, and `uv` for the `cli-show` command. `uv` runs the CLI on this machine from `backend/`, after `uv sync` in that folder.

## Set the demo folder

`SAFE_DEMO_DIR` is required. It has no default. Name a directory of your choice. It must be an absolute path, and it must be outside the repository.

```sh
export SAFE_DEMO_DIR="$HOME/safe-demo"
```

The script creates the directory and a `credentials` folder inside it, with mode 0700. The seed writes one credential file per demo role into that folder, with mode 0600, for example `demo-admin.credential`. The credentials are not in the repository. They are not printed, logged or passed as command arguments. Keep the folder outside the repository.

## Commands

```sh
scripts/demo.sh start --timeout 300   # build, start, seed once, and wait for readiness
scripts/demo.sh status                # service states, readiness, and which credential files exist
scripts/demo.sh cli-show <CASE_ID>    # read one case with the CLI, as a demo role
```

- `start` builds the stack, seeds the demo data once and waits until the gateway and the API report ready. `--timeout` is the wait in seconds (default 300).
- `status` shows the containers, whether the demo is ready, and whether each credential file is present.
- `cli-show` reads one case through the CLI. The sample case in the seed is `5afe0d00-0000-4000-8000-000000000201` (a tool call outside the approved scope). The default role is `demo-investigator`.

Sign in in the browser at https://127.0.0.1:8443. Accept the development certificate first (see the [Quick start](./quick-start.md), step 5).

## Demo roles

Each demo role has its own credential. The sign-in page lists the roles, and a banner shows **DEMO / SYNTHETIC DATA**.

| Demo role (username) | Role | Use it to see |
|---|---|---|
| `demo-admin` | `admin` | Users, roles, privacy inventory, settings |
| `demo-reporter` | `reporter` | The cases it opened |
| `demo-triager` | `triager` | Every case, including reporter-channel cases with no assignment |
| `demo-investigator` | `investigator` | Its assigned cases and their evidence |
| `demo-reviewer` | `reviewer` | Its assigned cases and their reviews |
| `demo-approver` | `disclosure_approver` | Its packet: the versions it may approve, and the versions it approved |
| `demo-auditor` | `auditor` | Overview counts and the catalog |

To sign in, pick the role in the sign-in page and type the credential from its file in `SAFE_DEMO_DIR/credentials`. Open the file, copy the value, and close the file. The script never prints it.

## Egress check

The demo application and worker have no route out. The check below runs inside the API container and tries outbound connections that must all fail. Run it with the demo stack up:

```sh
docker compose -p safe-demo -f compose.yaml -f compose.demo.yaml exec -T api sh -s < scripts/demo-egress-check.sh
```

The output has one line per check and ends with `PASS`, or `FAIL` when a check had an unexpected result.

## What has been run

- Run live on the owner's stack on 2026-11-03: sign-in, the case list, the case detail (`cli-show`) and the egress check.
- The `start` and `cli-show` steps were run on the owner's live stack on 2026-11-03. The browser sign-in step has not been recorded yet.

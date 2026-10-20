# Deploy: secrets and listeners on the VPS (KTD28)

This page is the rule for which service on the VPS (the virtual private server Slipway runs on) receives which secret, and which ports are reachable from outside. `compose.yaml` arrives with the deploy cards; whatever it says must match this page.

## Where secrets live

All secret values live in one environment file (a text file of `NAME=value` lines that compose passes to a service) owned by root, mode 600, outside the compose directory. Nothing in a repository holds a value.

The operator file (`$SLIPWAY_HOME/operator.toml`) never holds a value either. It names each credential by the environment variable that holds it, and a project file names credentials only by their operator file entry (R3). So the variable names in the second table below are whatever the operator chose in the operator file; Slipway fixes only the names in the first table.

## Variables Slipway itself reads

| Variable | Read by | What it is |
|:-|:-|:-|
| `SLIPWAY_HOME` | app, worker | The configuration root. Holds `operator.toml` and `projects/<slug>.toml`. Defaults to `~/.slipway`. |
| `SLIPWAY_DATABASE_URL` | app, worker | The Postgres connection for the `slipway` database only, never Hatchet's. |
| `HATCHET_CLIENT_TOKEN` | app, worker | The Hatchet client token. The app uses it to start runs and push events; the worker to receive tasks. |

## Which service receives which variables

| Compose service | Receives | Does not receive |
|:-|:-|:-|
| `app` (MCP routes, status page, webhooks) | `SLIPWAY_DATABASE_URL`, `HATCHET_CLIENT_TOKEN`, the identity provider settings (the operator file's `identity_provider.client_secret_env`), every webhook secret (`webhook_secrets[].env` and `previous_env`) | Account tokens, tracker and host credentials, Langfuse keys, runner registration tokens |
| `worker` (card workflow, watch, runner cron) | `SLIPWAY_DATABASE_URL`, `HATCHET_CLIENT_TOKEN`, and the rest of the operator file's variables: `accounts.*.env`, `tracker_credentials.*.token_env` and `email_env`, `host_credentials.*.env`, `langfuse_projects.*.public_key_env` and `secret_key_env` | Runner registration tokens |
| runner containers (one per instance) | Only that instance's registration tokens, `runner_instances.<name>.repositories[].token_env` and `previous_token_env` | Everything else |
| builder containers (one per card, started by the worker) | Only the account token for the card's binding and the git credential through a credential helper (KTD12) | Everything else |
| `hatchet-lite`, `postgres`, `dagger` | Their own settings only. The Hatchet default admin password is replaced on first boot. | Every Slipway secret |

## `SLIPWAY_HOME` mounts

| Service | Mount |
|:-|:-|
| `app` | `projects/` writable (onboard saves project files), `operator.toml` absent |
| `worker` | Read only, `operator.toml` present |

## Published ports

| Listener | Reachable from |
|:-|:-|
| App, port 443 | The internet |
| Hatchet gRPC, behind TLS with a token per worker | The internet, for the laptop worker. This is the fourth public listener counted in KTD28. |
| Hatchet dashboard and API | Compose network only |
| Runner read API | Compose network only |
| Postgres | Compose network only |
| Dagger engine | Compose network only |

## Open question for the operator

KTD28 keeps the operator file out of the app, yet the app needs two things that today live only there: the operator's OAuth subject (the page admits only that subject, KTD13) and the map from repository to webhook secret variable (KTD10). U6 does not settle this. The candidates are giving the app a reduced copy of those two entries, or fixed variable names for them. The deciding card is the one that builds the app (U18 or U20).

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
| `app` | `projects/` writable (onboard saves project files), and `operator.toml` alone as a read only file mount |
| `worker` | Read only, `operator.toml` present |

A bind mount maps one path on the VPS into a container. The app gets two: the `projects/` directory, writable, and the single file `operator.toml`, read only, both under the same `SLIPWAY_HOME` path inside the container so `load_operator()` and `load_project()` find them unchanged. The next section says why the app reads the operator file at all.

## Published ports

| Listener | Reachable from |
|:-|:-|
| App, port 443 | The internet |
| Hatchet gRPC, behind TLS with a token per worker | The internet, for the laptop worker. This is the fourth public listener counted in KTD28. |
| Hatchet dashboard and API | Compose network only |
| Runner read API | Compose network only |
| Postgres | Compose network only |
| Dagger engine | Compose network only |

## Creating the `slipway` database

The `slipway` database must use UTF8 encoding (the character set Postgres stores text in). A database created under the C locale defaults to SQL_ASCII, which stores bytes without checking them, and psycopg then hands Slipway bytes where it expects text. Create it with the encoding named, from `template0` because the default template may carry another one:

```sh
createdb -E UTF8 -T template0 slipway
```

The official Postgres image initialises its cluster as UTF8, so a database it creates through `POSTGRES_DB` is already right; any other route names the encoding as above. Check with `SHOW server_encoding;` inside the database. The integration tests run against a throwaway database that may be SQL_ASCII and survive it by asking for `client_encoding=utf8` on every connection; production does not rely on that.

## What the app reads from the operator file

Decided by #39. The app mounts the operator file read only and loads it with the same `load_operator()` the worker uses. It is the one source for everything the app needs from it:

- `operator_subject`, the OAuth subject (the stable user id the identity provider puts in a token) the status page admits and the MCP verifier knows as the operator's (KTD13).
- `identity_provider`, the issuer and client id the verifier pins, and the name of the variable holding the client secret.
- `webhook_secrets`, the map from each repository to the variable holding its webhook secret and, during a rotation, the previous one (KTD10). The webhook endpoint builds U9's `secrets_for(repository)` from it: the entry for the payload's repository, each named variable read from the app's environment, and no entry meaning a repository Slipway does not manage.
- The entry names onboard checks a project file against before saving it (`check_names`, U6), since onboard runs in the app and writes `projects/`.

What keeps the app narrow is its environment, not the file. The operator file holds no secret value (R3, and `refuse_secrets` refuses one), so reading it tells the app only which variable names exist. The values arrive by environment, and the app's compose service receives only its own, as the table above lists. An app that tried to resolve an account token or a host credential would find the variable unset and fail, as `bindings.py` already does for an unset variable.

The file is read when the app starts. Adding a repository or rotating a secret changes the environment file too, and compose hands a container its environment only when it creates the container, so either change is one `docker compose up -d app` after both files are edited.

Two other mechanisms were weighed and set aside:

- **A derived file the worker writes**, holding only the app's entries. The worker's `SLIPWAY_HOME` is read only, so it would need a second writable volume, and the app could start before the file exists or read one older than the operator file. Narrowing the file buys nothing when it holds no values.
- **Two app only variables**, the subject and the secret map packed into one string. They copy what the operator file already says into a second place that can drift from it, they leave out the identity provider and the names onboard checks, and a map packed into one variable has to be parsed by hand.

U18 (#21) loads the subject and identity provider this way, and U9 (#12) takes `secrets_for` as a callable, so it needs nothing from this decision beyond the endpoint that builds it.

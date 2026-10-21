# Deploy: secrets and listeners on the VPS (KTD28)

This page is the rule for which service on the VPS (the virtual private server Slipway runs on) receives which secret, and which ports are reachable from outside. `compose.yaml` must match it.

## Where secrets live

All secret values live in one environment file (a text file of `NAME=value` lines that compose passes to a service) owned by root, mode 600, outside the compose directory. Nothing in a repository holds a value.

The operator file (`$SLIPWAY_HOME/operator.toml`) never holds a value either. It names each credential by the environment variable that holds it, and a project file names credentials only by their operator file entry (R3). So the variable names in the second table below are whatever the operator chose in the operator file; Slipway fixes only the names in the first table.

## Variables Slipway itself reads

| Variable | Read by | What it is |
|:-|:-|:-|
| `SLIPWAY_HOME` | app, worker | The configuration root. Holds `operator.toml` and `projects/<slug>.toml`. Defaults to `~/.slipway`. |
| `SLIPWAY_DATABASE_URL` | app, worker | The Postgres connection for the `slipway` database only, never Hatchet's. |
| `HATCHET_CLIENT_TOKEN` | app, worker | The Hatchet client token. The app uses it to start runs and push events; the worker to receive tasks. |
| `SLIPWAY_SESSION_KEY` | app | The key that signs the status page's cookies, at least 32 random bytes (`openssl rand -hex 32` gives 64). Changing it signs every operator out. |
| `SLIPWAY_BASE_URL` | app | The https address the page is served at, such as `https://slipway.example.org`. The login callback is this plus `/callback`, so the identity provider must list that address. Compose builds it from `SLIPWAY_DOMAIN`. |
| `SLIPWAY_BUILDER_IMAGE` | worker | The tag built from `builder.Dockerfile`, which builder containers run. |
| `SLIPWAY_BUILDER_NETWORK` | worker | The isolated builder network builder containers join. |
| `SLIPWAY_BUILDER_USER` | worker | The non root `uid:gid` builder containers run as; it must own `SLIPWAY_SESSIONS`. |
| `SLIPWAY_SESSIONS` | worker | The writable folder builder sessions live in, mounted at the same path inside the worker and on the VPS, because Docker resolves a builder container's bind mounts on the VPS. |

The app refuses to start while `SLIPWAY_SESSION_KEY` or `SLIPWAY_BASE_URL` is unset, while the key is shorter than 32 bytes, or while the base URL is not https. The worker refuses to start, naming each one, while a worker variable above is unset. Compose also sets the Hatchet client's own `HATCHET_CLIENT_HOST_PORT`, `HATCHET_CLIENT_TLS_STRATEGY`, and `HATCHET_CLIENT_SERVER_URL` for both, so their gRPC and REST calls reach the engine on the compose network, where it speaks plain gRPC, instead of the addresses the token names for outside clients.

## Which service receives which variables

| Compose service | Receives | Does not receive |
|:-|:-|:-|
| `app` (MCP routes, status page, webhooks) | `SLIPWAY_DATABASE_URL`, `HATCHET_CLIENT_TOKEN`, `SLIPWAY_SESSION_KEY`, `SLIPWAY_BASE_URL`, the identity provider settings (the operator file's `identity_provider.client_secret_env`), every webhook secret (`webhook_secrets[].env` and `previous_env`) | Account tokens, tracker and host credentials, Langfuse keys, runner registration tokens |
| `worker` (card workflow, watch, runner cron) | `SLIPWAY_DATABASE_URL`, `HATCHET_CLIENT_TOKEN`, the three `SLIPWAY_BUILDER_*` settings and `SLIPWAY_SESSIONS`, and the rest of the operator file's variables: `accounts.*.env`, `tracker_credentials.*.token_env` and `email_env`, `host_credentials.*.env`, `langfuse_projects.*.public_key_env` and `secret_key_env` | Runner registration tokens |
| runner containers (one per instance) | Only that instance's registration tokens, `runner_instances.<name>.repositories[].token_env` and `previous_token_env`, with the runner's settings and its database URL, all from the runner's own environment file (`RUNNER_ENV_FILE`), which no other service reads | Everything else |
| builder containers (one per card, started by the worker) | Only the account token for the card's binding and the git credential through a credential helper (KTD12) | Everything else |
| `hatchet-lite`, `hatchet-postgres`, `slipway-postgres`, `caddy`, `dagger` (started only with the `dagger` profile) | Their own settings only. The Hatchet default admin password is replaced on first boot. | Every Slipway secret |

The operator chooses the names of the credential variables, so compose cannot list them one by one. Each of the app and the worker therefore reads its own environment file, root owned and mode 600, as the runner does: `SLIPWAY_APP_ENV_FILE` holds the identity provider's client secret and every webhook secret, and `SLIPWAY_WORKER_ENV_FILE` holds the account tokens, tracker and host credentials, and Langfuse keys. The shared values (`SLIPWAY_DATABASE_URL`, `HATCHET_CLIENT_TOKEN`) and the values Slipway fixes are named in `compose.yaml` itself, from the main environment file. `tests/unit/test_app.py` parses `compose.yaml` and fails if the app's service names any other variable.

## Required compose variables

`compose.yaml` writes each of these as `${NAME:?}`, so `docker compose up` refuses to start while one is unset in the root owned environment file. Set them before the next `up`, even for a Hatchet only boot:

| Variable | What it is |
|:-|:-|
| `HATCHET_POSTGRES_PASSWORD`, `HATCHET_SERVER_URL`, `HATCHET_COOKIE_DOMAIN`, `HATCHET_GRPC_BROADCAST_ADDRESS`, `HATCHET_ADMIN_EMAIL`, `HATCHET_ADMIN_PASSWORD` | Hatchet's own settings (U4). |
| `RUNNER_IMAGE` | The tag built from `runner.Dockerfile`. |
| `RUNNER_ENV_FILE` | The path of the runner's own environment file. |
| `SLIPWAY_BUILDER_NETWORK` | The isolated builder network's name, the one the worker starts builder containers on. |
| `SLIPWAY_IMAGE` | The tag built from `app.Dockerfile`, one image for the app and the worker. |
| `SLIPWAY_POSTGRES_PASSWORD` | The `slipway` database's password. Letters and digits only, for the same reason as Hatchet's. |
| `SLIPWAY_DOMAIN` | The VPS's public DNS name. Caddy obtains its certificate for it, and the app's base URL is `https://` plus it. |
| `SLIPWAY_ACME_EMAIL` | The address Let's Encrypt writes to about the certificate. |
| `SLIPWAY_SESSION_KEY` | The page's cookie key (first table). |
| `HATCHET_CLIENT_TOKEN` | Minted in the Hatchet dashboard after Hatchet's first boot (below). |
| `SLIPWAY_APP_ENV_FILE`, `SLIPWAY_WORKER_ENV_FILE` | The paths of the app's and the worker's own environment files. |
| `SLIPWAY_HOME_DIR` | The configuration root on the VPS, holding `operator.toml` and `projects/`. |
| `SLIPWAY_BUILDER_IMAGE`, `SLIPWAY_BUILDER_USER`, `SLIPWAY_SESSIONS` | The worker's builder settings (first table). |
| `DOCKER_GID` | The group that owns `/var/run/docker.sock`, which the worker joins to start builder containers (`stat -c %g /var/run/docker.sock`). |

`HATCHET_GRPC_INSECURE` is gone: Caddy terminates TLS on the public gRPC port, so the engine serves plain gRPC on the compose network only and compose fixes `SERVER_GRPC_INSECURE` to `t`. Set `HATCHET_GRPC_BROADCAST_ADDRESS` to `SLIPWAY_DOMAIN:7077`, the address the laptop worker dials over TLS.

`DAGGER_ENGINE_IMAGE` is needed only with the `dagger` profile.

## `SLIPWAY_HOME` mounts

| Service | Mount |
|:-|:-|
| `app` | `projects/` writable (onboard saves project files), and `operator.toml` alone as a read only file mount |
| `worker` | Read only, `operator.toml` present, with `SLIPWAY_HOME_DIR/cache` bound writable over `cache/` for the git cache (`gitread.cache_path`), the one thing the worker writes under `SLIPWAY_HOME`. That folder must exist on the VPS, owned by the image's uid, before the worker starts. |

The worker also mounts `SLIPWAY_SESSIONS` at the same path and the Docker socket, which it uses only to start, inspect, and remove builder containers. The app mounts neither.

A bind mount maps one path on the VPS into a container. The app gets two: the `projects/` directory, writable, and the single file `operator.toml`, read only, both under the same `SLIPWAY_HOME` path inside the container so `load_operator()` and `load_project()` find them unchanged. The next section says why the app reads the operator file at all.

## Published ports

| Listener | Reachable from |
|:-|:-|
| App, port 443, through Caddy | The internet. Caddy holds the certificate and forwards plain HTTP to the app's port 8000 on the compose network. |
| Hatchet gRPC, port 7077, through Caddy, with a token per worker | The internet, for the laptop worker. Caddy terminates TLS and forwards to the engine on the compose network. This is the fourth public listener counted in KTD28. |
| App port 8000 | Compose network only |
| Hatchet dashboard and API | Compose network, plus the VPS's own loopback for an SSH tunnel (`127.0.0.1:8888`, from the U4 seed); never the internet |
| Runner read API | Compose network only |
| Postgres | Compose network only |
| Dagger engine | Compose network only |

## Bringing the stack up

Run these on the VPS as root, from the repository's `deploy/` folder. `ENV` stands for `--env-file /etc/slipway/slipway.env`, the main environment file.

1. Point `SLIPWAY_DOMAIN`'s DNS record at the VPS. Caddy needs it to answer before it can obtain a certificate.
2. Build the images from the repository root: `docker build -f deploy/app.Dockerfile --build-arg SLIPWAY_UID=<uid> --build-arg SLIPWAY_GID=<gid> -t <SLIPWAY_IMAGE> .`, and the builder image from `builder.Dockerfile` with the same uid and gid.
3. Create the folders: `SLIPWAY_HOME_DIR` with `operator.toml`, `projects/` and `cache/` both owned by the image's uid; and `SLIPWAY_SESSIONS`, owned by the same uid.
4. Write the three environment files, each root owned and mode 600: the main one with every variable in the table above, the app's, and the worker's. For the very first boot `HATCHET_CLIENT_TOKEN` does not exist yet; set it to `pending`.
5. Start Hatchet alone: `docker compose ENV up -d hatchet-postgres hatchet-lite`. Open the dashboard through an SSH tunnel (`ssh -L 8888:127.0.0.1:8888 <vps>`), sign in with `HATCHET_ADMIN_EMAIL`, mint a client token, and put it in the main file as `HATCHET_CLIENT_TOKEN`.
6. Create the schema: `docker compose ENV run --rm worker sh -c 'python -m slipway.records.migrate "$SLIPWAY_DATABASE_URL"'`.
7. Start everything: `docker compose ENV up -d`. `docker compose ENV logs caddy` shows the certificate being obtained; `docker compose ENV logs app worker` shows each process starting, or the variable it refused to start without.

Then the operator checks it end to end, and records the outcome in the pull request that changed the stack:

- **Sign in.** Add `https://SLIPWAY_DOMAIN/callback` to the identity provider's allowed redirect addresses, open `https://SLIPWAY_DOMAIN/`, and sign in. The page loads with every project's totals. Any other account gets "Not for this account".
- **A signed test delivery.** In the managed repository's webhook settings, set the payload address to `https://SLIPWAY_DOMAIN/webhooks/github` (or `/webhooks/bitbucket`), the content type to JSON, and the secret to the value of that repository's `webhook_secrets` variable, then send a test delivery. The reply names the outcome: `accepted` or `recorded` for an event Slipway tracks, `dropped` with the reason for one it does not (GitHub's `ping` is dropped as not tracked, which proves the signature held, since an unsigned or badly signed delivery is answered 401 instead).

## Rotating the session key

The session key signs every cookie the page sets, so a new key turns every existing cookie into a forgery the page refuses. Rotating it signs every operator out, which is the point when a key may have leaked.

1. Write a new value, `openssl rand -hex 32`, over `SLIPWAY_SESSION_KEY` in the main environment file.
2. `docker compose ENV up -d app`. Compose hands a container its environment only when it creates it, so the new key takes effect here.
3. Sign in again.

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

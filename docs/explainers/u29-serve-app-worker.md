# U29 explainer: serving the app and the worker on the VPS

Before this unit, Slipway's parts existed only as code: the status page (U20), the webhook check (U9), the card workflow and its worker (U14). Nothing started them on the VPS, nothing held a certificate, and nothing decided which process received which secret beyond the rules in `deploy/README.md`. U29 is the wiring. It adds the one module that builds the running app, the worker's entry point, one image for both, Caddy in front of them, and the compose services that run it all with only their own variables (KTD28).

## Words used below

- **VPS.** The rented virtual server Slipway runs on.
- **Composition root.** The one module that builds the running program from its parts. Every other module receives what it needs; only this one decides which real thing each part gets. Here it is `src/slipway/app.py`.
- **WSGI.** The standard interface between a Python web application and the server that runs it. The page and the app are both WSGI applications.
- **Webhook.** An HTTP POST a host (GitHub or Bitbucket) sends to Slipway when something happens to a repository, signed with a shared secret.
- **TLS.** The encryption under https. The server proves who it is with a certificate the client checks.
- **Caddy.** A web server that obtains and renews its own certificate from Let's Encrypt. It is the only process on the VPS that holds one.
- **Terminate TLS.** Take the encryption off at the front door and forward plain traffic inward, here over the private compose network.
- **gRPC.** The protocol a Hatchet worker uses to talk to the engine, over HTTP/2. **h2c** is HTTP/2 without TLS, which is what the engine speaks once Caddy has taken TLS off.
- **Environment file.** A text file of `NAME=value` lines that compose hands to one service as its environment.
- **Credential helper.** A small program git asks for a user name and password instead of keeping them in a URL.
- **Session key.** The key that signs the page's cookies. Whoever holds it could forge a sign in, so it is a secret, and changing it signs every operator out.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/app.py` | The composition root. `build()` checks the two variables Slipway fixes, loads the operator file, builds the page with `OidcIdentity`, builds the webhook endpoint, and puts both behind one WSGI application. `main()` serves it on port 8000 with the standard library's threading WSGI server. |
| `src/slipway/workflow/worker.py` | Gains `main()`, the VPS worker's entry point, `python -m slipway.workflow.worker`: the builder port (a container per card, or the laptop's schedule), the credential port, and the git credential helper. |
| `deploy/app.Dockerfile` | One image for the app and the worker, running as a non root user, with git and the docker command line. |
| `deploy/Caddyfile` | Caddy's two sites: the app on 443 and the engine's gRPC on 7077, both behind one certificate for `SLIPWAY_DOMAIN`. |
| `deploy/compose.yaml` | Gains `slipway-postgres`, `app`, `worker`, and `caddy`. The engine's gRPC is no longer published directly, and its TLS setting is fixed. |
| `deploy/README.md` | The new variables, the app's and the worker's own environment files, the mounts, the steps to bring the stack up and check it, and how to rotate the session key. |
| `tests/unit/test_app.py` | The card's scenarios, the worker's entry point, and the endpoint's SQL against Postgres. |
| `pyproject.toml` | PyYAML joins the `dev` extra, so the test can parse `compose.yaml`. |

## How a request travels

```
internet -> caddy :443 -> app :8000 -> GET  ...             page.app.Page
                                    -> POST /webhooks/<host>  Webhooks
laptop worker -> caddy :7077 (TLS) -> hatchet-lite :7077 (h2c)
app, worker   -> hatchet-lite :7077 (plain gRPC, compose network)
```

The app answers two kinds of request on one listener. `App.__call__` sends a path under `/webhooks/` to the endpoint and everything else to U20's page, which keeps its own login, its read only rule, and its headers.

## The two variables Slipway fixes

`settings()` reads `SLIPWAY_SESSION_KEY` and `SLIPWAY_BASE_URL` and raises `StartRefused` when the key is unset or shorter than 32 bytes, or when the base URL is unset or not https. `build()` adds one more refusal: the identity provider's client secret, named by the operator file's `client_secret_env`, must be set. A refused start prints the reason and exits 2, so `docker compose logs app` names the missing setting instead of showing a traceback.

The base URL is never taken from a request. The page builds its login callback from it (`<base>/callback`), so a forged `Host` header cannot send the identity provider's code anywhere else. In compose it is `https://` plus `SLIPWAY_DOMAIN`, the same name Caddy obtains its certificate for, so the two cannot drift.

## The webhook endpoint

`Webhooks.handle` takes one delivery through these steps, stopping at the first that answers:

1. **The cap.** `App.webhook` reads `Content-Length` and refuses a body over U9's cap with 413 before reading it.
2. **verify.** `hosts.webhooks.verify` resolves the repository through `secrets_for`, built by `secrets_for()` from the operator file's `webhook_secrets` and the app's environment. An unknown repository is answered 202 `dropped`, not refused: a host pointed here by mistake learns nothing about which repositories are managed. A missing or wrong signature is 401. No database connection is opened before the signature holds.
3. **Opened by Slipway.** For a pull request event, verify asks whether Slipway opened it, which the endpoint answers by looking for a build with that pull request number. A pull request Slipway did not open is dropped.
4. **The delivery log.** U9's in process `DeliveryLog` answers a retried delivery id as a `duplicate` without a database trip. If storing the delivery then fails, the endpoint calls the log's `forget`, which drops the id, so the host's redelivery is stored rather than answered as a duplicate.
5. **The project.** `project_finder()` finds the project file whose host and repository match. None means dropped.
6. **host_events.** `PostgresWebhookRecords.ingest` writes the normalised event through U7's `ingest_host_event`, which dedupes on the delivery id again, durably.
7. **The build and the push.** `build_for` finds the build the event names, by pull request number first and by the commit a check or merge group reports otherwise, and `service.engine.push_host_event` wakes that build's run. The app reaches the workflow only through `service` (KTD23); `app.py` imports nothing from `workflow/`, and the test says so.

A host event is a wake up, never evidence (KTD10): the run re reads the pull request itself. So when the engine refuses a push, or the database is down, the endpoint says so (`recorded` or 503) and the run's hourly poll covers the gap.

Each request's database connections are opened through `Connections`, which closes every one when the response is built. The page asks for its ports once or twice per request and never closes them itself.

## The worker's entry point

`python -m slipway.workflow.worker` checks its five variables (`SLIPWAY_DATABASE_URL`, `SLIPWAY_BUILDER_IMAGE`, `SLIPWAY_BUILDER_NETWORK`, `SLIPWAY_BUILDER_USER`, `SLIPWAY_SESSIONS`), names every unset one, and otherwise starts U14's `vps_worker` with three ports U14 left to the deploy:

- **builder.** `container_builders()` returns U13's `ContainerBuilder` for the project, over its repository's clone address and the host's token user name, or `LaptopScheduled` for a project that builds on the laptop. The VPS worker only reads that one's schedule and sends the build to the laptop worker.
- **credential.** `host_credentials()` wraps the project's host credential, read through `resolve_host_credential`, in a builder `Credential` with no expiry, since the operator file's host credentials are static tokens.
- **git.** `main()` points every git call at the credential helper with `GIT_CONFIG_*` variables. When git reads a repository, it runs `python -m slipway.workflow.worker credential get`, and `credential_answer()` replies with the host credential of the one project whose repository that is, or nothing. The cache under `SLIPWAY_HOME/cache` never holds a token.

The laptop worker's entry point is not part of this unit; the laptop's own setup (U13, U26) still names its command in `SLIPWAY_WORKER_COMMAND`.

## Who receives what

The rule is the table in `deploy/README.md`. Two choices make it hold in `compose.yaml`:

- The operator chooses the names of credential variables, so compose cannot list them. The app and the worker each read their own environment file, as the runner already did: `SLIPWAY_APP_ENV_FILE` (the client secret and the webhook secrets) and `SLIPWAY_WORKER_ENV_FILE` (accounts, tracker and host credentials, Langfuse keys). Everything else is named in `compose.yaml` from the main file, and the test checks that the app's names are exactly the eight it needs. Three of them are the Hatchet client's own (`HATCHET_CLIENT_HOST_PORT`, `HATCHET_CLIENT_TLS_STRATEGY`, `HATCHET_CLIENT_SERVER_URL`): the token names the addresses an outside client dials, so the containers are pointed at the engine on the compose network instead.
- Only Caddy publishes ports, 443 and 7077. The engine's dashboard stays on the VPS's loopback, and the app, both databases, and the engine's own gRPC are on the compose network only. Because Caddy terminates TLS for the engine, `HATCHET_GRPC_INSECURE` is gone and the engine runs plain gRPC inside, which closes the TLS gap the U4 seed left.

The worker's configuration root is read only, as before, with two additions: the VPS folder `SLIPWAY_HOME_DIR/cache`, owned by the image's uid, bound writable over `cache/` for the git cache, and `SLIPWAY_SESSIONS` mounted at the same path inside and out, because Docker resolves a builder container's bind mounts on the VPS. It also joins the Docker socket's group to start builder containers; the app has no socket and no sessions.

## Running the tests

```sh
CI=true python -m pytest -q tests/unit/test_app.py
```

The last test needs the local Postgres from `CLAUDE.md`. The rest use the fixture config, `FakeIdentity` from the page's tests, the service fakes' `FakeEngine`, and in memory webhook records.

## What the operator checks on the VPS

The card's verification is a live one: bring the stack up, sign in through the real identity provider, and send one signed test delivery. `deploy/README.md` lists the steps under "Bringing the stack up".

# U6 explainer: project and operator configuration, redaction

Slipway may not know anything about any one project (R1). Every project specific value lives in a configuration file outside any repository, and every secret lives in an environment variable that a file only names. This card builds the code that reads those files, checks them, turns a role into an account, and the one module that removes secrets from text before it leaves Slipway.

## Words used below

- **TOML.** A plain text format of `key = value` lines and `[table]` headings. Python reads it with the standard library's `tomllib`.
- **Operator.** The one person who runs Slipway and holds the accounts it spends.
- **Slug.** A project's short name, used in its file name and its MCP URL.
- **Subject.** The stable user id an identity provider puts in a login token. Slipway decides who may act by subject.
- **Role.** A job a model does for a project: `builder`, `judge`, or an optional helper (R13).
- **Binding.** The project's pairing of a role with an operator account, a model, an effort, and a time budget.
- **Halt class.** One of the fixed reasons a build stops (KTD11). This card raises one of them, `binding_invalid`.
- **Redaction.** Removing a secret from text and leaving a marker, here `[redacted]`.
- **Hash.** A fixed length fingerprint of a value. The same value always gives the same hash, and the hash cannot be turned back into the value.

## Where the files live

```
$SLIPWAY_HOME                      default ~/.slipway
  operator.toml                     one per operator (R3)
  projects/
    <slug>.toml                     one per project (R2)
```

Neither file is ever in a repository. The test suite points `SLIPWAY_HOME` at a temporary directory and `HOME` at an empty one, so no test can read the real home directory.

## What each file does

| File | What it does |
|:-|:-|
| `src/slipway/config/__init__.py` | Finds `SLIPWAY_HOME`, reads TOML, and holds `ConfigError` and `Reader`, a small typed reader that names the dotted key (for example `roles.builder.account`) in every failure. Also the secret scan both files share. |
| `src/slipway/config/operator.py` | The `Operator` dataclass and its loader: the operator's subject, accounts (each a `subscription` or an `api_key` with its variable name), the identity provider, Langfuse projects, tracker and host credentials, the runner repository and instances, the runner read API, builder workers, and webhook secrets with a rotation pair. Every credential is a variable name, checked to look like one. |
| `src/slipway/config/project.py` | The `Project` dataclass with every R2 field, the `LandingPath`, `MergeMethod`, `Tracker`, and `TraceDetail` enums, the loader, and `check_names`, which refuses any name the operator file does not define. |
| `src/slipway/config/bindings.py` | `resolve_binding(slug, role)` and `resolve_tracker_credential(slug)`. Both read both files on every call and raise `BindingInvalid` (halt class `binding_invalid`) on any failure. |
| `src/slipway/redact.py` | `scrub(text, injected_values)`, `refusal_record(value, reason)`, and `TOKEN_PATTERNS`, the token shapes both redaction and configuration validation use. |
| `deploy/README.md` | The KTD28 table: which compose service receives which variables, the `SLIPWAY_HOME` mounts, and the published ports. |
| `tests/fixtures/config/` | One complete operator file and one complete project file with invented values. |
| `tests/unit/test_config.py`, `tests/unit/test_redact.py` | The card's test scenarios, plus a few more for the edges. |

## How a project file is validated

`load_project(slug)` reads `projects/<slug>.toml` and runs three passes. The first failure stops it, and the `ConfigError` carries the file, the dotted key, and the reason.

1. **Secrets.** Every string anywhere in the file is checked against `redact.TOKEN_PATTERNS`. A match fails with the key and the pattern's name, never the value.
2. **Variable names.** The fields that name an operator file entry, `tracker_credential`, `host_credential`, `trace_project`, `builder_worker`, and every `roles.<role>.account`, are checked for the shape of an environment variable name (capitals and digits joined by underscores, such as `EXAMPLE_JIRA_TOKEN`). A match fails. A project file names a credential by its operator file entry, so a variable name in one of those fields is always a mistake, and usually a sign the next edit would paste the value. Every other string is left alone, because a capitalized value can be legitimate there: a Jira workflow whose statuses are `TO_DO` and `IN_REVIEW` must be configurable. The list lives in `NAME_FIELDS` in `project.py`, and `name_fields(data)` yields the dotted key and value for each one present.
3. **Shape.** Every required key is present with the right type, every enum value is one of its members, `builder` and `judge` roles exist, and no unknown key is present (so a typo fails instead of being ignored).

`check_names(project, operator)` is a separate fourth pass: the tracker credential, host credential, trace project, builder worker, and every role's account must exist in the operator file, and the host credential must be for the project's host. The tracker credential's `site` must also equal the project's `tracker_site`, the same comparison `resolve_tracker_credential` makes. Without it a project file could pass `check_names` and still halt `binding_invalid` on its first dispatch; the rule is that every fault resolve can find in the two files, `check_names` finds first. Onboard (U19) calls it before it saves. It is separate because the plan does not validate a hand edited file at edit time; dispatch finds the problem at resolve time instead and halts with `binding_invalid`.

The operator file runs the same secret scan and shape pass. Its credential fields must look like variable names. Every nested table refuses a key it does not know, not only the top level: `operator.py` lists the keys each table may hold (`TRACKER_KEYS`, `ACCOUNT_KEYS`, and so on), and `checked(tables, known)` applies the list to every entry of a named table such as `[tracker_credentials.<name>]` or an array of tables such as `[[webhook_secrets]]`. A typo such as `tokn_env` therefore fails with its dotted key, `tracker_credentials.example-jira.tokn_env`. Before this, the typo was ignored, and the error named the missing `token_env` instead, which pointed the operator at the wrong line.

## How a binding is resolved

Each workflow step resolves the binding when it runs, so swapping a model is a file edit that takes effect at the next step with no restart (F4). The lookup:

```
resolve_binding(slug, role)
  |
  v
SLIPWAY_HOME set?  yes -> that directory
                    no  -> ~/.slipway
  |
  v
read projects/<slug>.toml now, validate      fail -> BindingInvalid(field)
read operator.toml now, validate             fail -> BindingInvalid(field)
  |
  v
project.roles[role]                          absent -> BindingInvalid("roles.<role>")
  |  gives account name, model, effort, time budget
  v
operator.accounts[account name]              absent -> BindingInvalid("roles.<role>.account")
  |  gives kind and the variable that holds the token
  v
ResolvedBinding(role, account, model, effort, time_budget_minutes)
```

`resolve_tracker_credential(slug)` follows the same path to `operator.tracker_credentials[project.tracker_credential]`, then refuses when that entry's site differs from the project's `tracker_site`, so one customer's tracker token can never be sent to another customer's site. Only then does it read the token (and the email, for Jira) from the environment. Its `repr` hides both values.

## How redaction works

`scrub(text, injected_values)` does two passes:

1. **Exact values.** Every credential Slipway injected into a session is passed in. Values shorter than eight characters are skipped, because removing every `main` from a transcript protects nothing. Each remaining value is found in three forms:
   - **Raw and percent encoded.** Percent encoding is how a value looks inside a URL: each unsafe byte becomes `%` and two hex digits, so `/` becomes `%2F`. The match is a regular expression (a pattern language for text) that lets each character appear raw or encoded, with the hex in either case, so `%2F`, `%2f`, and a bare `/` all match.
   - **Base64.** Base64 is a way of writing any bytes as letters, digits, `+`, and `/`, four characters for every three bytes; HTTP Basic authorization uses it. Because of that three byte grouping, the characters a value turns into depend on how many bytes sit in front of it. `scrub` works out, for each of the three possible offsets, the run of characters that depends on the value alone, in the standard alphabet and the URL safe one (`-` and `_` in place of `+` and `/`). That catches a token encoded inside a longer string such as `x-access-token:<token>`, minus one or two edge characters that also carry a neighbouring byte.
   - **Composed Basic credentials.** The Jira adapter passes both its email and its token, and its header is `Basic base64(email:token)`. A token's own base64 is not a plain substring of that, so `scrub` also encodes every `a:b` pair of two injected values and removes that encoding whole, padding included. When the email is passed in and is at least eight characters, the header comes out as `Basic [redacted]`. With a shorter email no pair is built, so only the token's own base64 characters go and the email's encoding stays visible.

   Every form is searched for in the original text first, then overlapping or touching matches merge into one `[redacted]`. Replacing one form at a time could break a second form's match halfway through and leave part of it behind. The compiled patterns for a set of values are built once and cached, since `scrub` runs on every string field of every trace record and a session passes the same values each time.
2. **Known shapes.** Every match of `TOKEN_PATTERNS` is replaced: GitHub, Anthropic, Langfuse, Atlassian, Bitbucket, AWS, and Slack token prefixes, JSON Web Tokens, PEM private keys, and a password written into a URL as `user:password@`.

Anything else passes through. A customer secret a test printed is not caught, by design (KTD24); the per project `trace_detail` switch exists for that case. Other encodings of an injected value also pass, for example: hex, double percent encoding (`%252F`), letters percent encoded (`%73`), HTML entities, the raw value wrapped across lines, JSON escapes, base64 wrapped across lines, and a Basic pair whose user part was never passed in (only the secret's own base64 characters go, less their edges).

`refusal_record(value, reason)` is how a refused value is remembered, for example an onboard answer that looked like a token: the reason, the SHA 256 hash, and the length. The value itself is never stored.

## Where this differs from the plan

- **`config/__init__.py` is an extra file.** It holds the shared reader, the error type, and `home()`, so the three listed modules do not repeat them.
- **The project file has `tracker_site`.** The plan says resolve compares "the project's site" with the credential's, but R2 has no site field, so the project names one.
- **The project file can pin `merge_method`** beside `landing_path`, because KTD9 says the project file may pin both.
- **R2's "accounts per role, model and effort per role, time budget per role" is one `[roles.<role>]` table** per role, and `builder` and `judge` are required.
- **Signatures take a slug, not a loaded project,** so each call reads the file from disk (F4). `refusal_record` takes a `reason` argument, since the plan says it returns one.
- **`resolve_tracker_credential` also fails with `binding_invalid` when a variable is unset,** not only on a site mismatch.
- **Token patterns live in `redact.py`** and the validator imports them, so the two can never disagree.
- **`deploy/README.md` records an open question:** KTD28 keeps the operator file out of the app, but the app needs the operator's subject and the webhook secret names.

# Yale Access

[![CI](https://github.com/mikkelke/ha-yale-access/actions/workflows/ci.yml/badge.svg)](https://github.com/mikkelke/ha-yale-access/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/mikkelke/ha-yale-access)](https://github.com/mikkelke/ha-yale-access/releases)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A Home Assistant custom integration for listing Yale lock users and managing guest
PINs through Home Assistant's authenticated WebSocket API. Administrators and guest
managers receive different access, and residents can view and change their own code.
Configuration is YAML only; the integration includes a login health sensor and no frontend.

**Unofficial, not affiliated with Yale or ASSA ABLOY.** This integration uses the Yale
Home app's API and may break when the app changes.

## Features

- Create random or chosen PINs with always-valid or temporary access.
- View and change codes, update validity, and end PIN access.
- List names, status, validity, creator, and the latest PIN or app unlock within 90 days.
- Keep expired guest codes available for reuse after a validity update.
- Limit guest managers to their own guests and explicitly assigned Yale users.
- Protect resident accounts from guest lists and access removal.
- Refresh the app session automatically and alert when a new login is needed.

## Requirements

- A Yale lock with a keypad and a working Yale Connect / Wi-Fi bridge.
- A Yale Home account with owner rights for the lock.
- Home Assistant **2026.10.0 or newer**.
- Python **3.12+** and `aiohttp` wherever you run the standalone login helper.

One lock is configured per Home Assistant installation.

## Installation

### HACS

1. In HACS, open the menu and select **Custom repositories**.
2. Add `https://github.com/mikkelke/ha-yale-access` with category **Integration**.
3. Find **Yale Access** and download it.
4. Complete [Logging in](#logging-in) and [Configuration](#configuration), then restart Home Assistant.

### Manual

1. Download the repository and copy `custom_components/yale_access` into your Home
   Assistant configuration directory's `custom_components` folder.
2. Complete [Logging in](#logging-in) and [Configuration](#configuration), then restart Home Assistant.

The login helper is a separate file: download `tools/yale_login.py` from this repository
for either installation method.

## Logging in

Choose one of the following methods. The helper privately prompts for your Yale
account identifier, password, and verification code when required. Use the same
uppercase two-letter country code here and in YAML; the default is `US`. For example,
use `--country DK` and `country: DK` for Denmark.

### Inside the Home Assistant container

Save the helper as `/config/yale_login.py`, then run this on the Docker host:

```sh
docker exec -it homeassistant python3 /config/yale_login.py --country US
```

Replace `homeassistant` with your container's name. The default session file is
`/config/.yale_app_session.json`. The Home Assistant container already includes `aiohttp`.

### Advanced SSH & Web Terminal add-on

Save the helper as `/config/yale_login.py`. The add-on needs Python 3.12+ and `aiohttp`;
add `python3` and `py3-aiohttp` to its `packages` option if needed, then restart the
add-on. Open its terminal and run:

```sh
python3 /config/yale_login.py --session-file /config/.yale_app_session.json --country US
```

### On any computer

Download the helper, open a terminal in that directory, and run with Python 3.12+:

```sh
pip install aiohttp
python3 yale_login.py --session-file ./yale_app_session.json --country US
```

Copy `yale_app_session.json` into Home Assistant's `/config` directory as
`.yale_app_session.json`, or set `session_file` to the destination you chose. Make
sure Home Assistant can read and replace the file, and restrict its permissions to
`600` on the Home Assistant host:

```sh
chmod 600 /config/.yale_app_session.json
```

The session refreshes itself as Yale returns new tokens. `sensor.yale_access_login`
reports `ok`, `expiring` (less than 14 days remaining), or `failed`. The configured
notification service and a Repairs issue warn when login needs attention. Run the
helper again and replace the same session file; Home Assistant reloads it automatically,
without a restart. Server policy may still require occasional new logins.

## Finding your ids

After login, the helper lists each accessible lock's **lock id**, **name**, and
**house name**, followed by its users' **Yale user ids**, **first and last names**,
and **types** such as `superuser` or `user`. Keypad-only users are listed as
`pin only` with their slot. It prints no tokens or PINs.
To repeat this using an existing session without logging in:

```sh
python3 yale_login.py --session-file ./yale_app_session.json --country US --list-only
```

Use the selected lock's id for `lock_id`. Use its user ids for `protected_users`,
`own_yale_user`, and `yale_users`. Yale user types describe the Yale account;
Home Assistant permissions are controlled separately by the roles below.

For Home Assistant user ids, enable **Advanced mode** in your Home Assistant profile,
then open **Settings → People → Users**, select a user, and copy their **ID**.
Quote these 32-character hexadecimal ids in YAML.

## Configuration

Add this to `configuration.yaml`, replacing every placeholder. The same example is
in [`examples/configuration.yaml`](examples/configuration.yaml). Restart Home Assistant
after configuration changes.

```yaml
yale_access:
  session_file: /config/.yale_app_session.json
  country: US
  lock_id: "<Yale lock id>"
  notify: notify.mobile_app_<admin_device>
  protected_users:
    - "<admin Yale user id>"
  users:
    - ha_user_id: "<admin Home Assistant user id: 32 hex characters>"
      role: admin
      own_yale_user: "<admin Yale user id>"
    - ha_user_id: "<guest manager Home Assistant user id: 32 hex characters>"
      role: guests
      own_yale_user: "<guest manager Yale user id>"
      yale_users:
        - "<assigned guest Yale user id>"
```

| Key | Required / default | Meaning |
| --- | --- | --- |
| `session_file` | Optional; `/config/.yale_app_session.json` | App session file. Home Assistant must be able to read and replace it. |
| `country` | Optional; `US` | Exactly two uppercase ASCII letters. Match the login helper's `--country`. |
| `lock_id` | Required | Yale lock id from the helper's listing. |
| `notify` | Required | A `notify.*` service for login health alerts. |
| `protected_users` | Optional; `[]` | Yale user ids hidden from all user lists and protected from access changes. Include resident or owner accounts you want protected. |
| `users` | Required | At least one Home Assistant user mapping. Unmapped users cannot use the API. |
| `users[].ha_user_id` | Required | Quoted Home Assistant user id, exactly 32 hexadecimal characters. Each may appear once. |
| `users[].role` | Required | `admin` or `guests`; see the visibility rules below. |
| `users[].own_yale_user` | Optional | Resident's own Yale user id, allowing them to view and change their own PIN, including when protected. Does not grant list access. |
| `users[].yale_users` | Optional; `[]` | Yale user ids explicitly assigned to this Home Assistant user for listing and management. |

Yale lock and user ids use 8–128 letters, digits, underscores, or hyphens. Each Yale
user id may appear only once across all `own_yale_user` and `yale_users` assignments.
It may also appear in `protected_users`.

## Roles and visibility rules

- **`admin`** can list and manage every unprotected user on the configured lock.
- **`guests`** can list and manage guests they created through this integration, plus
  Yale users assigned to them through `yale_users`. They must supply a start and end
  when creating access or changing validity. Other users appear absent.
- **Protected users** are hidden from lists for every role, including admins. Their
  occupied PIN slots remain reserved. Their PINs and access cannot be changed here,
  except that a resident can view and change their configured `own_yale_user` PIN.
- **Own access** is separate from list visibility. A configured resident can use `me`,
  `pin`, and `set_pin` for their own code. No role can change its own access period
  or end its own access through this integration.

Ownership is recorded by the authenticated Home Assistant creator id. Explicit
assignments grant additional access without changing that record. Yale account
permissions still apply to every request.

## WebSocket API

Send commands as `{"id": n, "type": "yale_access/<command>", ...}` over Home
Assistant's existing websocket. The caller is always `connection.user.id`; commands
never accept a creator id or role. Extra fields are rejected.

| Command | Fields | Result |
| --- | --- | --- |
| `yale_access/me` | none | `{"role": "admin", "pin_length": 6, "own": User}`; role may be `guests`, `own` is null if unconfigured or missing |
| `yale_access/users` | none | `{"users": [User]}` filtered to the caller's scope |
| `yale_access/create` | `name`, optional `from`, `to` and `pin` | `{"user": User, "pin": "<code>"}` |
| `yale_access/set_validity` | `user_id`, optional `from` and `to` | `{"user": User}`; refused for the caller's own user |
| `yale_access/pin` | `user_id` | `{"pin": "<code>"}` from the lowest loaded slot; allowed for the caller's own user |
| `yale_access/set_pin` | `user_id`, `pin` | `{"user": User}` with validity unchanged; allowed for the caller's own user |
| `yale_access/end` | `user_id` | `{"ok": true}` after confirmed removal; refused for the caller's own user |

`from` and `to` must both be supplied or both omitted. Use ISO datetimes with an
explicit UTC offset, for example `2026-10-29T14:00:00+01:00`; `to` must be later.
Admins can omit both for always-valid access; guest managers must supply both.
Recurring schedules are listed read-only
but can be revoked with `end`. Validity and PIN changes require a non-recurring PIN
credential; app-only schedules are read-only. Chosen codes must contain only ASCII
digits, match `pin_length`, and differ from other users' existing codes. Omitting
`pin` when creating a user generates a random code. `me` uses cached rows and code
length, refreshing first if needed, without an additional activity scan.

`User` contains `user_id`, `name`, `slot` (null for app-only users), `kind`
(`always`, `temporary`, `recurring`), `from`, `to`, `status`, `last_use`, and
`created_by`. Status is `scheduled`, `active`, `expired`, `pending`, or `failed`.
`created_by` is the assigned Home Assistant owner's display name, otherwise the
creator's name, or null for unassigned imported users. No matching unlock within
90 days means `last_use: null`.

Protected targets return `protected` to admins and `not_found` to guest managers.
Changing or ending the caller's own access returns `protected`. Writes are serialized
per lock and polled for confirmation for up to 180 seconds; an accepted HTTP request
alone does not confirm a change. An unrelated pending credential blocks writes
because Yale sync applies to the whole lock. Read refreshes reconcile pending changes.
On a `pending` error or disconnection, inspect the user list and Yale app before
retrying; do not blindly create the user again. Once loaded, an authorized caller
can retrieve the PIN with `pin`.

Ending an imported resident or app user's PIN access leaves their app membership
in place. An unverified PIN-only guest is removed by Yale after credential deletion.
Error codes are `forbidden`, `not_found`, `protected`, `invalid_format`, `read_only`,
`request_failed`, `upstream_unavailable`, and `pending`. Error messages contain no
Yale response bodies or secrets.

## Building a UI

The integration has no frontend. Any dashboard connected to Home Assistant can call
the WebSocket commands above and build its own user list, guest form, or resident
code controls. Use each person's authenticated Home Assistant connection so the
configured visibility rules apply.

## Security notes

- Treat the session file as a credential. It contains an app token, is saved with
  mode `600`, and must stay out of source control, logs, and issue reports.
- PINs are never logged or stored by the integration. They are returned only to an
  authorized caller through an explicit create or PIN request, and kept in memory
  only while processing requests. Tokens and PINs are excluded from entity states
  and attributes.
- Add owner and resident ids to `protected_users` to hide them and prevent access
  changes here. A resident's configured own-code controls remain available.
- Ownership storage contains creator and operation metadata, never PINs or tokens.
  Role and protected-user checks run before results are serialized.
- Mutation requests are not automatically retried. Redirects are disabled.

## Reporting issues

Open an [issue](https://github.com/mikkelke/ha-yale-access/issues) with your Home
Assistant and integration versions, lock model, country, command, error code, and
steps to reproduce. Redact names and ids from configuration and helper output. Never
include session files, tokens, passwords, PINs, or raw Yale API responses.

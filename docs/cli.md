# `argus-scout`

Homelab intelligence: reads your telemetry and reports back. It never acts.

**Usage**:

```console
$ argus-scout [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--install-completion`: Install completion for the current shell.
* `--show-completion`: Show completion for the current shell, to copy it or customize the installation.
* `--help`: Show this message and exit.

**Commands**:

* `version`: Print the installed version.
* `init`: Write a commented starter scout.yaml and a...
* `poll`: Poll sources once and print the events...
* `analyze`: Run one signal evaluation and engine cycle...
* `brief`: Write one report now and print it (the...
* `deliver`: Send a stored report to the configured...
* `serve`: Run the HTTP server and the scheduler...
* `check`: Validate config files before scout loads...
* `tariff`: Inspect a time-of-use tariff.

## `argus-scout version`

Print the installed version.

**Usage**:

```console
$ argus-scout version [OPTIONS]
```

**Options**:

* `--help`: Show this message and exit.

## `argus-scout init`

Write a commented starter scout.yaml and a .env.example beside it.

**Usage**:

```console
$ argus-scout init [OPTIONS] [path]
```

**Arguments**:

* `path <path>`: Config file (.yaml/.yml) to write, or a directory to write scout.yaml in.  [default: scout.yaml]

**Options**:

* `--force`: Overwrite existing files.
* `--help`: Show this message and exit.

## `argus-scout poll`

Poll sources once and print the events they return. A dry run unless --ingest is given.

**Usage**:

```console
$ argus-scout poll [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml  [required]
* `-s, --source <str>`: Poll only this source (default: every polling source).
* `--ingest`: Write the events to the store instead of only printing them.
* `--help`: Show this message and exit.

## `argus-scout analyze`

Run one signal evaluation and engine cycle against the configured Prometheus, and print the report.

For testing rules; the server runs the same cycle on its schedule. Scout only proposes and (by
standing rule) approves: nothing here deploys or acts.

**Usage**:

```console
$ argus-scout analyze [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml  [required]
* `--dry-run`: Work on an in-memory copy of the store: show what would change, write nothing.
* `--help`: Show this message and exit.

## `argus-scout brief`

Write one report now and print it (the server writes the scheduled ones).

The model&#x27;s daily cap applies; over it, or with the model unreachable, the report comes from the template.
A status report covers the open items the heartbeat policy&#x27;s report rules allow (rest and quiet hours
aside). A suite report lists every Argus service with its health and version, as Prometheus has them now; it
is always a plain listing, never model-written. The report only describes and recommends: nothing here acts.

**Usage**:

```console
$ argus-scout brief [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml  [required]
* `-k, --kind <str>`: Report kind: morning, evening, status, suite.  [default: morning]
* `--dry-run`: Store nothing: no report, no usage count, no memory file. The model is still asked.
* `--no-llm`: Skip the model and write the report from the template.
* `--help`: Show this message and exit.

## `argus-scout deliver`

Send a stored report to the configured channels.

Idempotent: a channel that already has the report is skipped (shown as duplicate) unless --force. Voice
channels follow the heartbeat policy (quiet hours, rest hours) and the daily speech caps the same as the
server does.

**Usage**:

```console
$ argus-scout deliver [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml  [required]
* `--latest`: Send the newest stored report (the only mode for now).
* `-k, --kind <str>`: The newest report of this kind (default: any).
* `--channel <list[str]>`: Send only to this channel, repeatable (default: every channel).
* `--force`: Send again to channels that already have it.
* `--help`: Show this message and exit.

## `argus-scout serve`

Run the HTTP server and the scheduler (needs the server extra).

**Usage**:

```console
$ argus-scout serve [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml; refuses to start if it is invalid. The container image sets SCOUT_CONFIG=/config/scout.yaml.  [env var: SCOUT_CONFIG]
* `--host <str>`: Bind address (default: server.host, else 127.0.0.1). Reads are open unless server.read_auth: keep it on loopback.
* `--port <int>`: TCP port (default: server.port, else 8400).
* `--help`: Show this message and exit.

## `argus-scout check`

Validate config files before scout loads them.

**Usage**:

```console
$ argus-scout check [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `tariff`: Check a tariff file loads and its periods...
* `heartbeat`: Check a heartbeat policy file parses, and...
* `approvals`: Check a standing-approvals file: every...
* `signals`: Check a signal rules file: every rule...
* `persona`: Check a persona file.
* `config`: Check scout.yaml and every policy file it...

### `argus-scout check tariff`

Check a tariff file loads and its periods cover the whole day.

**Usage**:

```console
$ argus-scout check tariff [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

### `argus-scout check heartbeat`

Check a heartbeat policy file parses, and show which keys it overrides.

**Usage**:

```console
$ argus-scout check heartbeat [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

### `argus-scout check approvals`

Check a standing-approvals file: every rule must parse.

**Usage**:

```console
$ argus-scout check approvals [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

### `argus-scout check signals`

Check a signal rules file: every rule (threshold, tariff_shift, tariff_plan) must parse, and its templates
use only allowed placeholders.

**Usage**:

```console
$ argus-scout check signals [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

### `argus-scout check persona`

Check a persona file. Scout falls back to the neutral default persona when one is invalid.

**Usage**:

```console
$ argus-scout check persona [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

### `argus-scout check config`

Check scout.yaml and every policy file it names. Secrets are reported as set or unset, never shown.

**Usage**:

```console
$ argus-scout check config [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

## `argus-scout tariff`

Inspect a time-of-use tariff.

**Usage**:

```console
$ argus-scout tariff [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--help`: Show this message and exit.

**Commands**:

* `now`: Show the active period and when it next...

### `argus-scout tariff now`

Show the active period and when it next changes.

**Usage**:

```console
$ argus-scout tariff now [OPTIONS] {path}
```

**Arguments**:

* `path <file>`: [required]

**Options**:

* `--help`: Show this message and exit.

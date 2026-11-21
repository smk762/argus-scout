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
* `serve`: Run the HTTP server (needs the ...
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
* `-s, --source <str>`: Poll only this source .
* `--ingest`: Write the events to the store instead of only printing them.
* `--help`: Show this message and exit.

## `argus-scout serve`

Run the HTTP server (needs the  extra).

**Usage**:

```console
$ argus-scout serve [OPTIONS]
```

**Options**:

* `-c, --config <file>`: scout.yaml; refuses to start if it is invalid.
* `--host <str>`: Bind address . Scout has no auth yet: keep it on loopback.
* `--port <int>`: 
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

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

## `argus-scout serve`

Run the HTTP server (needs the  extra).

**Usage**:

```console
$ argus-scout serve [OPTIONS]
```

**Options**:

* `--host <str>`: Bind address. Scout has no auth yet: keep it on loopback.  [default: 127.0.0.1]
* `--port <int>`: [default: 8400]
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

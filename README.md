# OpenC3 COSMOS Migration Plugin

![SplashScreen](/public/store_img.png)

A Python plugin for migrating historical COSMOS decommutated telemetry and command data from bin files into QuestDB time-series database.

## Overview

This plugin reads COSMOS binary packet log files (decom_logs) from S3-compatible storage and ingests the decommutated telemetry and command data into QuestDB for historical analysis and trending.

### Key Features

- Parses COSMOS binary packet log format (`.bin` and `.bin.gz` files)
- Extracts JSON-encoded decommutated telemetry and commands from packet logs
- Ingests data into QuestDB via ILP HTTP protocol
- Creates any missing table from the current packet definition before ingesting
- Routes each target to its configured `db_shard`
- Processes files in reverse chronological order (newest first)
- Rate-limits ingestion to avoid overwhelming operational systems
- Moves each file to `processed/` on success or `error/` on failure, so a failed file can be retried

## Architecture

```
S3/versitygw Logs Bucket         Plugin Microservice              QuestDB
┌─────────────────────┐       ┌─────────────────────────┐       ┌──────────────┐
│ {scope}/decom_logs/ │       │                         │       │              │
│   ├── tlm/          │──────>│  1. List files (desc)   │       │ {SCOPE}__    │
│   │   ├── INST/     │       │  2. Download .bin.gz    │       │ TLM__TARGET  │
│   │   └── ...       │       │  3. Decompress          │──────>│ __PACKET     │
│   └── cmd/          │       │  4. Parse JSON packets  │       │              │
│       ├── INST/     │       │  5. Ensure table exists │       │ {SCOPE}__    │
│       └── ...       │       │  6. Batch ingest        │       │ CMD__TARGET  │
│                     │       │  7. Move to processed/  │       │ __PACKET     │
└─────────────────────┘       └─────────────────────────┘       └──────────────┘
```

Each target is routed to the QuestDB shard named by its `db_shard` setting, so a sharded
deployment is migrated to the same hosts the real-time ingest writes to.

## COSMOS Data Type to QuestDB Type Mapping

| COSMOS Type | Bit Size          | QuestDB Type   | Notes                                                                                   |
| ----------- | ----------------- | -------------- | --------------------------------------------------------------------------------------- |
| INT         | < 32 (8, 16, ...) | int            | Small signed integers and bitfields                                                     |
| INT         | 32                | long           | Promoted to long (QuestDB uses int MIN as NULL)                                         |
| INT         | 64                | varchar        | Avoids the LONG NULL sentinel; decoded back to int on read                               |
| UINT        | < 32 (8, 16, ...) | int            | Fits in signed int                                                                      |
| UINT        | 32                | long           | Needs 33 bits for full unsigned range                                                   |
| UINT        | 64                | varchar        | Full 64-bit unsigned range, decoded back to int on read                                  |
| FLOAT       | 32                | float          | IEEE 754 single precision                                                               |
| FLOAT       | 64                | double         | IEEE 754 double precision                                                               |
| STRING      | var               | varchar        | Variable-length text                                                                    |
| TIME        | var               | varchar        | Stored as text                                                                          |
| BLOCK       | var               | varchar        | Base64-encoded binary                                                                   |
| ARRAY       | var               | varchar        | JSON-serialized arrays                                                                  |
| OBJECT      | var               | varchar        | JSON-serialized                                                                         |
| ANY         | var               | varchar        | JSON-serialized                                                                         |
| DERIVED     | var               | varies         | Based on read_conversion converted_type/bit_size; defaults to varchar (JSON-serialized) |
| Other       | var               | varchar        | JSON-serialized                                                                         |

### System Columns

Every table also carries these columns, which are written from packet metadata rather than
from a defined item:

| Column                 | QuestDB Type   | Notes                                                        |
| ---------------------- | -------------- | ------------------------------------------------------------ |
| `PACKET_TIMESECONDS`   | timestamp_ns   | Designated timestamp, from the packet time                   |
| `RECEIVED_TIMESECONDS` | timestamp_ns   | From the packet received time                                |
| `RECEIVED_COUNT`       | long           | Received count                                               |
| `COSMOS_DATA_TAG`      | symbol         | Filtering / indexing column                                  |
| `COSMOS_EXTRA`         | varchar        | Command metadata (user, approver) and extra telemetry data   |

`PACKET_TIMEFORMATTED` and `RECEIVED_TIMEFORMATTED` are not stored; they are calculated on
read from the two timestamp columns.

### Special Float Value Handling

QuestDB does not support IEEE 754 special values directly. The following sentinel values are used:

#### 64-bit (double) Sentinels

| Special Value | Sentinel Value                                         |
| ------------- | ------------------------------------------------------ |
| `Infinity`    | `1.7976931348623155e+308` (near `DBL_MAX`)             |
| `-Infinity`   | `-1.7976931348623155e+308` (near `-DBL_MAX`)           |
| `NaN`         | `-1.7976931348623153e+308` (negative, near `-DBL_MAX`) |

#### 32-bit (float) Sentinels

| Special Value | Sentinel Value                               |
| ------------- | -------------------------------------------- |
| `Infinity`    | `3.4028233e+38` (near `FLT_MAX`)             |
| `-Infinity`   | `-3.4028233e+38` (near `-FLT_MAX`)           |
| `NaN`         | `-3.4028231e+38` (negative, near `-FLT_MAX`) |

These sentinels allow special float values to be stored and retrieved without data loss.

### Other Limitations

- Integer `MIN_VALUE` is used as NULL sentinel in QuestDB, so types are promoted to keep the full range addressable
- Arrays are always stored as JSON-serialized strings for consistency
- Integers needing 64 bits or more are stored as `varchar` and converted back to integers on read.
  Tables created by COSMOS 7.0.x may still use `DECIMAL(20, 0)` for these columns; both are read correctly.

## Prerequisites

- COSMOS 7

## Installation

The easiest way to get this plugin installed is via the [App Store](https://store.openc3.com/cosmos_plugins/21).

You can also install the built gem from the [releases](https://github.com/OpenC3/openc3-cosmos-tsdb-migration/releases) page, or build it yourself with `just build-gem`.

### Configuration

The following plugin variables can be set during plugin installation:

| Variable                       | Description                                  | Default |
| ------------------------------ | -------------------------------------------- | ------- |
| `migration_batch_size`         | Packets per batch before flush and sleep     | 1000    |
| `migration_sleep_seconds`      | Sleep between batches (seconds)              | 0.1     |
| `migration_files_before_pause` | Files to process before pausing              | 20      |
| `migration_pause_seconds`      | Pause duration between file groups (seconds) | 1.0     |
| `migration_initial_delay`      | Delay before starting migration (seconds)    | 20      |
| `migration_log_level`          | Minimum microservice log level                | INFO    |

Valid log levels are `DEBUG`, `INFO`, `WARN`, `ERROR`, and `FATAL`. Set
`migration_log_level` to `DEBUG` during plugin installation to include diagnostic
messages without changing or rebuilding the plugin.

## Copying Logs from a Separate COSMOS 6 Installation

The plugin only reads from the COSMOS 7 logs bucket. If your COSMOS 7 installation is
separate from your COSMOS 6 one (for example, to test against real data without disturbing
production), first copy the `decom_logs` objects from the COSMOS 6 MinIO into the COSMOS 7
logs bucket over the S3 API. The [MinIO client](https://min.io/docs/minio/linux/reference/minio-mc.html)
(`mc`) only reads from the source, so this is safe to run against a production system.

```bash
# Source = COSMOS 6 MinIO, Destination = COSMOS 7 versitygw
mc alias set src http://<cosmos6-bucket-host>:9000 <bucket-user> <bucket-pass>
mc alias set dst http://<cosmos7-bucket-host>:9000 <bucket-user> <bucket-pass>

# Dry run first
mc mirror --preserve --dry-run src/logs/<SCOPE>/decom_logs dst/logs/<SCOPE>/decom_logs

# Copy
mc mirror --preserve src/logs/<SCOPE>/decom_logs dst/logs/<SCOPE>/decom_logs
```

Notes:

- Keep the `<SCOPE>` path identical on both sides. The plugin reads `<SCOPE>/decom_logs/...`,
  so a mismatch means it finds nothing. Repeat the copy for each scope you want to migrate.
- The destination bucket is the one named by `OPENC3_LOGS_BUCKET` (`logs` by default).
- To limit the copy, narrow the source prefix (e.g. `.../decom_logs/tlm/<TARGET>/<PACKET>`)
  or add a time filter such as `--newer-than 14d`.
- If the two buckets are on different hosts or networks, run `mc` somewhere it can reach both
  endpoints (for example, as a throwaway container attached to the destination's network,
  reaching the source over a port-forward via the host gateway).
- Finish the copy before installing the plugin. The plugin moves each file out of
  `decom_logs/` into `processed/` or `error/` once handled, so running `mc mirror` again
  afterwards copies those files back and they can be ingested a second time.

## Developers Section

Common tasks are wrapped in the [justfile](justfile). Run `just` to list them all.

### Setup

```bash
just install-dev
```

### Unit Tests

The tests use stub packets and do not need QuestDB or a running COSMOS:

```bash
just test
```

With a coverage report:

```bash
just test-cov
```

### Linting, Formatting and Type Checking

```bash
just lint          # ruff check
just format        # ruff format
just typecheck     # ty check
just check         # all of the above plus the tests
```

### Building the Plugin

Builds a gem using the version from `pyproject.toml`:

```bash
just build-gem
```

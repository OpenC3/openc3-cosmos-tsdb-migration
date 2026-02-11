# OpenC3 COSMOS Migration Plugin

![SplashScreen](/public/store_img.png)

A Python plugin for migrating historical COSMOS decommutated telemetry and command data from bin files into QuestDB time-series database.

## Overview

This plugin reads COSMOS5 binary packet log files (decom_logs) from S3-compatible storage and ingests the decommutated telemetry and command data into QuestDB for historical analysis and trending.

### Key Features

- Parses COSMOS5 binary packet log format (`.bin` and `.bin.gz` files)
- Extracts JSON-encoded decommutated telemetry and commands from packet logs
- Ingests data into QuestDB via ILP HTTP protocol
- Processes files in reverse chronological order (newest first)
- Rate-limits ingestion to avoid overwhelming operational systems

## Architecture

```
S3/versitygw Logs Bucket         Plugin Microservice              QuestDB
┌─────────────────────┐       ┌─────────────────────────┐       ┌──────────────┐
│ {scope}/decom_logs/ │       │                         │       │              │
│   ├── tlm/          │──────>│  1. List files (desc)   │       │ TLM__TARGET  │
│   │   ├── INST/     │       │  2. Download .bin.gz    │       │ __PACKET     │
│   │   └── ...       │       │  3. Decompress          │──────>│              │
│   └── cmd/          │       │  4. Parse JSON packets  │       │ CMD__TARGET  │
│       ├── INST/     │       │  5. Batch ingest        │       │ __PACKET     │
│       └── ...       │       │                         │       │              │
└─────────────────────┘       └─────────────────────────┘       └──────────────┘
```

## COSMOS Data Type to QuestDB Type Mapping

| COSMOS Type | Bit Size          | QuestDB Type   | Notes                                                                                   |
| ----------- | ----------------- | -------------- | --------------------------------------------------------------------------------------- |
| INT         | < 32 (8, 16, ...) | int            | Small signed integers and bitfields                                                     |
| INT         | 32                | long           | Promoted to long (QuestDB uses int MIN as NULL)                                         |
| INT         | 64                | DECIMAL(20, 0) | Full 64-bit signed range                                                                |
| UINT        | < 32 (8, 16, ...) | int            | Fits in signed int                                                                      |
| UINT        | 32                | long           | Needs 33 bits for full unsigned range                                                   |
| UINT        | 64                | DECIMAL(20, 0) | Full 64-bit unsigned range                                                              |
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

- Integer `MIN_VALUE` is used as NULL sentinel in QuestDB
- Arrays are always stored as JSON-serialized strings for consistency
- 64-bit integer values are sent as strings via ILP; QuestDB casts them to DECIMAL

## Prerequisites

- COSMOS 7

## Installation

The easiest way to get this plugin installed is via the [App Store](https://store.openc3.com/cosmos_plugins/21).

You can also install from [file](openc3-cosmos-tsdb-migration-1.0.0.gem) directly from this repo.

### Configuration

The following plugin variables can be set during plugin installation:

| Variable                       | Description                                  | Default |
| ------------------------------ | -------------------------------------------- | ------- |
| `migration_batch_size`         | Packets per batch before flush and sleep     | 1000    |
| `migration_sleep_seconds`      | Sleep between batches (seconds)              | 0.1     |
| `migration_files_before_pause` | Files to process before pausing              | 20      |
| `migration_pause_seconds`      | Pause duration between file groups (seconds) | 1.0     |
| `migration_initial_delay`      | Delay before starting migration (seconds)    | 20      |

## Developers Section

### Unit Tests (No QuestDB Required)

Run the bin file processor tests:

```bash
cd openc3/python
poetry run pytest ../../openc3-cosmos-migration/tests/test_bin_file_processor.py -v
```

### Integration Tests (Requires QuestDB)

1. Start the QuestDB test container:

```bash
cd openc3-cosmos-migration
docker compose -f docker-compose.test.yml up -d
```

2. Wait for QuestDB to be healthy (about 10 seconds):

```bash
docker compose -f docker-compose.test.yml ps
# Should show "healthy" status
```

3. Run the integration tests:

```bash
cd openc3/python
poetry run pytest ../../openc3-cosmos-migration/tests/test_questdb_integration.py -v
```

4. Stop QuestDB when done:

```bash
cd openc3-cosmos-migration
docker compose -f docker-compose.test.yml down
```

### Running All Tests

```bash
# Start QuestDB
cd openc3-cosmos-migration
docker compose -f docker-compose.test.yml up -d
sleep 10

# Run all tests from openc3/python
cd ../openc3/python
poetry run pytest ../../openc3-cosmos-migration/ -v

# Stop QuestDB
cd ../../openc3-cosmos-migration
docker compose -f docker-compose.test.yml down
```

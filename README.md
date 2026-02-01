# OpenC3 COSMOS Migration Microservice

A Python microservice for migrating historical COSMOS decommutated telemetry and command data from bin files into QuestDB time-series database.

## Overview

This microservice reads COSMOS5 binary packet log files (decom_logs) from S3-compatible storage and ingests the decommutated telemetry and command data into QuestDB for historical analysis and trending.

### Key Features

- Parses COSMOS5 binary packet log format (`.bin` and `.bin.gz` files)
- Extracts JSON-encoded decommutated telemetry and commands from packet logs
- Ingests data into QuestDB via ILP HTTP protocol
- Processes files in reverse chronological order (newest first)
- Tracks progress in Redis for resume capability
- Rate-limits ingestion to avoid overwhelming operational systems

## Architecture

```
S3/MinIO Logs Bucket          Migration Microservice              QuestDB
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

| COSMOS Type | Bit Size | QuestDB Type | Notes |
|-------------|----------|--------------|-------|
| INT | 8/16/32 | int | Signed integers |
| INT | 64 | long | 64-bit signed |
| INT | 3, 13 (bitfield) | int | Bitfields fit in int |
| UINT | 8/16 | int | Fits in signed int |
| UINT | 32 | long | Needs 33 bits for full range |
| UINT | 64 | varchar | Exceeds signed long range |
| FLOAT | 32 | float | IEEE 754 single precision |
| FLOAT | 64 | double | IEEE 754 double precision |
| STRING | var | varchar | Variable-length text |
| BLOCK | var | varchar | Base64-encoded binary |
| BOOL | N/A | boolean | Native boolean |
| ARRAY | var | varchar | JSON-serialized arrays |
| OBJECT | var | varchar | JSON-serialized |
| ANY | var | varchar | JSON-serialized |

### Special Float Value Handling

QuestDB does not support IEEE 754 special values directly. The following sentinel values are used:

| Special Value | Sentinel Value |
|---------------|----------------|
| `Infinity` | `1.7976931348623157e+308` (near `DBL_MAX`) |
| `-Infinity` | `-1.7976931348623157e+308` (near `-DBL_MAX`) |
| `NaN` | `1.7976931348623156e+308` (near `DBL_MAX`, slightly less) |

These sentinels allow special float values to be stored and retrieved without data loss.

### Other Limitations

- Integer `MIN_VALUE` is used as NULL sentinel in QuestDB
- Arrays are always stored as JSON-serialized strings for consistency

## Prerequisites

- Python 3.10+
- Docker (for QuestDB integration testing)
- Poetry

## Installation

The migration microservice depends on the `openc3` Python package. Install using Poetry:

```bash
# From the cosmos repository root, install the openc3 package
cd openc3/python
poetry install

# Install test dependencies (pytest, psycopg, questdb are included in openc3)
# Tests are run from the openc3/python poetry environment
```

## Running Tests

All tests are run using the `openc3/python` Poetry environment.

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

## Configuration

The microservice uses the following environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `OPENC3_TSDB_HOSTNAME` | QuestDB hostname | Required |
| `OPENC3_TSDB_INGEST_PORT` | HTTP ILP ingest port | 9000 |
| `OPENC3_TSDB_QUERY_PORT` | PostgreSQL wire protocol port | 8812 |
| `OPENC3_TSDB_USERNAME` | QuestDB username | Required |
| `OPENC3_TSDB_PASSWORD` | QuestDB password | Required |
| `MIGRATION_ENABLED` | Enable migration processing | false |
| `MIGRATION_BATCH_SIZE` | Packets per batch | 1000 |
| `MIGRATION_SLEEP_SECONDS` | Sleep between batches | 0.5 |

## File Structure

```
openc3-cosmos-migration/
├── README.md                    # This file
├── docker-compose.test.yml      # QuestDB container for testing
├── migration_microservice.py    # Main microservice class
├── bin_file_processor.py        # Bin file parsing logic
└── tests/
    ├── conftest.py              # Pytest fixtures for QuestDB
    ├── test_bin_file_processor.py   # Unit tests for bin processor
    └── test_questdb_integration.py  # Integration tests for all COSMOS types
```

## Related Components

- `openc3/python/openc3/utilities/questdb_client.py` - Shared QuestDB client
- `openc3/python/openc3/logs/packet_log_reader.py` - Binary packet log parser
- `openc3/python/openc3/logs/packet_log_writer.py` - Binary packet log writer
- `openc3/python/openc3/packets/json_packet.py` - JSON packet representation

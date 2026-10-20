# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See
# LICENSE.md for more details
#
# This file may also be used under the terms of a commercial license
# if purchased from OpenC3, Inc.

"""
Migration microservice for ingesting historical bin file data into QuestDB.

This microservice:
- Pulls decom_logs bin files from S3-compatible storage (both telemetry and commands)
- Parses COSMOS log file binary format
- Ingests data into QuestDB with type casting based on existing column types
- Uses TLM__ and CMD__ prefixes for table names
- Stores arrays as JSON-serialized strings
- Handles special float values (+/-Infinity, NaN) via sentinel values
- Processes files in reverse chronological order (newest first)
- Moves processed files to a processed/ folder (and failed files to error/)
- Resumes where it left off by only listing files still under decom_logs/
- Routes each target to its configured db_shard
- Rate-limits ingestion to avoid overwhelming the operational system
"""

import contextlib
import gzip
import json
import os
import re
import traceback
from datetime import UTC, datetime

from bin_file_processor import (
    BinFileProcessor,
    extract_timestamp_from_filename,
    parse_target_packet_from_filename,
)
from openc3.api import (
    get_all_cmd_names,
    get_all_tlm_names,
    get_cmd,
    get_target_names,
    get_tlm,
)
from openc3.microservices.microservice import Microservice
from openc3.utilities.bucket import Bucket
from openc3.utilities.questdb_client import QuestDBClient
from openc3.utilities.sleeper import Sleeper
from openc3.utilities.store import Store
from questdb.ingress import IngressError


def configure_log_level(logger, log_level: str) -> None:
    """Set and validate the migration microservice's logger level."""
    normalized_level = log_level.upper()
    valid_log_levels = {
        "DEBUG": logger.DEBUG,
        "INFO": logger.INFO,
        "WARN": logger.WARN,
        "ERROR": logger.ERROR,
        "FATAL": logger.FATAL,
    }
    try:
        logger.level = valid_log_levels[normalized_level]
    except KeyError as error:
        valid_values = ", ".join(valid_log_levels)
        raise ValueError(
            f"Invalid LOG_LEVEL {log_level!r}; expected one of: {valid_values}"
        ) from error


class TsdbMigration(Microservice):
    """
    Migration microservice for ingesting historical bin file data into QuestDB.
    """

    # QuestDB returns "table does not exist" when QDB_LINE_AUTO_CREATE_NEW_TABLES=false
    # and an ILP write targets a table that was never created (or was dropped from the
    # admin UI). Matches TsdbMicroservice so both recover the same way.
    MISSING_TABLE_RE = re.compile(r"table\s+does\s+not\s+exist", re.IGNORECASE)

    # Reverse of the unit suffixes QuestDBClient accepts in retain_time, used to hand an
    # existing table's TTL back to create_table unchanged.
    TTL_UNIT_SUFFIXES = {"HOUR": "h", "DAY": "d", "WEEK": "w", "MONTH": "M", "YEAR": "y"}

    def __init__(self, name):
        super().__init__(name)

        # Default configuration
        self.batch_size = 1000
        self.sleep_seconds = 0.5
        self.files_before_pause = 10
        self.pause_seconds = 30.0
        self.initial_delay = 60
        self.log_level = "INFO"

        # Process options from plugin.txt
        for option in self.config.get("options", []):
            match option[0].upper():
                case "BATCH_SIZE":
                    self.batch_size = int(option[1])
                case "SLEEP_SECONDS":
                    self.sleep_seconds = float(option[1])
                case "FILES_BEFORE_PAUSE":
                    self.files_before_pause = int(option[1])
                case "PAUSE_SECONDS":
                    self.pause_seconds = float(option[1])
                case "INITIAL_DELAY":
                    self.initial_delay = int(option[1])
                case "LOG_LEVEL":
                    self.log_level = option[1].upper()
                case _:
                    self.logger.error(f"Unknown option passed to microservice {name}: {option}")

        configure_log_level(self.logger, self.log_level)
        self.logger.debug(f"Debug logging enabled for microservice {name}")

        self.sleeper = Sleeper()
        self.bucket = Bucket.getClient()
        # QuestDB clients keyed by db_shard. Targets can be assigned to different
        # shards so each one needs its own connection, resolved lazily per target.
        self.questdb_clients = {}
        self.bin_processor = BinFileProcessor(logger=self.logger)

        # Valid targets/packets (current system definitions)
        self.valid_targets = set()
        self.valid_tlm_packets = {}  # {target_name: set(packet_names)}
        self.valid_cmd_packets = {}  # {target_name: set(packet_names)}

        # Statistics
        self.files_processed = 0
        self.packets_ingested = 0
        self.errors_count = 0

        # Track which table schemas have been loaded for column type tracking
        self._loaded_schemas = set()

    def _load_valid_targets_packets(self):
        """Load current target/packet definitions from the system (both telemetry and commands)."""
        try:
            self.valid_targets = set(get_target_names(scope=self.scope))
            self.valid_tlm_packets = {}
            self.valid_cmd_packets = {}

            for target in self.valid_targets:
                # Load telemetry packets
                try:
                    self.valid_tlm_packets[target] = set(
                        get_all_tlm_names(target, hidden=True, scope=self.scope)
                    )
                except Exception:
                    self.valid_tlm_packets[target] = set()

                # Load command packets
                try:
                    self.valid_cmd_packets[target] = set(
                        get_all_cmd_names(target, hidden=True, scope=self.scope)
                    )
                except Exception:
                    self.valid_cmd_packets[target] = set()

            total_tlm = sum(len(p) for p in self.valid_tlm_packets.values())
            total_cmd = sum(len(p) for p in self.valid_cmd_packets.values())
            self.logger.info(
                f"Loaded {len(self.valid_targets)} targets with "
                f"{total_tlm} telemetry packets and {total_cmd} command packets"
            )
        except Exception as e:
            self.logger.error(f"Failed to load target/packet definitions: {e}")
            raise

    def _client_for_shard(self, db_shard: int) -> QuestDBClient:
        """
        Get or create a connected QuestDBClient for the given db_shard.

        Each shard is a separate QuestDB host (OPENC3_TSDB_HOSTNAME with SHARDNUM
        substitution) so a target assigned to shard 1 cannot be written through the
        shard 0 connection.
        """
        client = self.questdb_clients.get(db_shard)
        if client is None:
            client = QuestDBClient(
                logger=self.logger,
                name=f"Microservice {self.name}",
                batch_size=self.batch_size,
                db_shard=db_shard,
            )
            client.connect_ingest()
            client.connect_query()
            self.questdb_clients[db_shard] = client
            self.logger.info(f"Connected to QuestDB db_shard {db_shard}")
        return client

    def _close_clients(self):
        """Close every per-shard QuestDB client."""
        for client in self.questdb_clients.values():
            with contextlib.suppress(Exception):
                client.close()
        self.questdb_clients.clear()

    def _existing_retain_time(self, client: QuestDBClient, table_name: str):
        """
        Read the TTL already set on a table and return it in create_table's compact format.

        create_table treats retain_time=None as "this table should have no TTL" and will
        ALTER the TTL away. The migration is not the owner of retention (the TSDB
        microservice applies CMD/TLM_DECOM_RETAIN_TIME from the plugin config), so the
        current value is handed straight back to leave it untouched.

        Returns a string like "30d", or None if the table has no TTL or does not exist.
        """
        existing = client._get_existing_ttl(table_name)
        if not existing:
            return None
        value, unit = existing
        suffix = self.TTL_UNIT_SUFFIXES.get(unit)
        if not value or not suffix:
            return None
        return f"{value}{suffix}"

    def _ensure_table(
        self, client: QuestDBClient, target_name: str, packet_name: str, cmd_or_tlm: str
    ) -> str:
        """
        Make sure the table exists and the client knows how to convert each column.

        Delegates to QuestDBClient.create_table, which is the same call the real-time
        TsdbMicroservice makes. It creates the table when missing (ILP auto create is
        disabled, so writing to a missing table just fails) and, as a side effect,
        populates the json_columns / varchar_columns / float_bit_sizes tracking from the
        packet definition. Deriving that tracking from the definition rather than from
        SHOW COLUMNS is what keeps BLOCK items base64 encoded and DERIVED __C items JSON
        encoded exactly as the real-time path writes them.

        Returns the sanitized table name. Raises if the table could not be created.
        """
        table_name, _ = QuestDBClient.sanitize_table_name(
            target_name, packet_name, cmd_or_tlm=cmd_or_tlm, scope=self.scope
        )
        if table_name in self._loaded_schemas:
            return table_name

        if cmd_or_tlm == "CMD":
            packet = get_cmd(target_name, packet_name, scope=self.scope)
        else:
            packet = get_tlm(target_name, packet_name, scope=self.scope)

        client.create_table(
            target_name,
            packet_name,
            packet,
            cmd_or_tlm,
            retain_time=self._existing_retain_time(client, table_name),
            scope=self.scope,
        )
        # Only cache on success so a transient failure doesn't permanently leave this
        # table without column type tracking.
        self._loaded_schemas.add(table_name)
        return table_name

    def _is_command_file(self, file_path: str) -> bool:
        """Determine if a file is from the command logs (vs telemetry)."""
        return "/decom_logs/cmd/" in file_path

    def _should_process_file(self, filename: str) -> bool:
        """Check if a file should be processed based on current system definitions."""
        target_name, packet_name = parse_target_packet_from_filename(filename)
        if target_name is None or packet_name is None:
            self.logger.debug(f"Could not parse target/packet from filename: {filename}")
            return False

        if target_name not in self.valid_targets:
            self.logger.debug(f"Skipping obsolete target: {target_name}")
            return False

        # "ALL" means the file contains all packets for the target - always process
        if packet_name == "ALL":
            return True

        # Check the appropriate packet set based on cmd vs tlm
        cmd_or_tlm = "CMD" if self._is_command_file(filename) else "TLM"
        if not self._is_valid_packet(target_name, packet_name, cmd_or_tlm):
            self.logger.debug(f"Skipping obsolete packet: {target_name}/{packet_name}")
            return False

        return True

    def _is_valid_packet(self, target_name: str, packet_name: str, cmd_or_tlm: str) -> bool:
        """Check a single target/packet against the current system definitions."""
        if target_name not in self.valid_targets:
            return False
        if cmd_or_tlm == "CMD":
            return packet_name in self.valid_cmd_packets.get(target_name, set())
        return packet_name in self.valid_tlm_packets.get(target_name, set())

    def _list_files_recursive(self, bucket: str, prefix: str) -> list:
        """Recursively list all files under a prefix."""
        all_files = []
        dirs_to_process = [prefix]

        while dirs_to_process:
            current_prefix = dirs_to_process.pop(0)
            self.logger.info(f"Listing files under: {current_prefix}")
            try:
                dir_list, file_list = self.bucket.list_files(bucket=bucket, path=current_prefix)

                # Add subdirectories to process (dir_list contains strings)
                for dir_name in dir_list:
                    if dir_name:
                        dirs_to_process.append(f"{current_prefix}{dir_name}/")

                # Add files (file_list contains dicts with 'name' key or strings)
                for file_info in file_list:
                    if isinstance(file_info, dict):
                        filename = file_info.get("name", "")
                    else:
                        filename = file_info
                    if filename:
                        self.logger.info(f"found file: {current_prefix}{filename}")
                        all_files.append(f"{current_prefix}{filename}")

            except Exception as e:
                self.logger.debug(f"Error listing {current_prefix}: {e}")

        return all_files

    def _list_decom_files(self) -> list:
        """List all decom log files in the bucket, sorted by timestamp descending (newest first)."""
        files = []
        logs_bucket = os.environ.get("OPENC3_LOGS_BUCKET", "logs")

        # List files in decom_logs/tlm/ directory (recursively)
        try:
            prefix = f"{self.scope}/decom_logs/tlm/"
            all_files = self._list_files_recursive(logs_bucket, prefix)

            for filepath in all_files:
                if filepath.endswith(".bin") or filepath.endswith(".bin.gz"):
                    files.append(filepath)
        except Exception as e:
            self.logger.error(f"Error listing decom_logs/tlm/: {e}")

        # Also check decom_logs/cmd/ if we want to migrate commands (recursively)
        try:
            prefix = f"{self.scope}/decom_logs/cmd/"
            all_files = self._list_files_recursive(logs_bucket, prefix)

            for filepath in all_files:
                if filepath.endswith(".bin") or filepath.endswith(".bin.gz"):
                    files.append(filepath)
        except Exception as e:
            self.logger.debug(f"No decom_logs/cmd/ or error: {e}")

        # Sort by timestamp descending (newest first)
        files.sort(key=lambda f: extract_timestamp_from_filename(f), reverse=True)

        self.logger.info(f"Found {len(files)} decom log files to process")
        return files

    def _download_file(self, bucket_path: str) -> bytes:
        """Download a file from the bucket and return its contents."""
        logs_bucket = os.environ.get("OPENC3_LOGS_BUCKET", "logs")
        response = self.bucket.get_object(bucket=logs_bucket, key=bucket_path)

        if isinstance(response, dict) and "Body" in response:
            data = response["Body"].read()
        else:
            data = response

        # Decompress if gzipped
        if bucket_path.endswith(".gz"):
            data = gzip.decompress(data)

        return data

    def _move_to_processed(self, original_path: str):
        """Move a processed file to the processed/ folder."""
        logs_bucket = os.environ.get("OPENC3_LOGS_BUCKET", "logs")

        # Replace decom_logs with processed/decom_logs
        processed_path = original_path.replace("/decom_logs/", "/processed/decom_logs/", 1)

        try:
            # Read the original file
            response = self.bucket.get_object(bucket=logs_bucket, key=original_path)
            if isinstance(response, dict) and "Body" in response:
                data = response["Body"].read()
            else:
                data = response

            # Write to processed location
            self.bucket.put_object(bucket=logs_bucket, key=processed_path, body=data)

            # Delete original
            self.bucket.delete_object(bucket=logs_bucket, key=original_path)
            self.logger.debug(f"Moved {original_path} to {processed_path}")
        except Exception as e:
            self.logger.warn(f"Failed to move file to processed: {e}")

    def _move_to_error(self, original_path: str):
        """Move a file that had errors to the error/ folder."""
        logs_bucket = os.environ.get("OPENC3_LOGS_BUCKET", "logs")

        # Replace decom_logs with error/decom_logs
        error_path = original_path.replace("/decom_logs/", "/error/decom_logs/", 1)

        try:
            # Read the original file
            response = self.bucket.get_object(bucket=logs_bucket, key=original_path)
            if isinstance(response, dict) and "Body" in response:
                data = response["Body"].read()
            else:
                data = response

            # Write to error location
            self.bucket.put_object(bucket=logs_bucket, key=error_path, body=data)

            # Delete original
            self.bucket.delete_object(bucket=logs_bucket, key=original_path)
            self.logger.debug(f"Moved {original_path} to {error_path}")
        except Exception as e:
            self.logger.warn(f"Failed to move file to error: {e}")

    def _handle_ingress_error(self, client: QuestDBClient, error, packet, cmd_or_tlm: str) -> bool:
        """
        Recover from an IngressError raised while writing or flushing a row.

        Mirrors TsdbMicroservice: a missing table is recreated from the packet definition
        and the buffered rows replayed, anything else is delegated to the client which
        casts the offending value to the column type and retries.

        Returns True if the buffered rows were recovered, False if they were dropped.
        """
        if self.MISSING_TABLE_RE.search(str(error)):
            # ILP auto create is disabled so a dropped table fails every write until it
            # is recreated with the proper COSMOS schema and designated timestamp.
            self.logger.warn(f"QuestDB: Missing table IngressError: {error}")
            if packet is None:
                self._reset_after_error(client)
                return False
            try:
                table_name, _ = QuestDBClient.sanitize_table_name(
                    packet.target_name, packet.packet_name, cmd_or_tlm=cmd_or_tlm, scope=self.scope
                )
                self._loaded_schemas.discard(table_name)
                self._ensure_table(client, packet.target_name, packet.packet_name, cmd_or_tlm)
                pending_count = len(client.pending_rows)
                client._reconnect_and_retry_pending()
                client.flush()
                self.logger.info(
                    f"QuestDB: Recreated missing table {table_name}, "
                    f"replayed {pending_count} pending rows"
                )
                return True
            except Exception as exc:
                self.logger.error(
                    f"QuestDB: Failed to recreate missing table: {exc}\n{traceback.format_exc()}"
                )
                self._reset_after_error(client)
                return False

        # Cast the value to fit the column type and retry. Returns False when the rows
        # could not be recovered, in which case they have been dropped.
        return client.handle_ingress_error(error)

    def _reset_after_error(self, client: QuestDBClient):
        """Discard buffered rows and reopen the ILP sender."""
        client.pending_rows.clear()
        try:
            client.connect_ingest()
        except Exception:
            self.logger.error(f"QuestDB: Failed to reconnect:\n{traceback.format_exc()}")

    def _process_file(self, file_path: str) -> tuple:
        """
        Process a single bin file and ingest its data into QuestDB.

        Returns tuple of (packets_ingested, had_error, cancelled). had_error is True if any
        row was dropped, so the caller can route the file to error/ instead of processed/
        and leave the data available for a retry. cancelled is True if shutdown stopped
        the file part way, so the caller leaves it in decom_logs/ to be picked up again.
        Re-ingesting the rows already written is safe because tables DEDUP on
        PACKET_TIMESECONDS.
        """
        packets_in_file = 0
        had_error = False
        cancelled = False

        # Used only when a packet doesn't carry its own cmd_or_tlm
        path_cmd_or_tlm = "CMD" if self._is_command_file(file_path) else "TLM"
        clients_used = {}
        last_packet = None
        last_cmd_or_tlm = path_cmd_or_tlm

        try:
            # Download and decompress file
            data = self._download_file(file_path)

            for packet in self.bin_processor.process_bytes(data):
                if self.cancel_thread:
                    cancelled = True
                    break

                # The packet records whether it is a command or telemetry. Fall back to
                # the bucket path, which matters for __ALL__ files holding both.
                cmd_or_tlm = (
                    packet.cmd_or_tlm if packet.cmd_or_tlm in ("CMD", "TLM") else path_cmd_or_tlm
                )
                # An __ALL__ file holds every packet logged for the target, including
                # ones since removed from the config. Skip those instead of failing the
                # whole file, matching how obsolete packets are filtered by filename.
                if not self._is_valid_packet(packet.target_name, packet.packet_name, cmd_or_tlm):
                    self.logger.debug(
                        f"Skipping obsolete packet: {packet.target_name}/{packet.packet_name}"
                    )
                    continue

                last_packet = packet
                last_cmd_or_tlm = cmd_or_tlm

                # Targets can be assigned to different shards, so pick the client per packet.
                # Store.db_shard_for_target caches lookups for a minute.
                db_shard = Store.db_shard_for_target(packet.target_name, scope=self.scope)
                client = self._client_for_shard(db_shard)
                clients_used[db_shard] = client

                # Create the table if needed and register column types for proper casting
                table_name = self._ensure_table(
                    client, packet.target_name, packet.packet_name, cmd_or_tlm
                )

                # Convert JSON data to QuestDB columns
                columns = client.process_json_data(packet.json_hash, table_name)

                if not columns:
                    continue

                # COSMOS_EXTRA carries command metadata (user, approver) and any telemetry
                # data that doesn't fit in a defined item
                if packet.extra:
                    columns["COSMOS_EXTRA"] = json.dumps(packet.extra)

                # Write with both the packet time and the received time. Without the
                # received time RECEIVED_TIMESECONDS and RECEIVED_TIMEFORMATTED are
                # missing from every migrated row.
                flushed = False
                try:
                    flushed = client.write_row(
                        table_name, columns, packet.time_nsec, packet.received_time_nsec
                    )
                    packets_in_file += 1
                except IngressError as error:
                    if self._handle_ingress_error(client, error, packet, cmd_or_tlm):
                        packets_in_file += 1
                    else:
                        had_error = True

                # write_row auto flushes once batch_size rows are buffered. Sleep after
                # each flush to keep the operational system responsive.
                if flushed and self.sleeper.sleep(self.sleep_seconds):
                    cancelled = True
                    break

            # Final flush of every shard this file wrote to
            for client in clients_used.values():
                try:
                    client.flush()
                except IngressError as error:
                    if not self._handle_ingress_error(client, error, last_packet, last_cmd_or_tlm):
                        had_error = True

        except Exception as e:
            self.logger.error(f"Error processing file {file_path}: {e}\n{traceback.format_exc()}")
            had_error = True

        if had_error:
            self.errors_count += 1

        return packets_in_file, had_error, cancelled

    def run(self):
        """Main run loop for the migration microservice."""
        # Allow the other target processes to start before running the microservice
        if self.sleeper.sleep(self.initial_delay):
            return

        self.logger.info("Starting QuestDB migration microservice")
        self.started_at = datetime.now(UTC).isoformat()
        self.state = "STARTING"

        try:
            # QuestDB connections are opened lazily per db_shard as targets are encountered

            # Load current target/packet definitions
            self._load_valid_targets_packets()

            # Get list of files to process
            files = self._list_decom_files()

            # Filter to valid targets/packets
            files = [f for f in files if self._should_process_file(f)]
            self.logger.info(f"After filtering: {len(files)} files to process")

            # Process files
            files_since_pause = 0
            total_files = len(files)
            for idx, file_path in enumerate(files):
                if self.cancel_thread:
                    break

                self.state = f"MIGRATING {idx + 1}/{total_files}"
                self.logger.info(f"Processing: {file_path}")

                packets, had_error, cancelled = self._process_file(file_path)
                self.packets_ingested += packets
                if cancelled:
                    # Leave the partly read file under decom_logs/ so it is retried on restart
                    self.logger.info(f"Cancelled: {file_path} left in place for retry")
                    break
                self.files_processed += 1
                files_since_pause += 1

                # Move to appropriate folder based on success/failure
                if had_error:
                    self._move_to_error(file_path)
                else:
                    self._move_to_processed(file_path)

                self.logger.info(
                    f"Completed: {file_path} - {packets} packets "
                    f"(total: {self.packets_ingested} packets, {self.files_processed} files)"
                    f"{' (WITH ERRORS)' if had_error else ''}"
                )

                # Periodic pause to let operational system catch up
                if files_since_pause >= self.files_before_pause:
                    self.state = "PAUSED"
                    self.logger.info(f"Pausing for {self.pause_seconds}s to reduce system load...")
                    if self.sleeper.sleep(self.pause_seconds):
                        break
                    files_since_pause = 0

            # Final summary
            self.state = "COMPLETE"
            self.logger.info(
                f"Migration complete! "
                f"Files: {self.files_processed}, "
                f"Packets: {self.packets_ingested}, "
                f"Errors: {self.errors_count}"
            )

            # Keep running idle so the microservice doesn't restart
            while not self.cancel_thread:
                if self.sleeper.sleep(60):
                    break

        except Exception as e:
            self.error = e
            self.state = "ERROR"
            self.logger.error(f"Migration error: {e}\n{traceback.format_exc()}")
        finally:
            self._close_clients()

    def shutdown(self, state="STOPPED"):
        """Graceful shutdown."""
        self.sleeper.cancel()
        self._close_clients()
        super().shutdown(state)


if __name__ == "__main__":
    TsdbMigration.class_run()

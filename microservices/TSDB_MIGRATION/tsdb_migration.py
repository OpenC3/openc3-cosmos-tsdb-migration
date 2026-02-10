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
- Parses COSMOS5 binary format
- Ingests data into QuestDB with type casting based on existing column types
- Uses TLM__ and CMD__ prefixes for table names
- Stores arrays as JSON-serialized strings
- Handles special float values (+/-Infinity, NaN) via sentinel values
- Processes files in reverse chronological order (newest first)
- Moves processed files to a processed/ folder
- Tracks progress in Redis for resume capability
- Rate-limits ingestion to avoid overwhelming the operational system
"""

import gzip
import os
import traceback
from datetime import datetime, timezone

from questdb.ingress import IngressError
from openc3.microservices.microservice import Microservice
from openc3.utilities.bucket import Bucket
from openc3.utilities.sleeper import Sleeper
from openc3.utilities.questdb_client import QuestDBClient
from openc3.api import *

from bin_file_processor import (
    BinFileProcessor,
    extract_timestamp_from_filename,
    parse_target_packet_from_filename,
)


class TsdbMigration(Microservice):
    """
    Migration microservice for ingesting historical bin file data into QuestDB.
    """

    def __init__(self, name):
        super().__init__(name)

        # Default configuration
        self.batch_size = 1000
        self.sleep_seconds = 0.5
        self.files_before_pause = 10
        self.pause_seconds = 30.0
        self.initial_delay = 60

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
                case _:
                    self.logger.error(
                        f"Unknown option passed to microservice {name}: {option}"
                    )

        self.sleeper = Sleeper()
        self.bucket = Bucket.getClient()
        self.questdb = QuestDBClient(logger=self.logger)
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
                        get_all_tlm_names(target, scope=self.scope)
                    )
                except Exception:
                    self.valid_tlm_packets[target] = set()

                # Load command packets
                try:
                    self.valid_cmd_packets[target] = set(
                        get_all_cmd_names(target, scope=self.scope)
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

    def _load_table_schema(self, table_name: str):
        """
        Load existing table schema from QuestDB and register column types.

        Populates the QuestDB client's column tracking dictionaries so that
        convert_value() casts data correctly during migration:
        - json_columns: VARCHAR columns needing JSON serialization (DERIVED, arrays)
        - varchar_columns: VARCHAR columns for state __C values (cast non-strings to str)
        - decimal_int_columns: DECIMAL columns for 64-bit integers (cast ints to str)
        - float_bit_sizes: FLOAT/DOUBLE columns for proper inf/nan sentinel encoding
        """
        if table_name in self._loaded_schemas:
            return

        try:
            with self.questdb.query.cursor() as cur:
                cur.execute(f'SHOW COLUMNS FROM "{table_name}"')
                for row in cur.fetchall():
                    col_name, col_type = row[0], row[1]
                    col_key = f"{table_name}__{col_name}"
                    upper_type = col_type.upper()
                    if upper_type == "VARCHAR":
                        # __C columns are state columns (non-string values need str())
                        # __F columns are formatted columns (always strings, no tracking needed)
                        # Other VARCHAR columns are DERIVED/array items (need JSON serialization)
                        if col_name.endswith("__C"):
                            self.questdb.varchar_columns[col_key] = True
                        elif not col_name.endswith("__F"):
                            self.questdb.json_columns[col_key] = True
                    elif upper_type == "FLOAT":
                        self.questdb.float_bit_sizes[col_key] = 32
                    elif upper_type == "DOUBLE":
                        self.questdb.float_bit_sizes[col_key] = 64
                    elif upper_type == "DECIMAL":
                        self.questdb.decimal_int_columns[col_key] = True
        except Exception as e:
            self.logger.debug(f"Could not load schema for {table_name}: {e}")

        self._loaded_schemas.add(table_name)

    def _is_command_file(self, file_path: str) -> bool:
        """Determine if a file is from the command logs (vs telemetry)."""
        return "/decom_logs/cmd/" in file_path

    def _should_process_file(self, filename: str) -> bool:
        """Check if a file should be processed based on current system definitions."""
        target_name, packet_name = parse_target_packet_from_filename(filename)
        if target_name is None or packet_name is None:
            self.logger.debug(
                f"Could not parse target/packet from filename: {filename}"
            )
            return False

        if target_name not in self.valid_targets:
            self.logger.debug(f"Skipping obsolete target: {target_name}")
            return False

        # "ALL" means the file contains all packets for the target - always process
        if packet_name == "ALL":
            return True

        # Check the appropriate packet set based on cmd vs tlm
        if self._is_command_file(filename):
            valid_packets = self.valid_cmd_packets.get(target_name, set())
        else:
            valid_packets = self.valid_tlm_packets.get(target_name, set())

        if packet_name not in valid_packets:
            self.logger.debug(f"Skipping obsolete packet: {target_name}/{packet_name}")
            return False

        return True

    def _list_files_recursive(self, bucket: str, prefix: str) -> list:
        """Recursively list all files under a prefix."""
        all_files = []
        dirs_to_process = [prefix]

        while dirs_to_process:
            current_prefix = dirs_to_process.pop(0)
            self.logger.info(f"Listing files under: {current_prefix}")
            try:
                dir_list, file_list = self.bucket.list_files(
                    bucket=bucket, path=current_prefix
                )

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
        processed_path = original_path.replace(
            "/decom_logs/", "/processed/decom_logs/", 1
        )

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

    def _process_file(self, file_path: str) -> tuple:
        """
        Process a single bin file and ingest its data into QuestDB.

        Returns tuple of (packets_ingested, had_error).
        """
        packets_in_file = 0
        had_error = False

        # Determine if this is a command or telemetry file
        is_command = self._is_command_file(file_path)
        cmd_or_tlm = "CMD" if is_command else "TLM"

        try:
            # Download and decompress file
            data = self._download_file(file_path)

            # Process the file
            batch_count = 0
            for packet in self.bin_processor.process_bytes(data):
                if self.cancel_thread:
                    break

                # Get table name with appropriate prefix (CMD__ or TLM__)
                table_name, _ = QuestDBClient.sanitize_table_name(
                    packet.target_name, packet.packet_name, cmd_or_tlm=cmd_or_tlm
                )

                # Load table schema to register column types for proper casting
                self._load_table_schema(table_name)

                # Convert JSON data to QuestDB columns
                columns = self.questdb.process_json_data(packet.json_hash, table_name)

                if not columns:
                    continue

                # Write row and handle type mismatches via casting
                try:
                    self.questdb.write_row(table_name, columns, packet.time_nsec)
                except IngressError as error:
                    self.questdb.handle_ingress_error(
                        error, table_name, columns, packet.time_nsec
                    )

                packets_in_file += 1
                batch_count += 1

                # Flush and sleep periodically
                if batch_count >= self.batch_size:
                    try:
                        self.questdb.flush()
                    except IngressError as error:
                        self.questdb.handle_ingress_error(
                            error, table_name, columns, packet.time_nsec
                        )
                    if self.sleeper.sleep(self.sleep_seconds):
                        break
                    batch_count = 0

            # Final flush
            try:
                self.questdb.flush()
            except IngressError as error:
                self.questdb.handle_ingress_error(
                    error, table_name, columns, packet.time_nsec
                )

        except Exception as e:
            self.logger.error(
                f"Error processing file {file_path}: {e}\n{traceback.format_exc()}"
            )
            self.errors_count += 1
            had_error = True

        return packets_in_file, had_error

    def run(self):
        """Main run loop for the migration microservice."""
        # Allow the other target processes to start before running the microservice
        if self.sleeper.sleep(self.initial_delay):
            return

        self.logger.info("Starting QuestDB migration microservice")
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.state = "STARTING"

        try:
            # Connect to QuestDB
            self.questdb.connect_ingest()
            self.questdb.connect_query()

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

                packets, had_error = self._process_file(file_path)
                self.packets_ingested += packets
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
                    self.logger.info(
                        f"Pausing for {self.pause_seconds}s to reduce system load..."
                    )
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
            self.questdb.close()

    def shutdown(self):
        """Graceful shutdown."""
        self.sleeper.cancel()
        self.questdb.close()
        super().shutdown()


if __name__ == "__main__":
    TsdbMigration.class_run()

# Copyright 2026 OpenC3, Inc.
# All Rights Reserved.

import pytest
from tsdb_migration import configure_log_level


class StubLogger:
    DEBUG = 0
    INFO = 1
    WARN = 2
    ERROR = 3
    FATAL = 4

    def __init__(self):
        self.level = self.INFO
        self.messages = []

    def debug(self, message=None, **kwargs):
        self.messages.append(("DEBUG", message))

    def info(self, message=None, **kwargs):
        self.messages.append(("INFO", message))

    def warn(self, message=None, **kwargs):
        self.messages.append(("WARN", message))

    def error(self, message=None, **kwargs):
        self.messages.append(("ERROR", message))


@pytest.mark.parametrize(
    ("configured_level", "expected_level"),
    [
        ("debug", StubLogger.DEBUG),
        ("INFO", StubLogger.INFO),
        ("WARN", StubLogger.WARN),
        ("ERROR", StubLogger.ERROR),
        ("FATAL", StubLogger.FATAL),
    ],
)
def test_configure_log_level(configured_level, expected_level):
    logger = StubLogger()

    configure_log_level(logger, configured_level)

    assert logger.level == expected_level


def test_configure_log_level_rejects_unknown_value():
    with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
        configure_log_level(StubLogger(), "trace")


class StubClient:
    """Minimal stand-in for QuestDBClient."""

    def __init__(self, ttl=None):
        self.ttl = ttl
        self.created = []
        self.pending_rows = []
        self.handled = []
        self.handle_result = True
        self.replayed = 0
        self.flushed = 0
        self.reconnects = 0
        self.create_error = None

    def _get_existing_ttl(self, table_name):
        return self.ttl

    def create_table(
        self, target_name, packet_name, packet, cmd_or_tlm, retain_time=None, scope=None
    ):
        if self.create_error:
            raise self.create_error
        self.created.append((target_name, packet_name, cmd_or_tlm, retain_time, scope))

    def handle_ingress_error(self, error):
        self.handled.append(error)
        return self.handle_result

    def _reconnect_and_retry_pending(self):
        self.replayed += 1

    def flush(self):
        self.flushed += 1

    def connect_ingest(self):
        self.reconnects += 1


class StubPacket:
    def __init__(self, target_name="INST", packet_name="HEALTH_STATUS", cmd_or_tlm="TLM"):
        self.target_name = target_name
        self.packet_name = packet_name
        self.cmd_or_tlm = cmd_or_tlm


def build_migration(monkeypatch=None):
    """Build a TsdbMigration without running Microservice.__init__."""
    from tsdb_migration import TsdbMigration

    migration = TsdbMigration.__new__(TsdbMigration)
    migration.scope = "DEFAULT"
    migration.name = "TSDB_MIGRATION"
    # Deliberate test double in place of the real Logger
    migration.logger = StubLogger()  # ty: ignore[invalid-assignment]
    migration.batch_size = 1000
    migration.questdb_clients = {}
    migration._loaded_schemas = set()
    migration.valid_targets = {"INST"}
    migration.valid_tlm_packets = {"INST": {"HEALTH_STATUS"}}
    migration.valid_cmd_packets = {"INST": {"COLLECT", "HEALTH_STATUS"}}
    return migration


@pytest.mark.parametrize(
    ("existing_ttl", "expected"),
    [
        ((30, "DAY"), "30d"),
        ((1, "WEEK"), "1w"),
        ((6, "MONTH"), "6M"),
        ((2, "YEAR"), "2y"),
        ((12, "HOUR"), "12h"),
        ((0, "HOUR"), None),  # QuestDB reports "no TTL" as value 0
        (None, None),  # table doesn't exist / TTL unreadable
        ((5, "FORTNIGHT"), None),  # unknown unit
    ],
)
def test_existing_retain_time(existing_ttl, expected):
    migration = build_migration()
    client = StubClient(ttl=existing_ttl)

    assert migration._existing_retain_time(client, "DEFAULT__TLM__INST__HEALTH_STATUS") == expected


def test_ensure_table_preserves_existing_ttl(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_tlm", lambda *a, **k: {"items": []})
    migration = build_migration()
    client = StubClient(ttl=(30, "DAY"))

    table_name = migration._ensure_table(client, "INST", "HEALTH_STATUS", "TLM")

    # create_table must receive the TTL already on the table, otherwise it alters it away
    assert client.created == [("INST", "HEALTH_STATUS", "TLM", "30d", "DEFAULT")]
    assert table_name == "DEFAULT__TLM__INST__HEALTH_STATUS"


def test_ensure_table_uses_get_cmd_for_commands(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_cmd", lambda *a, **k: {"items": [], "src": "cmd"})
    monkeypatch.setattr(
        tsdb_migration, "get_tlm", lambda *a, **k: pytest.fail("get_tlm used for CMD")
    )
    migration = build_migration()
    client = StubClient()

    table_name = migration._ensure_table(client, "INST", "COLLECT", "CMD")

    assert table_name == "DEFAULT__CMD__INST__COLLECT"
    assert client.created[0][2] == "CMD"


def test_ensure_table_caches_only_once(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_tlm", lambda *a, **k: {"items": []})
    migration = build_migration()
    client = StubClient()

    migration._ensure_table(client, "INST", "HEALTH_STATUS", "TLM")
    migration._ensure_table(client, "INST", "HEALTH_STATUS", "TLM")

    assert len(client.created) == 1


def test_ensure_table_does_not_cache_on_failure(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_tlm", lambda *a, **k: {"items": []})
    migration = build_migration()
    client = StubClient()
    client.create_error = RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        migration._ensure_table(client, "INST", "HEALTH_STATUS", "TLM")

    # A transient failure must not leave the table permanently untracked
    assert migration._loaded_schemas == set()
    client.create_error = None
    migration._ensure_table(client, "INST", "HEALTH_STATUS", "TLM")
    assert len(client.created) == 1


def test_handle_ingress_error_recreates_missing_table(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_tlm", lambda *a, **k: {"items": []})
    migration = build_migration()
    client = StubClient()
    migration._loaded_schemas.add("DEFAULT__TLM__INST__HEALTH_STATUS")

    recovered = migration._handle_ingress_error(
        client,
        Exception("table does not exist [table=DEFAULT__TLM__INST__HEALTH_STATUS]"),
        StubPacket(),
        "TLM",
    )

    assert recovered is True
    assert len(client.created) == 1
    assert client.replayed == 1
    assert client.flushed == 1


def test_handle_ingress_error_missing_table_recreate_fails(monkeypatch):
    import tsdb_migration

    monkeypatch.setattr(tsdb_migration, "get_tlm", lambda *a, **k: {"items": []})
    migration = build_migration()
    client = StubClient()
    client.create_error = RuntimeError("still gone")
    client.pending_rows = [("t", {}, 1)]

    recovered = migration._handle_ingress_error(
        client, Exception("table does not exist"), StubPacket(), "TLM"
    )

    assert recovered is False
    assert client.pending_rows == []  # dropped rows must be reported, not silently kept
    assert client.reconnects == 1


def test_handle_ingress_error_delegates_cast_errors():
    migration = build_migration()
    client = StubClient()
    error = Exception("cast error from protocol type: FLOAT to column type: LONG")

    assert migration._handle_ingress_error(client, error, StubPacket(), "TLM") is True
    assert client.handled == [error]


def test_handle_ingress_error_reports_unrecovered_drop():
    migration = build_migration()
    client = StubClient()
    client.handle_result = False

    assert migration._handle_ingress_error(client, Exception("boom"), StubPacket(), "TLM") is False


def test_client_for_shard_creates_one_client_per_shard(monkeypatch):
    import tsdb_migration

    built = []

    class FakeClient(StubClient):
        def __init__(self, logger=None, name=None, batch_size=None, db_shard=0):
            super().__init__()
            self.db_shard = db_shard
            self.batch_size = batch_size
            self.name = name
            built.append(self)

        def connect_query(self):
            pass

    monkeypatch.setattr(tsdb_migration, "QuestDBClient", FakeClient)
    migration = build_migration()

    first = migration._client_for_shard(0)
    again = migration._client_for_shard(0)
    second = migration._client_for_shard(1)

    assert first is again
    assert second is not first
    assert [c.db_shard for c in built] == [0, 1]
    # batch_size must reach the client or it silently caps at DEFAULT_BATCH_SIZE
    assert first.batch_size == 1000


class RecordingClient(StubClient):
    """StubClient that records write_row calls."""

    def __init__(self, write_error=None):
        super().__init__()
        self.rows = []
        self.write_error = write_error

    def process_json_data(self, json_hash, table_name):
        return dict(json_hash)

    def write_row(self, table_name, columns, timestamp_ns, rx_timestamp_ns=None):
        if self.write_error:
            raise self.write_error
        self.rows.append((table_name, columns, timestamp_ns, rx_timestamp_ns))
        return False


class FilePacket(StubPacket):
    def __init__(self, cmd_or_tlm="TLM", extra=None, **kwargs):
        super().__init__(cmd_or_tlm=cmd_or_tlm, **kwargs)
        self.json_hash = {"TEMP1": 1.0}
        self.time_nsec = 1_700_000_000_000_000_000
        self.received_time_nsec = 1_700_000_000_500_000_000
        self.extra = extra


def build_file_migration(monkeypatch, packets, client):
    """Build a migration wired up so _process_file can run against stub packets."""
    import tsdb_migration

    migration = build_migration()
    migration.cancel_thread = False
    migration.sleep_seconds = 0
    migration.errors_count = 0
    migration.questdb_clients = {0: client}
    migration._download_file = lambda path: b""
    migration._ensure_table = lambda c, t, p, cot: f"DEFAULT__{cot}__{t}__{p}"
    migration.bin_processor = type("BP", (), {"process_bytes": lambda self, data: iter(packets)})()
    migration.sleeper = type("S", (), {"sleep": lambda self, secs: False})()
    monkeypatch.setattr(
        tsdb_migration.Store, "db_shard_for_target", staticmethod(lambda t, scope=None: 0)
    )
    return migration


def test_process_file_writes_received_time(monkeypatch):
    client = RecordingClient()
    migration = build_file_migration(monkeypatch, [FilePacket()], client)

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__decom.bin"
    )

    assert (count, had_error, cancelled) == (1, False, False)
    _table, _columns, ts, rx = client.rows[0]
    # Without rx the RECEIVED_TIMESECONDS/RECEIVED_TIMEFORMATTED items are missing on read
    assert ts == 1_700_000_000_000_000_000
    assert rx == 1_700_000_000_500_000_000


def test_process_file_writes_cosmos_extra(monkeypatch):
    client = RecordingClient()
    packet = FilePacket(extra={"user": "jason"})
    migration = build_file_migration(monkeypatch, [packet], client)

    migration._process_file("DEFAULT/decom_logs/cmd/INST/x__decom.bin")

    assert client.rows[0][1]["COSMOS_EXTRA"] == '{"user": "jason"}'


def test_process_file_omits_cosmos_extra_when_absent(monkeypatch):
    client = RecordingClient()
    migration = build_file_migration(monkeypatch, [FilePacket()], client)

    migration._process_file("DEFAULT/decom_logs/tlm/INST/x__decom.bin")

    assert "COSMOS_EXTRA" not in client.rows[0][1]


def test_process_file_uses_packet_cmd_or_tlm_over_path(monkeypatch):
    """An __ALL__ file under tlm/ can still hold command packets."""
    client = RecordingClient()
    tables = []
    migration = build_file_migration(monkeypatch, [FilePacket(cmd_or_tlm="CMD")], client)
    migration._ensure_table = lambda c, t, p, cot: tables.append(cot) or f"DEFAULT__{cot}__{t}__{p}"

    migration._process_file("DEFAULT/decom_logs/tlm/INST/x__ALL__rt__decom.bin")

    assert tables == ["CMD"]


def test_process_file_reports_dropped_rows_as_error(monkeypatch):
    """A dropped row must mark the file failed so it lands in error/ and is retryable."""
    from questdb.ingress import IngressError

    client = RecordingClient(write_error=IngressError(1, "connection closed"))
    client.handle_result = False  # client could not recover, rows were dropped
    migration = build_file_migration(monkeypatch, [FilePacket()], client)

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__decom.bin"
    )

    assert (count, had_error, cancelled) == (0, True, False)
    assert migration.errors_count == 1


def test_process_file_counts_recovered_rows(monkeypatch):
    """A row the client recovers by casting is still ingested, so not an error."""
    from questdb.ingress import IngressError

    client = RecordingClient(write_error=IngressError(1, "cast error from protocol type"))
    client.handle_result = True
    migration = build_file_migration(monkeypatch, [FilePacket()], client)

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__decom.bin"
    )

    assert (count, had_error, cancelled) == (1, False, False)
    assert migration.errors_count == 0


def test_process_file_skips_obsolete_packet_without_failing_file(monkeypatch):
    """One deleted packet in an __ALL__ file must not send the whole file to error/."""
    client = RecordingClient()
    packets = [
        FilePacket(),
        FilePacket(packet_name="DELETED_PACKET"),
        FilePacket(),
    ]
    migration = build_file_migration(monkeypatch, packets, client)

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__ALL__rt__decom.bin"
    )

    assert (count, had_error, cancelled) == (2, False, False)
    assert migration.errors_count == 0


class FlushingClient(RecordingClient):
    """RecordingClient whose every write triggers an auto flush."""

    def write_row(self, table_name, columns, timestamp_ns, rx_timestamp_ns=None):
        super().write_row(table_name, columns, timestamp_ns, rx_timestamp_ns)
        return True


def test_process_file_reports_cancel_during_rate_limit_sleep(monkeypatch):
    """A shutdown mid file must not let run() move the partly read file to processed/."""
    client = FlushingClient()
    migration = build_file_migration(monkeypatch, [FilePacket(), FilePacket()], client)
    migration.sleeper = type("S", (), {"sleep": lambda self, secs: True})()

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__decom.bin"
    )

    assert (count, had_error, cancelled) == (1, False, True)


def test_process_file_reports_cancel_thread(monkeypatch):
    client = RecordingClient()
    migration = build_file_migration(monkeypatch, [FilePacket()], client)
    migration.cancel_thread = True

    count, had_error, cancelled = migration._process_file(
        "DEFAULT/decom_logs/tlm/INST/x__decom.bin"
    )

    assert (count, had_error, cancelled) == (0, False, True)


def test_is_valid_packet_separates_cmd_and_tlm():
    migration = build_migration()

    assert migration._is_valid_packet("INST", "HEALTH_STATUS", "TLM") is True
    assert migration._is_valid_packet("INST", "COLLECT", "TLM") is False
    assert migration._is_valid_packet("INST", "COLLECT", "CMD") is True
    assert migration._is_valid_packet("OTHER", "HEALTH_STATUS", "TLM") is False

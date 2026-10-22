import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from contextlib import redirect_stderr
from pathlib import Path
from typing import cast
from unittest import mock

from local_logging import (
    LocalJsonLogger,
    SafeJsonFormatter,
    _DestinationOwnership,
    _sanitize_text,
)


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.log_path = Path(self.tempdir.name) / "application.jsonl"
        self.loggers = []

    def tearDown(self):
        for logger in self.loggers:
            logger.close()

    def make_logger(self, **overrides):
        options = {
            "path": self.log_path,
            "component": "test_component",
            "run_id": "run-123",
            "known_secrets": ("xy", "long-secret-value"),
        }
        options.update(overrides)
        logger = LocalJsonLogger(**options)
        self.loggers.append(logger)
        return logger

    def artifacts(self):
        return sorted(self.log_path.parent.glob(self.log_path.name + "*"))

    def records(self):
        result = []
        for artifact in self.artifacts():
            raw = artifact.read_bytes()
            raw.decode("utf-8")
            for line in raw.splitlines():
                self.assertTrue(line, artifact)
                result.append(json.loads(line))
        return result

    def all_artifact_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.artifacts())

    def test_writes_one_complete_json_object_with_stable_required_fields(self):
        logger = self.make_logger()

        self.assertTrue(logger.event("startup_complete", severity="INFO", metadata={"status": "ready", "count": 2}))
        logger.close()
        self.loggers.remove(logger)

        records = self.records()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("startup_complete", record["event"])
        self.assertEqual("INFO", record["severity"])
        self.assertEqual("test_component", record["component"])
        self.assertEqual("run-123", record["run_id"])
        self.assertEqual("ready", record["status"])
        self.assertEqual(2, record["count"])
        self.assertRegex(record["timestamp"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_metadata_is_fixed_allow_list_of_scalars(self):
        logger = self.make_logger()
        logger.event(
            "metadata_test",
            metadata={
                "status": "ok",
                "count": 3,
                "duration_ms": 1.25,
                "success": True,
                "question": "private question",
                "headers": {"Authorization": "Bearer xy"},
                "filename": "secret.txt",
                "remote_id": "remote-77",
                "nested": {"secret": "long-secret-value"},
                "items": ["long-secret-value"],
                "object": object(),
            },
        )
        logger.close()
        self.loggers.remove(logger)

        record = self.records()[0]
        self.assertEqual({"status", "count", "duration_ms", "success"}, set(record) - {"timestamp", "severity", "event", "component", "run_id"})
        self.assertNotIn("private question", self.all_artifact_text())
        self.assertNotIn("secret.txt", self.all_artifact_text())
        self.assertNotIn("remote-77", self.all_artifact_text())

    def test_invalid_event_component_and_run_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make_logger(component="../../private")
        with self.assertRaises(ValueError):
            self.make_logger(run_id="token=xy")
        logger = self.make_logger()
        with self.assertRaises(ValueError):
            logger.event("question: raw user text")

    def test_identifier_string_subclasses_are_rejected(self):
        class CustomString(str):
            pass

        with self.assertRaises(ValueError):
            self.make_logger(component=CustomString("component"))
        with self.assertRaises(ValueError):
            self.make_logger(run_id=CustomString("run-id"))
        with self.assertRaises(ValueError):
            self.make_logger(run_id=CustomString(""))
        logger = self.make_logger()
        with self.assertRaises(ValueError):
            logger.event(CustomString("event_name"))

    def test_allowed_string_metadata_cannot_carry_prohibited_content(self):
        logger = self.make_logger()
        prohibited = {
            "status": "What private question did the user ask?",
            "operation": "AI answer and source excerpt",
            "outcome": "C:/private/imported-content.txt",
            "method": "postgresql://user:password@host/db",
        }
        logger.event("content_boundary", metadata=prohibited)
        logger.close()
        self.loggers.remove(logger)

        record = self.records()[0]
        self.assertEqual(
            {"timestamp", "severity", "event", "component", "run_id"},
            set(record),
        )
        text = self.all_artifact_text()
        for value in prohibited.values():
            self.assertNotIn(value, text)


    def test_exception_logs_only_class_and_explicit_safe_code(self):
        logger = self.make_logger()
        secret_message = "failure token=long-secret-value and short xy"

        try:
            raise RuntimeError(secret_message)
        except RuntimeError as exc:
            logger.event("operation_failed", severity="ERROR", exception=exc, error_code="E_STORAGE")
        logger.close()
        self.loggers.remove(logger)

        record = self.records()[0]
        self.assertEqual("RuntimeError", record["exception_class"])
        self.assertEqual("E_STORAGE", record["error_code"])
        text = self.all_artifact_text()
        self.assertNotIn(secret_message, text)
        self.assertNotIn("long-secret-value", text)
        self.assertNotRegex(text, r"\bxy\b")
        self.assertNotIn("traceback", text.lower())

    def test_exception_class_lookup_bypasses_hostile_metaclass_name_hook(self):
        class HostileExceptionMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise AssertionError("metaclass name hook was invoked")
                return super().__getattribute__(name)

        class HostileException(Exception, metaclass=HostileExceptionMeta):
            pass

        logger = self.make_logger()
        self.assertTrue(
            logger.event(
                "operation_failed",
                severity="ERROR",
                exception=HostileException("private"),
                error_code="E_HOSTILE_CLASS",
            )
        )
        logger.close()
        self.loggers.remove(logger)

        self.assertEqual("HostileException", self.records()[0]["exception_class"])

    def test_exception_class_string_subclasses_are_sanitized_at_both_boundaries(self):
        class CustomString(str):
            pass

        logger = self.make_logger()
        logger.event(
            "operation_failed",
            exception_class=CustomString("ForgedError"),
            error_code="E_STABLE",
        )
        logger.close()
        self.loggers.remove(logger)

        self.assertEqual("Exception", self.records()[0]["exception_class"])
        self.assertNotIn("ForgedError", self.all_artifact_text())

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.ERROR, __file__, 1, "ignored", (), None)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
            "exception_class": CustomString("ForgedError"),
            "error_code": "E_STABLE",
        }
        rendered = formatter.format(record)

        self.assertEqual("Exception", json.loads(rendered)["exception_class"])
        self.assertNotIn("ForgedError", rendered)

    def test_error_code_string_subclasses_are_rejected_at_both_boundaries(self):
        class CustomString(str):
            pass

        logger = self.make_logger()
        error_code = CustomString("E_FORGED")
        with self.assertRaises(ValueError):
            logger.event("operation_failed", exception=RuntimeError("private"), error_code=error_code)
        with self.assertRaises(ValueError):
            logger.event("operation_failed", exception_class="RuntimeError", error_code=error_code)
        with self.assertRaises(ValueError):
            logger.event("operation_failed", error_code=error_code)

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.ERROR, __file__, 1, "ignored", (), None)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
            "error_code": error_code,
        }
        rendered = formatter.format(record)

        self.assertNotIn("error_code", json.loads(rendered))
        self.assertNotIn("E_FORGED", rendered)

    def test_formatter_ignores_non_exact_metadata_containers_without_hooks(self):
        class HostileDict(dict):
            def get(self, *args, **kwargs):
                raise AssertionError("dict subclass get hook was invoked")

            def items(self):
                raise AssertionError("dict subclass items hook was invoked")

        class HostileMapping(Mapping):
            def __getitem__(self, key):
                raise AssertionError("mapping getitem hook was invoked")

            def __iter__(self):
                raise AssertionError("mapping iter hook was invoked")

            def __len__(self):
                raise AssertionError("mapping len hook was invoked")

            def get(self, *args, **kwargs):
                raise AssertionError("mapping get hook was invoked")

            def items(self):
                raise AssertionError("mapping items hook was invoked")

        formatter = SafeJsonFormatter()
        for payload in (HostileDict(event="forged"), HostileMapping()):
            with self.subTest(payload_type=type(payload).__name__):
                record = logging.LogRecord(
                    "external", logging.INFO, __file__, 1, "ignored", (), None
                )
                record.safe_payload = payload

                rendered = formatter.format(record)

                self.assertEqual("logging_record", json.loads(rendered)["event"])
                self.assertNotIn("forged", rendered)

    def test_formatter_ignores_hostile_non_exact_metadata_keys(self):
        class HostileKey(str):
            armed = False

            def __hash__(self):
                return hash("event")

            def __eq__(self, other):
                if self.armed:
                    raise AssertionError("key equality hook was invoked")
                return super().__eq__(other)

        key = HostileKey("forged")
        payload = {key: "private"}
        key.armed = True
        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.INFO, __file__, 1, "ignored", (), None)
        record.safe_payload = payload

        rendered = formatter.format(record)

        self.assertEqual("logging_record", json.loads(rendered)["event"])
        self.assertNotIn("private", rendered)

    def test_formatter_rejects_hostile_scalar_subclasses_without_hooks(self):
        class HostileString(str):
            def __str__(self):
                raise AssertionError("string hook was invoked")

        class HostileInteger(int):
            def __ge__(self, other):
                raise AssertionError("integer comparison hook was invoked")

        class HostileFloat(float):
            def __float__(self):
                raise AssertionError("float hook was invoked")

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.INFO, __file__, 1, "ignored", (), None)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
            "status": HostileString("ok"),
            "count": HostileInteger(1),
            "duration_ms": HostileFloat(1.5),
        }

        rendered = formatter.format(record)

        parsed = json.loads(rendered)
        self.assertNotIn("status", parsed)
        self.assertNotIn("count", parsed)
        self.assertNotIn("duration_ms", parsed)

    def test_final_sanitizer_redacts_credential_shapes_and_known_values(self):
        formatter = SafeJsonFormatter(known_secrets=("xy", "long-secret-value"))
        attacks = [
            "Bearer abc.DEF-123_~+/=",
            "password = swordfish",
            "TOKEN: long-secret-value",
            "api_key=xy",
            "Authorization: Basic dXNlcjpwYXNz",
            "Cookie: session=abcdef",
            "https://alice:secret@example.test/path",
            "https://example.test/path?token=abcdef&safe=value",
            "DATABASE_URL=postgresql://alice:secret@example.test/db",
            "prefix-long-secret-value-suffix",
            "short xy value",
        ]

        rendered = "\n".join(_sanitize_text(attack, formatter._known_secrets) for attack in attacks)
        self.assertNotIn("long-secret-value", rendered)
        self.assertNotRegex(rendered, r"\bxy\b")
        self.assertNotIn("swordfish", rendered)
        self.assertNotIn("abc.DEF-123_~+/=", rendered)
        self.assertNotIn("alice:secret@", rendered)
        self.assertNotIn("token=abcdef", rendered)

        record = logging.LogRecord("external", logging.INFO, __file__, 1, "ignored", (), None)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
            "status": "prefix-long-secret-value-suffix",
        }
        boundary = formatter.format(record)
        self.assertNotIn("long-secret-value", boundary)
        self.assertIn("[REDACTED]", boundary)

    def test_formatter_ignores_non_exact_known_secrets_without_stringifying_them(self):
        class ExplosiveString(str):
            def __str__(self):
                raise RuntimeError("string subclass hook was invoked")

        class ExplosiveObject:
            def __str__(self):
                raise RuntimeError("object string hook was invoked")

        formatter = SafeJsonFormatter(
            known_secrets=cast(
                tuple[str, ...],
                (
                    "valid-secret",
                    ExplosiveString("subclass-secret"),
                    ExplosiveObject(),
                    "",
                    "other-valid-secret",
                ),
            )
        )

        self.assertEqual(
            ("other-valid-secret", "valid-secret"),
            formatter._known_secrets,
        )

    def test_pattern_redaction_precedes_overlapping_known_secret_replacement(self):
        bearer = _sanitize_text("Bearer credential123", ("Bearer",))
        assignment = _sanitize_text("token=credential123", ("token",))

        self.assertNotIn("credential123", bearer)
        self.assertNotIn("credential123", assignment)

    def test_sanitizer_redacts_full_header_values_and_queries_for_all_path_forms(self):
        attacks = [
            "Cookie: session=abc; refresh=def; theme=dark",
            'Authorization: Digest username="alice", realm="private", response="secret"',
            "ftp://example.test/path?q=ftp-secret&x=1",
            "custom+scheme://example.test/path?q=scheme-secret",
            "/relative/path?q=relative-secret&x=1",
            "bare/path?q=bare-secret&x=1",
            "example.test/path?q=url-secret&x=1",
        ]

        rendered = "\n".join(_sanitize_text(attack, ()) for attack in attacks)
        for secret in (
            "abc", "def", "dark", "alice", "private", "secret", "ftp-secret",
            "scheme-secret", "relative-secret", "bare-secret", "url-secret",
        ):
            self.assertNotIn(secret, rendered)

    def test_sanitizer_redacts_header_assignments_to_end_of_line(self):
        attacks = {
            "Cookie=session=abc; refresh=def": "Cookie=[REDACTED]",
            "Set-Cookie=id=abc; Path=/, refresh=def": "Set-Cookie=[REDACTED]",
            "Authorization=Basic abc, realm=private": "Authorization=[REDACTED]",
            "Proxy-Authorization=Digest abc; realm=private": "Proxy-Authorization=[REDACTED]",
        }

        for attack, expected in attacks.items():
            with self.subTest(attack=attack):
                self.assertEqual(expected, _sanitize_text(attack, ()))

        self.assertEqual(
            "Cookie=[REDACTED]\nstatus=ok",
            _sanitize_text("Cookie=session=abc; refresh=def\nstatus=ok", ()),
        )
        self.assertEqual(
            "password=[REDACTED], status=ok",
            _sanitize_text("password=swordfish, status=ok", ()),
        )
        self.assertEqual(
            "token=[REDACTED]; outcome=ready",
            _sanitize_text("token=secret; outcome=ready", ()),
        )

    def test_sanitizer_redacts_standalone_query_strings(self):
        self.assertEqual("?[REDACTED]", _sanitize_text("?q=private", ()))
        self.assertEqual("?[REDACTED]", _sanitize_text("?x=one&y=two", ()))

    def test_reopening_with_smaller_retention_prunes_old_numbered_backups(self):
        first = self.make_logger(max_bytes=240, backup_count=5)
        for index in range(100):
            self.assertTrue(
                first.event(
                    "retention_probe",
                    metadata={"count": index, "status": "x" * 30},
                )
            )
        first.close()
        self.loggers.remove(first)
        self.assertGreater(len(self.artifacts()), 2)

        second = self.make_logger(max_bytes=240, backup_count=1)

        numbered = [
            artifact
            for artifact in self.artifacts()
            if artifact.name.removeprefix(self.log_path.name + ".").isdigit()
        ]
        self.assertLessEqual(len(numbered), 1)
        self.assertTrue(second.event("retention_reopened"))

    def test_startup_prunes_only_out_of_range_numbered_backup_files(self):
        self.log_path.write_text("current\n", encoding="utf-8")
        for number in (1, 2, 6, 999):
            Path(f"{self.log_path}.{number}").write_text(
                f"backup-{number}\n", encoding="utf-8"
            )
        unrelated = (
            Path(f"{self.log_path}.bogus"),
            Path(f"{self.log_path}.2.txt"),
            self.log_path.with_name(self.log_path.name + "-other.9"),
        )
        for path in unrelated:
            path.write_text("unrelated\n", encoding="utf-8")

        self.make_logger(backup_count=1)

        self.assertTrue(Path(f"{self.log_path}.1").exists())
        for number in (2, 6, 999):
            self.assertFalse(Path(f"{self.log_path}.{number}").exists())
        self.assertTrue(all(path.read_text(encoding="utf-8") == "unrelated\n" for path in unrelated))
        self.assertEqual("current\n", self.log_path.read_text(encoding="utf-8"))

    def test_retention_pruning_does_not_delete_hard_link_aliases(self):
        unrelated = self.log_path.with_name("unrelated-data")
        unrelated.write_text("preserve\n", encoding="utf-8")
        unsafe_backup = Path(f"{self.log_path}.9")
        try:
            os.link(unrelated, unsafe_backup)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"hard links unsupported: {exc}")

        self.make_logger(backup_count=1)

        self.assertEqual("preserve\n", unrelated.read_text(encoding="utf-8"))
        self.assertEqual("preserve\n", unsafe_backup.read_text(encoding="utf-8"))

    def test_retention_pruning_failure_is_generic_and_releases_ownership(self):
        self.log_path.write_text("current\n", encoding="utf-8")
        stale = Path(f"{self.log_path}.2")
        stale.write_text("stale\n", encoding="utf-8")

        with mock.patch("local_logging.os.unlink", side_effect=PermissionError):
            with self.assertRaisesRegex(RuntimeError, "retention failed") as raised:
                LocalJsonLogger(
                    self.log_path,
                    "test_component",
                    run_id="failed-retention",
                    backup_count=1,
                )

        self.assertNotIn(str(self.log_path), str(raised.exception))
        self.assertEqual("current\n", self.log_path.read_text(encoding="utf-8"))
        stale.unlink()
        logger = self.make_logger(backup_count=1)
        self.assertTrue(logger.event("post_retention_failure"))

    def test_rotation_keeps_json_records_complete_and_at_most_five_backups(self):
        logger = self.make_logger(max_bytes=300, backup_count=5)
        for index in range(250):
            self.assertTrue(logger.event("rotation_probe", metadata={"count": index, "status": "x" * 30}))
        logger.close()
        self.loggers.remove(logger)

        artifacts = self.artifacts()
        self.assertLessEqual(len(artifacts) - 1, 5)
        self.assertGreater(len(artifacts), 1)
        records = self.records()
        self.assertTrue(records)
        self.assertTrue(all(record["event"] == "rotation_probe" for record in records))

    def test_secret_scanning_across_every_rotated_artifact(self):
        logger = self.make_logger(
            max_bytes=260,
            backup_count=5,
            known_secrets=("artifact-secret", "xy"),
        )
        injections = [
            {"status": "prefix-artifact-secret-suffix"},
            {"operation": "op.xy.value"},
        ]
        for index in range(120):
            logger.event("secret_probe", metadata=injections[index % len(injections)])
        logger.close()
        self.loggers.remove(logger)

        text = self.all_artifact_text()
        for forbidden in ("artifact-secret", "op.xy.value"):
            self.assertNotIn(forbidden, text)
        self.assertIn("[REDACTED]", text)
        self.records()

    def test_only_explicit_standard_severities_are_accepted_at_both_boundaries(self):
        malicious_level = 35
        malicious_name = "TOKEN=CREDENTIAL123"
        original_level_names = dict(logging._levelToName)
        original_name_levels = dict(logging._nameToLevel)

        def restore_level_names():
            logging._levelToName.clear()
            logging._levelToName.update(original_level_names)
            logging._nameToLevel.clear()
            logging._nameToLevel.update(original_name_levels)

        logging.addLevelName(malicious_level, malicious_name)
        self.addCleanup(restore_level_names)
        logger = self.make_logger()

        with self.assertRaises(ValueError):
            logger.event("custom_level", severity=malicious_name)

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", malicious_level, __file__, 1, "ignored", (), None)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
        }
        rendered = formatter.format(record)
        self.assertNotIn("CREDENTIAL123", rendered.upper())
        self.assertNotIn("TOKEN=", rendered.upper())
        self.assertEqual("UNKNOWN", json.loads(rendered)["severity"])

    def test_formatter_rejects_levelname_subclasses_without_invoking_them(self):
        class ExplosiveLevelName(str):
            def upper(self):
                raise RuntimeError("levelname subclass hook was invoked")

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.INFO, __file__, 1, "ignored", (), None)
        record.levelname = ExplosiveLevelName("INFO")
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
        }

        rendered = formatter.format(record)

        self.assertEqual("UNKNOWN", json.loads(rendered)["severity"])

    def test_formatter_rejects_levelno_subclasses_without_invoking_them(self):
        class ExplosiveLevelNumber(int):
            def __eq__(self, other):
                raise AssertionError("level number equality hook was invoked")

            def __index__(self):
                raise AssertionError("level number index hook was invoked")

        formatter = SafeJsonFormatter()
        record = logging.LogRecord("external", logging.INFO, __file__, 1, "ignored", (), None)
        record.levelno = ExplosiveLevelNumber(logging.INFO)
        record.safe_payload = {
            "event": "external_record",
            "component": "test_component",
            "run_id": "run-123",
        }

        rendered = formatter.format(record)

        self.assertEqual("UNKNOWN", json.loads(rendered)["severity"])

    def test_severity_rejects_stateful_string_subclasses_without_invoking_them(self):
        class StatefulSeverity(str):
            def __new__(cls):
                instance = super().__new__(cls, "INFO")
                instance.upper_calls = 0
                return instance

            def upper(self):
                self.upper_calls += 1
                if self.upper_calls > 1:
                    raise RuntimeError("stateful severity was invoked twice")
                return "INFO"

        severity = StatefulSeverity()
        logger = self.make_logger()

        with self.assertRaises(ValueError):
            logger.event("custom_severity", severity=severity)

        self.assertEqual(0, severity.upper_calls)

    def test_rotation_configuration_rejects_bool_values(self):
        with self.assertRaises(ValueError):
            self.make_logger(max_bytes=True)
        with self.assertRaises(ValueError):
            self.make_logger(backup_count=False)

    def test_rotation_requires_at_least_one_backup(self):
        with self.assertRaises(ValueError):
            self.make_logger(max_bytes=100, backup_count=0)

    def test_backup_count_rejects_integer_subclasses(self):
        class CustomInteger(int):
            pass

        with self.assertRaises(ValueError):
            self.make_logger(backup_count=CustomInteger(1))

    def test_max_bytes_rejects_integer_subclasses_without_invoking_them(self):
        class ExplosiveInteger(int):
            def __le__(self, other):
                raise RuntimeError("integer subclass hook was invoked")

        with self.assertRaises(ValueError):
            self.make_logger(max_bytes=ExplosiveInteger(100))

    def test_public_metadata_requires_exact_dict_without_invoking_mapping_hooks(self):
        class HostileDict(dict):
            def items(self):
                raise AssertionError("dict subclass items hook was invoked")

        class HostileMapping(Mapping):
            def __getitem__(self, key):
                raise AssertionError("mapping getitem hook was invoked")

            def __iter__(self):
                raise AssertionError("mapping iter hook was invoked")

            def __len__(self):
                raise AssertionError("mapping len hook was invoked")

            def items(self):
                raise AssertionError("mapping items hook was invoked")

        logger = self.make_logger()

        with self.assertRaises(TypeError):
            logger.event("hostile_dict", metadata=HostileDict(status="ok"))
        with self.assertRaises(TypeError):
            logger.event("hostile_mapping", metadata=cast(dict[str, object], HostileMapping()))

    def test_public_metadata_ignores_hostile_non_exact_keys_without_hooks(self):
        class HostileKey(str):
            armed = False

            def __hash__(self):
                if self.armed:
                    raise AssertionError("key hash hook was invoked")
                return super().__hash__()

        key = HostileKey("status")
        metadata = {key: "ok"}
        key.armed = True
        logger = self.make_logger()

        self.assertTrue(
            logger.event("hostile_key", metadata=cast(dict[str, object], metadata))
        )
        logger.close()
        self.loggers.remove(logger)

        self.assertNotIn("status", self.records()[0])

    def test_metadata_types_are_enforced_per_key(self):
        logger = self.make_logger()
        logger.event(
            "typed_metadata",
            metadata={
                "attempt": True,
                "count": -1,
                "item_count": 1.5,
                "duration_ms": float("inf"),
                "success": 1,
                "method": 7,
                "operation": "not stable",
                "outcome": None,
                "status": False,
            },
        )
        logger.event("negative_duration", metadata={"duration_ms": -0.1})
        logger.event("boolean_duration", metadata={"duration_ms": True})
        logger.event(
            "typed_metadata_valid",
            metadata={
                "attempt": 0,
                "count": 2,
                "item_count": 3,
                "duration_ms": 1.5,
                "success": False,
                "method": "GET",
                "operation": "sync.run",
                "outcome": "completed",
                "status": "ok",
            },
        )
        logger.close()
        self.loggers.remove(logger)

        *invalid_records, valid = self.records()
        metadata_keys = {
            "attempt", "count", "item_count", "duration_ms", "success",
            "method", "operation", "outcome", "status",
        }
        self.assertTrue(all(metadata_keys.isdisjoint(record) for record in invalid_records))
        self.assertEqual(metadata_keys, metadata_keys.intersection(valid))

    def test_metadata_scalar_subclasses_are_rejected(self):
        class CustomString(str):
            pass

        class CustomInteger(int):
            pass

        class CustomFloat(float):
            pass

        logger = self.make_logger()
        logger.event(
            "subclass_metadata",
            metadata={
                "status": CustomString("ok"),
                "count": CustomInteger(1),
                "duration_ms": CustomFloat(1.5),
            },
        )
        logger.close()
        self.loggers.remove(logger)

        record = self.records()[0]
        self.assertNotIn("status", record)
        self.assertNotIn("count", record)
        self.assertNotIn("duration_ms", record)

    def test_concurrent_threads_produce_parseable_complete_records(self):
        logger = self.make_logger(max_bytes=20_000, backup_count=5)
        thread_count = 8
        per_thread = 40

        def worker(worker_id):
            for item in range(per_thread):
                logger.event("worker_progress", metadata={"count": worker_id * per_thread + item})

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(thread_count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        logger.close()
        self.loggers.remove(logger)

        records = self.records()
        self.assertEqual(thread_count * per_thread, len(records))
        self.assertEqual(set(range(thread_count * per_thread)), {record["count"] for record in records})

    def test_unwritable_destination_returns_false_and_callback_is_sanitized(self):
        failures = []
        blocker = Path(self.tempdir.name) / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        bad_path = blocker / "private-long-secret-value" / "application.jsonl"
        logger = self.make_logger(path=bad_path, failure_callback=failures.append)

        result = logger.event("write_probe", metadata={"status": "Bearer raw-destination-secret"})

        self.assertFalse(result)
        self.assertEqual(1, len(failures))
        self.assertEqual({"event": "logging_destination_unavailable", "component": "test_component"}, failures[0])
        self.assertNotIn("raw-destination-secret", repr(failures))
        self.assertNotIn("long-secret-value", repr(failures))

    def test_destination_and_base_exception_callback_never_escape_or_write_stderr(self):
        blocker = Path(self.tempdir.name) / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        bad_path = blocker / "private-long-secret-value" / "application.jsonl"

        def raising_callback(_failure):
            raise KeyboardInterrupt("callback leaked raw-destination-secret")

        logger = self.make_logger(path=bad_path, failure_callback=raising_callback)
        stderr = io.StringIO()
        original_raise_exceptions = logging.raiseExceptions
        try:
            logging.raiseExceptions = True
            with redirect_stderr(stderr):
                result = logger.event(
                    "write_probe",
                    metadata={"status": "prefix-long-secret-value-suffix"},
                )
        finally:
            logging.raiseExceptions = original_raise_exceptions

        self.assertFalse(result)
        self.assertEqual("", stderr.getvalue())

    def test_failure_callback_can_reenter_same_logger_without_recursing(self):
        blocker = Path(self.tempdir.name) / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        bad_path = blocker / "application.jsonl"
        failures = []
        nested_results = []
        logger = None

        def callback(failure):
            failures.append(failure)
            assert logger is not None
            nested_results.append(logger.event("callback_reentry"))

        logger = self.make_logger(path=bad_path, failure_callback=callback)
        stderr = io.StringIO()
        original_raise_exceptions = logging.raiseExceptions
        try:
            logging.raiseExceptions = True
            with redirect_stderr(stderr):
                result = logger.event("write_probe")
        finally:
            logging.raiseExceptions = original_raise_exceptions

        self.assertFalse(result)
        self.assertEqual([False], nested_results)
        self.assertEqual(1, len(failures))
        self.assertEqual("", stderr.getvalue())

    def test_failure_callback_runs_after_logger_and_handler_locks_are_released(self):
        blocker = Path(self.tempdir.name) / "not-a-directory"
        blocker.write_text("occupied", encoding="utf-8")
        bad_path = blocker / "application.jsonl"
        callbacks = []
        nested_done = threading.Event()
        completed_before_callback_returned = []
        logger = None

        def callback(failure):
            callbacks.append(failure)
            if len(callbacks) != 1:
                return

            def nested_write():
                assert logger is not None
                logger.event("concurrent_callback_reentry")
                nested_done.set()

            thread = threading.Thread(target=nested_write)
            thread.start()
            completed_before_callback_returned.append(nested_done.wait(timeout=2))
            thread.join(timeout=2)

        logger = self.make_logger(path=bad_path, failure_callback=callback)
        self.assertFalse(logger.event("write_probe"))
        self.assertTrue(nested_done.wait(timeout=2))
        self.assertEqual([True], completed_before_callback_returned)
        self.assertEqual(2, len(callbacks))

    def test_defaults_are_five_mib_and_five_backups_with_delayed_open(self):
        logger = self.make_logger()
        self.assertEqual(5 * 1024 * 1024, logger.max_bytes)
        self.assertEqual(5, logger.backup_count)
        self.assertFalse(self.log_path.exists())

    def test_second_same_process_writer_for_canonical_path_is_rejected(self):
        logger = self.make_logger()
        alias = self.log_path.parent / "." / self.log_path.name

        with self.assertRaisesRegex(RuntimeError, "active writer") as raised:
            LocalJsonLogger(alias, "other_component", run_id="other-run")

        self.assertNotIn(str(self.log_path), str(raised.exception))
        self.assertNotIn("application.jsonl", str(raised.exception))
        self.assertFalse(self.log_path.exists())

    def test_hard_link_created_after_first_write_cannot_bypass_destination_ownership(self):
        first = self.make_logger()
        self.assertTrue(first.event("first_write"))
        alias = self.log_path.with_name("application-alias.jsonl")
        try:
            os.link(self.log_path, alias)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"hard links unsupported: {exc}")

        with self.assertRaisesRegex(RuntimeError, "active writer"):
            LocalJsonLogger(alias, "other_component", run_id="other-run")

    def test_hard_link_created_after_first_write_is_rejected_in_child_process(self):
        first = self.make_logger()
        self.assertTrue(first.event("first_write"))
        alias = self.log_path.with_name("application-alias.jsonl")
        try:
            os.link(self.log_path, alias)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"hard links unsupported: {exc}")
        child_code = (
            "import sys\n"
            "from local_logging import LocalJsonLogger\n"
            "try:\n"
            "    LocalJsonLogger(sys.argv[1], 'child', run_id='child-run')\n"
            "except RuntimeError:\n"
            "    print('REJECTED')\n"
        )

        child = subprocess.run(
            [sys.executable, "-c", child_code, str(alias)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(0, child.returncode)
        self.assertEqual("REJECTED", child.stdout.strip())
        self.assertEqual("", child.stderr)

    def test_replaced_destination_keeps_original_textual_path_owned(self):
        first = self.make_logger()
        self.assertTrue(first.event("first_write"))
        try:
            self.log_path.unlink()
        except PermissionError as exc:
            self.skipTest(f"open-file replacement unsupported: {exc}")
        self.log_path.write_text("replacement\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "active writer"):
            LocalJsonLogger(self.log_path, "other_component", run_id="other-run")

        self.assertEqual("replacement\n", self.log_path.read_text(encoding="utf-8"))

    def test_replaced_destination_is_not_rotated_by_original_writer(self):
        if os.name == "nt":
            self.skipTest("open-file replacement unsupported on Windows")
        first = self.make_logger(max_bytes=200, backup_count=1)
        self.assertTrue(first.event("first_write"))
        self.log_path.unlink()
        self.log_path.write_text("replacement\n", encoding="utf-8")

        self.assertFalse(first.event("must_not_rotate_replacement"))
        self.assertEqual("replacement\n", self.log_path.read_text(encoding="utf-8"))
        self.assertFalse(Path(f"{self.log_path}.1").exists())

    def test_hard_link_alias_cannot_bypass_destination_ownership(self):
        self.log_path.write_text("", encoding="utf-8")
        alias = self.log_path.with_name("application-alias.jsonl")
        try:
            os.link(self.log_path, alias)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"hard links unsupported: {exc}")
        self.make_logger()

        with self.assertRaisesRegex(RuntimeError, "active writer"):
            LocalJsonLogger(alias, "other_component", run_id="other-run")

    @unittest.skipUnless(os.name == "nt", "Windows path equivalence only")
    def test_windows_trailing_dot_alias_cannot_bypass_destination_ownership(self):
        self.make_logger()

        with self.assertRaisesRegex(RuntimeError, "active writer"):
            LocalJsonLogger(f"{self.log_path}.", "other_component", run_id="other-run")

    @unittest.skipUnless(os.name == "nt", "Windows path equivalence only")
    def test_windows_trailing_space_alias_cannot_bypass_destination_ownership(self):
        self.make_logger()

        with self.assertRaisesRegex(RuntimeError, "active writer"):
            LocalJsonLogger(f"{self.log_path} ", "other_component", run_id="other-run")

    def test_symlink_retargeting_does_not_change_owned_write_destination(self):
        if os.name == "nt":
            self.skipTest("POSIX symlink behavior only")
        target_a = Path(self.tempdir.name) / "target-a.jsonl"
        target_b = Path(self.tempdir.name) / "target-b.jsonl"
        link = Path(self.tempdir.name) / "current.jsonl"
        try:
            link.symlink_to(target_a)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"symlinks unsupported: {exc}")

        through_link = self.make_logger(path=link, component="link_component")
        link.unlink()
        link.symlink_to(target_b)
        direct_b = self.make_logger(path=target_b, component="target_b_component")

        self.assertTrue(through_link.event("through_link"))
        self.assertTrue(direct_b.event("direct_target_b"))
        through_link.close()
        direct_b.close()
        self.loggers.remove(through_link)
        self.loggers.remove(direct_b)

        records_a = [json.loads(line) for line in target_a.read_text(encoding="utf-8").splitlines()]
        records_b = [json.loads(line) for line in target_b.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(["through_link"], [record["event"] for record in records_a])
        self.assertEqual(["direct_target_b"], [record["event"] for record in records_b])

    def test_destination_ownership_can_be_reacquired_after_close(self):
        first = self.make_logger()
        first.close()
        self.loggers.remove(first)

        second = self.make_logger()
        self.assertTrue(second.event("reacquired"))

    def test_destination_ownership_is_released_after_constructor_failure(self):
        def broken_secrets():
            raise RuntimeError("secret setup failed")
            yield "unreachable"

        with self.assertRaisesRegex(RuntimeError, "secret setup failed"):
            LocalJsonLogger(
                self.log_path,
                "test_component",
                run_id="failed-run",
                known_secrets=broken_secrets(),
            )

        logger = self.make_logger()
        self.assertTrue(logger.event("post_failure_reacquired"))

    def test_constructor_cleanup_base_exception_does_not_mask_failure_or_ownership_release(self):
        def broken_secrets():
            raise RuntimeError("secret setup failed")
            yield "unreachable"

        with mock.patch(
            "local_logging._SafeRotatingFileHandler.close",
            side_effect=KeyboardInterrupt("close interrupted"),
        ):
            with self.assertRaisesRegex(RuntimeError, "secret setup failed"):
                LocalJsonLogger(
                    self.log_path,
                    "test_component",
                    run_id="failed-run",
                    known_secrets=broken_secrets(),
                )

        logger = self.make_logger()
        self.assertTrue(logger.event("post_cleanup_interrupt_reacquired"))

    def test_destination_ownership_is_released_after_base_exception(self):
        with mock.patch.object(
            _DestinationOwnership,
            "_acquire_os_lock",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt):
                LocalJsonLogger(
                    self.log_path,
                    "test_component",
                    run_id="interrupted-run",
                )

        logger = self.make_logger()
        self.assertTrue(logger.event("post_interrupt_reacquired"))

    def test_destination_ownership_ignores_child_temp_environment(self):
        parent = self.make_logger()
        self.assertTrue(parent.event("parent_active"))
        child_temp = Path(self.tempdir.name) / "child-temp"
        child_temp.mkdir()
        child_code = (
            "import sys\n"
            "from local_logging import LocalJsonLogger\n"
            "try:\n"
            "    logger = LocalJsonLogger(sys.argv[1], 'child', run_id='child-run')\n"
            "except RuntimeError:\n"
            "    print('REJECTED')\n"
            "else:\n"
            "    print('ACQUIRED')\n"
            "    logger.close()\n"
        )
        environment = os.environ.copy()
        for name in ("TMP", "TEMP", "TMPDIR"):
            environment[name] = str(child_temp)

        child = subprocess.run(
            [sys.executable, "-c", child_code, str(self.log_path)],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

        self.assertEqual(0, child.returncode)
        self.assertEqual("REJECTED", child.stdout.strip())
        self.assertEqual("", child.stderr)

    def test_destination_ownership_is_enforced_across_processes(self):
        ready_path = Path(self.tempdir.name) / "child-ready"
        child_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from local_logging import LocalJsonLogger\n"
            "logger = LocalJsonLogger(sys.argv[1], 'child', run_id='child-run')\n"
            "Path(sys.argv[2]).write_text('READY', encoding='utf-8')\n"
            "sys.stdin.read()\n"
            "logger.close()\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", child_code, str(self.log_path), str(ready_path)],
            cwd=Path(__file__).resolve().parents[1],
            stdin=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(lambda: child.poll() is None and child.kill())
        deadline = time.monotonic() + 5
        while not ready_path.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(ready_path.exists(), "child did not become ready before timeout")

        with self.assertRaisesRegex(RuntimeError, "active writer") as raised:
            LocalJsonLogger(self.log_path, "parent", run_id="parent-run")
        self.assertNotIn(str(self.log_path), str(raised.exception))

        _, stderr = child.communicate("", timeout=5)
        self.assertEqual("", stderr)
        self.assertEqual(0, child.returncode)
        logger = self.make_logger()
        self.assertTrue(logger.event("post_process_reacquired"))


if __name__ == "__main__":
    unittest.main()

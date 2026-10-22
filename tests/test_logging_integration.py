import io
import json
import queue
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import app
import cli
import local_logging
from diagnostics import DiagnosticResult


class OperationalLoggerFactoryTests(unittest.TestCase):
    def test_public_warning_helper_emits_exact_generic_warning_and_swallows_callback_failure(self):
        warnings = []
        local_logging.warn_operational_logging_failure(warnings.append)
        self.assertEqual([local_logging.OPERATIONAL_LOGGING_WARNING], warnings)

        def broken_callback(message):
            self.assertEqual(local_logging.OPERATIONAL_LOGGING_WARNING, message)
            raise KeyboardInterrupt("private callback failure")

        local_logging.warn_operational_logging_failure(broken_callback)

    def test_factory_creates_only_default_log_directory_and_returns_logger(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            logger = local_logging.create_operational_logger(base, "cli")
            self.assertIsInstance(logger, local_logging.LocalJsonLogger)
            self.assertTrue((base / "data" / "logs").is_dir())
            self.assertFalse((base / "data" / "logs" / "wissensraum.jsonl").exists())
            logger.event("probe", metadata={"backend": "sqlite", "provider": "ollama"})
            logger.close()
            self.assertTrue((base / "data" / "logs" / "wissensraum.jsonl").is_file())

    def test_factory_redacts_supplied_known_secret_in_allowed_metadata(self):
        secret = "standalone-known-secret"
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            logger = local_logging.create_operational_logger(
                base,
                "cli",
                known_secrets=(secret,),
            )
            self.assertIsNotNone(logger)
            logger.event("probe", metadata={"status": f"ready.{secret}.done"})
            logger.close()

            text = (base / "data" / "logs" / "wissensraum.jsonl").read_text(encoding="utf-8")
            record = json.loads(text)
            self.assertNotIn(secret, text)
            self.assertEqual("ready.[REDACTED].done", record["status"])

    def test_factory_failure_returns_none_and_warns_without_details(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            (base / "data").write_text("private-secret", encoding="utf-8")
            warnings = []
            logger = local_logging.create_operational_logger(base, "cli", warnings.append)
            self.assertIsNone(logger)
            self.assertEqual([local_logging.OPERATIONAL_LOGGING_WARNING], warnings)
            rendered = repr(warnings)
            self.assertNotIn(str(base), rendered)
            self.assertNotIn("private-secret", rendered)

    def test_close_failure_releases_destination_before_generic_notification(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / "data" / "logs" / "wissensraum.jsonl"
            replacement = []
            warnings = []
            callback_observed_lock = []

            def warning_callback(message):
                warnings.append(message)
                acquired = []
                probe = threading.Thread(
                    target=lambda: (logger._state_lock.acquire(), acquired.append(True),
                                    logger._state_lock.release())
                )
                probe.start()
                probe.join(1)
                callback_observed_lock.append(probe.is_alive())
                replacement.append(local_logging.LocalJsonLogger(path, "replacement"))

            logger = local_logging.create_operational_logger(base, "cli", warning_callback)
            with mock.patch.object(
                logger._handler,
                "close",
                side_effect=KeyboardInterrupt("private close path"),
            ):
                logger.close()

            self.assertEqual([local_logging.OPERATIONAL_LOGGING_WARNING], warnings)
            self.assertEqual([False], callback_observed_lock)
            self.assertEqual(1, len(replacement))
            replacement[0].close()


class CliLoggingIntegrationTests(unittest.TestCase):
    def _store(self):
        store = mock.Mock(backend="sqlite")
        store.list.return_value = []
        return store

    def test_doctor_never_initializes_logging_or_creates_log_directory(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(cli, "BASE", Path(folder)), \
             mock.patch.object(cli, "run_diagnostics", return_value=()), \
             mock.patch.object(cli, "create_operational_logger") as factory, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["doctor"]))
        factory.assert_not_called()
        self.assertFalse((Path(folder) / "data" / "logs").exists())

    def test_success_logs_lifecycle_after_store_startup_and_always_closes(self):
        store = self._store()
        logger = mock.Mock()
        settings = mock.Mock()
        order = []
        settings.open_store.side_effect = lambda path: order.append("store") or store
        with mock.patch.object(cli, "DatabaseSettings", return_value=settings), \
             mock.patch.object(cli, "SystemVault"), \
             mock.patch.object(cli, "create_operational_logger",
                               side_effect=lambda *args, **kwargs: order.append("logger") or logger), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["list"]))
        self.assertEqual(["store", "logger"], order)
        self.assertEqual("command_started", logger.event.call_args_list[0].args[0])
        self.assertEqual("command_completed", logger.event.call_args_list[1].args[0])
        logger.close.assert_called_once_with()

    def test_cli_supplies_already_open_postgresql_password_for_redaction(self):
        secret = "standalone-database-secret"
        store = self._store()
        store.password = secret
        logger = mock.Mock()
        settings = mock.Mock()
        settings.open_store.return_value = store
        with mock.patch.object(cli, "DatabaseSettings", return_value=settings), \
             mock.patch.object(cli, "SystemVault"), \
             mock.patch.object(cli, "create_operational_logger", return_value=logger) as factory, \
             redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(["list"]))

        self.assertEqual((secret,), factory.call_args.kwargs["known_secrets"])

    def test_factory_returning_none_preserves_exact_success_output(self):
        store = self._store()
        settings = mock.Mock()
        settings.open_store.return_value = store
        output = io.StringIO()
        with mock.patch.object(cli, "DatabaseSettings", return_value=settings), \
             mock.patch.object(cli, "SystemVault"), \
             mock.patch.object(cli, "create_operational_logger", return_value=None), \
             redirect_stdout(output):
            result = cli.main(["list"])
        self.assertEqual(0, result)
        self.assertEqual("[]\n", output.getvalue())

    def test_failure_logs_only_safe_command_backend_exception_class_and_code(self):
        private_query = "private query token=super-secret"
        private_message = "database leaked super-secret"

        class HostileExceptionMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise AssertionError("metaclass name hook was invoked")
                return super().__getattribute__(name)

        class HostileCommandError(RuntimeError, metaclass=HostileExceptionMeta):
            pass

        store = self._store()
        store.search.side_effect = HostileCommandError(private_message)
        logger = mock.Mock()
        settings = mock.Mock()
        settings.open_store.return_value = store
        with mock.patch.object(cli, "DatabaseSettings", return_value=settings), \
             mock.patch.object(cli, "SystemVault"), \
             mock.patch.object(cli, "create_operational_logger", return_value=logger), \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cli.main(["search", private_query])
        failed = logger.event.call_args_list[-1]
        self.assertEqual("command_failed", failed.args[0])
        self.assertEqual({"operation": "search", "backend": "sqlite"}, failed.kwargs["metadata"])
        self.assertEqual("HostileCommandError", failed.kwargs["exception_class"])
        self.assertEqual("CLI_COMMAND_FAILED", failed.kwargs["error_code"])
        self.assertNotIn(private_query, repr(logger.mock_calls))
        self.assertNotIn(private_message, repr(logger.mock_calls))
        logger.close.assert_called_once_with()

    def test_logger_base_exceptions_warn_generically_without_changing_cli_success(self):
        store = self._store()
        logger = mock.Mock()
        logger.event.side_effect = KeyboardInterrupt("raw event secret C:/private")
        logger.close.side_effect = KeyboardInterrupt("raw close secret C:/private")
        settings = mock.Mock()
        settings.open_store.return_value = store
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(cli, "DatabaseSettings", return_value=settings), \
             mock.patch.object(cli, "SystemVault"), \
             mock.patch.object(cli, "create_operational_logger", return_value=logger), \
             redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(0, cli.main(["list"]))
        self.assertEqual("[]\n", output.getvalue())
        self.assertEqual(
            [local_logging.OPERATIONAL_LOGGING_WARNING] * 3,
            errors.getvalue().splitlines(),
        )
        self.assertNotIn("raw", errors.getvalue())
        self.assertNotIn("private", errors.getvalue())


class DesktopLoggingIntegrationTests(unittest.TestCase):
    @staticmethod
    def _window(mainloop=None):
        window = object.__new__(app.App)
        window.store = mock.Mock(backend="sqlite")
        window._operational_logger = None
        window._operational_logging_closed = False
        window.mainloop = mainloop or mock.Mock()
        window.destroy = mock.Mock()
        return window

    def test_startup_failure_does_not_initialize_logging(self):
        failure = app.StartupFailure((DiagnosticResult("database", "FAIL", "safe", {}),))
        with mock.patch.object(app, "App", side_effect=failure), \
             mock.patch.object(app, "show_startup_failure"), \
             mock.patch.object(app, "create_operational_logger") as factory:
            self.assertEqual(1, app.main())
        factory.assert_not_called()

    def test_generic_app_construction_failure_does_not_initialize_logging(self):
        with mock.patch.object(app, "App", side_effect=RuntimeError("private constructor")), \
             mock.patch.object(app, "create_operational_logger") as factory, \
             redirect_stderr(io.StringIO()):
            self.assertEqual(1, app.main())
        factory.assert_not_called()

    def test_success_initializes_logging_only_after_app_construction(self):
        order = []
        window = self._window()
        logger = mock.Mock()
        with mock.patch.object(app, "App", side_effect=lambda: order.append("app") or window), \
             mock.patch.object(app, "create_operational_logger",
                               side_effect=lambda *args, **kwargs: order.append("logger") or logger):
            self.assertEqual(0, app.main())
        self.assertEqual(["app", "logger"], order)
        self.assertEqual("application_started", logger.event.call_args_list[0].args[0])
        self.assertEqual({"backend": "sqlite"}, logger.event.call_args_list[0].kwargs["metadata"])
        window.mainloop.assert_called_once_with()
        self.assertEqual("application_stopped", logger.event.call_args_list[1].args[0])
        logger.close.assert_called_once_with()

    def test_desktop_supplies_already_open_postgresql_password_for_redaction(self):
        secret = "standalone-database-secret"
        window = self._window()
        setattr(window.store, "password", secret)
        logger = mock.Mock()
        with mock.patch.object(app, "App", return_value=window), \
             mock.patch.object(app, "create_operational_logger", return_value=logger) as factory:
            self.assertEqual(0, app.main())

        self.assertEqual((secret,), factory.call_args.kwargs["known_secrets"])

    def test_factory_returning_none_preserves_desktop_success_behavior(self):
        window = self._window()
        with mock.patch.object(app, "App", return_value=window), \
             mock.patch.object(app, "create_operational_logger", return_value=None):
            self.assertEqual(0, app.main())
        window.mainloop.assert_called_once_with()
        self.assertIsNone(window._operational_logger)

    def test_desktop_event_and_close_base_exceptions_warn_generically_without_changing_result(self):
        window = self._window()
        logger = mock.Mock()
        logger.event.side_effect = KeyboardInterrupt("raw desktop event C:/private")
        logger.close.side_effect = KeyboardInterrupt("raw desktop close C:/private")
        errors = io.StringIO()
        with mock.patch.object(app, "App", return_value=window), \
             mock.patch.object(app, "create_operational_logger", return_value=logger), \
             redirect_stderr(errors):
            self.assertEqual(0, app.main())
        self.assertEqual(
            [local_logging.OPERATIONAL_LOGGING_WARNING] * 3,
            errors.getvalue().splitlines(),
        )
        self.assertNotIn("raw", errors.getvalue())
        self.assertNotIn("private", errors.getvalue())

    def test_mainloop_failure_logs_safe_details_closes_and_reraises_same_exception(self):
        class HostileExceptionMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise AssertionError("metaclass name hook was invoked")
                return super().__getattribute__(name)

        class HostileRuntimeError(RuntimeError, metaclass=HostileExceptionMeta):
            pass

        private_error = HostileRuntimeError("private customer C:/secret/file.txt")
        window = self._window(mainloop=mock.Mock(side_effect=private_error))
        logger = mock.Mock()
        with mock.patch.object(app, "App", return_value=window), \
             mock.patch.object(app, "create_operational_logger", return_value=logger):
            with self.assertRaises(RuntimeError) as raised:
                app.main()
        self.assertIs(private_error, raised.exception)
        failed = logger.event.call_args_list[-1]
        self.assertEqual("application_failed", failed.args[0])
        self.assertEqual({"backend": "sqlite"}, failed.kwargs["metadata"])
        self.assertEqual("HostileRuntimeError", failed.kwargs["exception_class"])
        self.assertEqual("DESKTOP_RUNTIME_FAILED", failed.kwargs["error_code"])
        self.assertNotIn(str(private_error), repr(logger.mock_calls))
        logger.close.assert_called_once_with()

    def test_background_failure_logs_safe_class_and_code_before_ui_conversion(self):
        instance = object.__new__(app.App)
        instance.busy = False
        instance.buttons = []
        instance.status = mock.Mock()
        instance.queue = queue.Queue()
        instance._operational_logger = mock.Mock()
        instance._operational_logging_closed = False
        class HostileExceptionMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise AssertionError("metaclass name hook was invoked")
                return super().__getattribute__(name)

        class HostileBackgroundError(ValueError, metaclass=HostileExceptionMeta):
            pass

        private_message = "private path C:/secret/customer.txt"

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target
            def start(self):
                self.target()

        with mock.patch.object(app.threading, "Thread", ImmediateThread):
            instance.work(lambda: (_ for _ in ()).throw(HostileBackgroundError(private_message)), mock.Mock())

        logged = instance._operational_logger.event.call_args_list
        self.assertEqual("background_operation_started", logged[0].args[0])
        failed = logged[1]
        self.assertEqual("background_operation_failed", failed.args[0])
        self.assertEqual({"operation": "background"}, failed.kwargs["metadata"])
        self.assertEqual("HostileBackgroundError", failed.kwargs["exception_class"])
        self.assertEqual("DESKTOP_BACKGROUND_FAILED", failed.kwargs["error_code"])
        self.assertNotIn(private_message, repr(logged))
        _, _, ui_error = instance.queue.get_nowait()
        self.assertEqual(private_message, ui_error)

    def test_background_success_preserves_result_identity_and_logs_no_result_data(self):
        instance = object.__new__(app.App)
        instance.busy = False
        instance.buttons = []
        instance.status = mock.Mock()
        instance.queue = queue.Queue()
        instance._operational_logger = mock.Mock()
        instance._operational_logging_closed = False
        result = {"private": object()}
        done = mock.Mock()

        class ImmediateThread:
            def __init__(self, target, daemon):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(app.threading, "Thread", ImmediateThread):
            instance.work(lambda: result, done)

        queued_done, queued_result, queued_error = instance.queue.get_nowait()
        self.assertIs(done, queued_done)
        self.assertIs(result, queued_result)
        self.assertIsNone(queued_error)
        logged = instance._operational_logger.event.call_args_list
        self.assertEqual(
            ["background_operation_started", "background_operation_completed"],
            [call.args[0] for call in logged],
        )
        self.assertNotIn(result, [call.kwargs for call in logged])

    def test_exit_logs_stop_closes_idempotently_then_destroys(self):
        instance = object.__new__(app.App)
        logger = mock.Mock()
        instance.store = mock.Mock(backend="sqlite")
        instance._operational_logger = logger
        instance._operational_logging_closed = False
        instance.destroy = mock.Mock()
        instance.exit_application()
        instance.exit_application()
        self.assertEqual(1, sum(call.args == ("application_stopped",) for call in logger.event.call_args_list))
        logger.close.assert_called_once_with()
        self.assertEqual(2, instance.destroy.call_count)

    def test_exit_during_mainloop_and_main_finally_stop_and_close_only_once(self):
        logger = mock.Mock()
        window = self._window()
        window.mainloop = mock.Mock(side_effect=window.exit_application)
        with mock.patch.object(app, "App", return_value=window), \
             mock.patch.object(app, "create_operational_logger", return_value=logger):
            self.assertEqual(0, app.main())
        stopped = [
            call for call in logger.event.call_args_list
            if call.args == ("application_stopped",)
        ]
        self.assertEqual(1, len(stopped))
        logger.close.assert_called_once_with()
        window.destroy.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

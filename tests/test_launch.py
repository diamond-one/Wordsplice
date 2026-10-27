"""Startup regressions; no downloads, browser launches, or real server required."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launch


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.data.mkdir()
        self.python = self.root / ('.venv/Scripts/python.exe' if launch.os.name == 'nt' else '.venv/bin/python')
        self.python.parent.mkdir(parents=True)
        self.python.touch()
        self.root_patch = patch.object(launch, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_interrupted_install_is_resumed_and_verified(self):
        with patch.object(launch, 'dependencies_ready', side_effect=[False, True]) as verify, \
                patch.object(launch, 'run_setup') as setup:
            self.assertEqual(launch.prepare_runtime(self.data), self.python)
        self.assertEqual(verify.call_count, 2)
        commands = [call.args[0] for call in setup.call_args_list]
        self.assertIn('ensurepip', commands[0])
        self.assertIn('install', commands[1])
        self.assertIn(str(self.root / 'requirements.txt'), commands[1])

    def test_healthy_install_needs_no_downloads(self):
        with patch.object(launch, 'dependencies_ready', return_value=True), \
                patch.object(launch, 'run_setup') as setup:
            launch.prepare_runtime(self.data)
        setup.assert_not_called()

    def test_successful_installer_with_broken_imports_blocks_start(self):
        with patch.object(launch, 'dependencies_ready', return_value=False), \
                patch.object(launch, 'run_setup'), \
                patch.object(launch, 'alive', return_value=False), \
                patch.object(launch.subprocess, 'Popen') as spawn, \
                patch.dict(launch.os.environ, {'WORDSPLICE_DATA': str(self.data)}):
            with self.assertRaisesRegex(RuntimeError, 'still cannot load'):
                launch.main(no_browser=True)
        spawn.assert_not_called()

    def test_failed_setup_retains_child_output_and_reports_log(self):
        log = self.data / 'setup.log'
        with self.assertRaisesRegex(RuntimeError, 'exit code 7.*setup.log'):
            launch.run_setup([sys.executable, '-c', "print('installer failure detail'); raise SystemExit(7)"],
                             log, 'Test installation')
        self.assertIn('installer failure detail', log.read_text(encoding='utf-8'))

    def test_import_failure_is_saved_for_diagnosis(self):
        log = self.data / 'setup.log'
        # A real subprocess writes an import error into the same diagnostic sink.
        real_run = subprocess.run
        def fail_import(command, **kwargs):
            return real_run([sys.executable, '-c', 'import wordsplice_intentionally_missing'], **kwargs)
        with patch.object(launch.subprocess, 'run', side_effect=fail_import):
            self.assertFalse(launch.dependencies_ready(sys.executable, log))
        self.assertIn('ModuleNotFoundError', log.read_text(encoding='utf-8'))

    def test_server_exit_is_reported_without_waiting_thirty_seconds(self):
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with patch.object(launch, 'alive', return_value=False), \
                patch.object(launch, 'prepare_runtime', return_value=self.python), \
                patch.object(launch.subprocess, 'Popen', return_value=process), \
                patch.dict(launch.os.environ, {'WORDSPLICE_DATA': str(self.data)}), \
                patch.object(launch, 'open_browser') as browser:
            with self.assertRaisesRegex(RuntimeError, 'studio exited.*studio.log'):
                launch.main()
        browser.assert_not_called()

    def test_running_studio_skips_setup_and_honors_no_browser(self):
        with patch.object(launch, 'alive', return_value=True), \
                patch.object(launch, 'prepare_runtime') as setup, \
                patch.object(launch, 'open_browser') as browser:
            launch.main(no_browser=True)
        setup.assert_not_called()
        browser.assert_not_called()


if __name__ == '__main__':
    unittest.main()

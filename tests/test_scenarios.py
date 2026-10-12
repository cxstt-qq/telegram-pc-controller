from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app_catalog import AppEntry, discover_apps
from bot import Bot
from config import Config
import scenarios


class ScenarioTests(unittest.TestCase):
    def test_catalog_finds_desktop_and_start_menu_shortcuts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            desktop = root / "Desktop"
            start = root / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
            desktop.mkdir()
            start.mkdir(parents=True)
            (desktop / "Potassium.lnk").touch()
            (start / "Roblox Account Manager.lnk").touch()
            with (patch("app_catalog.Path.home", return_value=root),
                  patch("app_catalog._start_apps", return_value=[]),
                  patch.dict("app_catalog.os.environ", {"PUBLIC": str(root), "PROGRAMDATA": str(root)})):
                found = discover_apps()
                filtered = discover_apps("roblox")
        self.assertEqual({entry.name for entry in found}, {"Potassium", "Roblox Account Manager"})
        self.assertEqual([entry.name for entry in filtered], ["Roblox Account Manager"])

    def test_catalog_includes_registered_apps_without_shortcuts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (patch("app_catalog.Path.home", return_value=root),
                  patch("app_catalog._start_apps", return_value=[("Calculator", "Microsoft.Calculator")]),
                  patch.dict("app_catalog.os.environ", {"PUBLIC": str(root), "PROGRAMDATA": str(root)})):
                found = discover_apps("calc")
        self.assertEqual([(entry.name, entry.app_id) for entry in found],
                         [("Calculator", "Microsoft.Calculator")])

    def test_scenario_preflight_does_not_close_apps_without_admin(self):
        with (patch("scenarios.is_admin", return_value=False),
              patch("scenarios.close_open_apps") as close):
            with self.assertRaises(PermissionError):
                scenarios.start_farm()
        close.assert_not_called()

    def test_close_open_apps_preserves_ram_and_forces_other_app(self):
        ram = Mock()
        ram.pid = 11
        ram.create_time.return_value = 1.0
        ram.exe.return_value = str(scenarios.RAM_EXE)
        ram.name.return_value = scenarios.RAM_EXE.name
        other = Mock()
        other.pid = 22
        other.create_time.return_value = 2.0
        other.exe.return_value = r"C:\Apps\other.exe"
        other.name.return_value = "other.exe"
        other.is_running.return_value = True
        windows = [
            scenarios.desktop.Window(101, 11, "RAM", scenarios.RAM_EXE.name),
            scenarios.desktop.Window(202, 22, "Other", "other.exe"),
        ]
        with (patch("scenarios.desktop.windows", return_value=windows),
              patch("scenarios.psutil.Process", side_effect=lambda pid: {11: ram, 22: other}[pid]),
              patch("scenarios.desktop.close_window") as close,
              patch("scenarios.desktop.kill_process") as kill):
            count, failures = scenarios.close_open_apps({scenarios.RAM_EXE})
        self.assertEqual((count, failures), (1, []))
        close.assert_called_once_with(202, grace_seconds=1.5)
        kill.assert_called_once_with(22, expected_create_time=2.0)

    def test_start_sequence_preserves_requested_apps(self):
        process = Mock(pid=900)
        process.create_time.return_value = 100.0
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "state.json"
            with (patch("scenarios.STATE_PATH", state),
                  patch("scenarios.is_admin", return_value=True),
                  patch("scenarios._running_farm_processes", return_value=[]),
                  patch("scenarios.proxy_process_ids", return_value={77}),
                  patch("scenarios.close_open_apps", return_value=(4, [])) as close,
                  patch("scenarios._launch_bat", return_value=process) as launch,
                  patch("scenarios.time.sleep")):
                response = scenarios.start_farm("socks5h://127.0.0.1:1080")
        close.assert_called_once_with({scenarios.POTASSIUM_EXE, scenarios.RAM_EXE}, {77})
        self.assertEqual([call.args[0] for call in launch.call_args_list],
                         [scenarios.ZAPRET_BAT, scenarios.FARM_BAT])
        self.assertIn("Закрыто приложений: 4", response)

    def test_stop_sequence_keeps_ram_and_proxy(self):
        with (patch("scenarios.is_admin", return_value=True),
              patch("scenarios._running_farm_processes", return_value=[]),
              patch("scenarios._zapret_processes", return_value=[]),
              patch("scenarios.proxy_process_ids", return_value={77}),
              patch("scenarios.close_open_apps", return_value=(3, [])) as close):
            response = scenarios.stop_farm("socks5h://127.0.0.1:1080")
        close.assert_called_once_with({scenarios.RAM_EXE}, {77})
        self.assertIn("RAM оставлен", response)

    def test_bot_lists_and_launches_discovered_apps(self):
        bot = Bot(Config("unused", 123, 5, "tesseract"))
        bot.send = Mock()
        bot.send_screen = Mock()
        entry = AppEntry("Potassium", "Рабочий стол", Path("C:/Potassium.lnk"))
        try:
            with patch("bot.discover_apps", return_value=[entry]):
                bot.apps(123, "potas")
            self.assertIn("Potassium", str(bot.send.call_args.args[2]))
            nonce = bot.app_results[123][0]
            with (patch("pathlib.Path.is_file", return_value=True),
                  patch("bot.desktop.launch") as launch):
                bot.launch_discovered_app(123, 0, nonce)
                launch.assert_called_once_with(str(entry.path))
                with self.assertRaises(ValueError):
                    bot.launch_discovered_app(123, 0, "old-list")
            bot.app_results[123] = (nonce, [AppEntry("Calculator", "Установлено", Path(),
                                                   "Microsoft.Calculator")], "")
            with patch("bot.subprocess.Popen") as popen:
                bot.launch_discovered_app(123, 0, nonce)
                popen.assert_called_once()
                self.assertIn("shell:AppsFolder\\Microsoft.Calculator", popen.call_args.args[0])
        finally:
            bot.executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()

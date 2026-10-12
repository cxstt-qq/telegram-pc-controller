from __future__ import annotations

import unittest
import io
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from PIL import Image, ImageDraw, ImageFont

import windows_control as desktop
from bot import Bot
from console_session import ConsoleSession
from config import Config, validate_proxy_url
from telegram_api import TelegramAPI, TelegramError


TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


class CoreTests(unittest.TestCase):
    def test_ocr_finds_text_and_coordinates(self):
        if not Path(TESSERACT).exists():
            self.skipTest("Tesseract not installed")
        image = Image.new("RGB", (600, 170), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("arial.ttf", 38)
        draw.text((30, 50), "Start farm", font=font, fill="black")
        hits = desktop.find_text(image, "Start farm", TESSERACT)
        self.assertTrue(hits)
        self.assertTrue(20 <= hits[0].center[0] <= 300)
        self.assertTrue(40 <= hits[0].center[1] <= 120)

    def test_ocr_ignores_echoed_find_command(self):
        if not Path(TESSERACT).exists():
            self.skipTest("Tesseract not installed")
        image = Image.new("RGB", (700, 250), "white")
        draw = ImageDraw.Draw(image)
        font = ImageFont.truetype("arial.ttf", 32)
        draw.text((20, 20), "/find Target", font=font, fill="black")
        draw.text((20, 150), "Target", font=font, fill="black")
        hits = desktop.find_text(image, "Target", TESSERACT)
        self.assertTrue(hits)
        self.assertTrue(all(hit.y > 100 for hit in hits))

    def test_ocr_tsv_quote_does_not_swallow_later_rows(self):
        # Tesseract TSV is unquoted; an OCR result containing a quote is literal.
        header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
        first = '5\t1\t1\t1\t1\t1\t10\t10\t20\t10\t80\t"'
        second = "5\t1\t2\t1\t1\t1\t80\t90\t60\t15\t90\tCookie(s)"
        result = Mock(returncode=0, stderr=b"", stdout=(header + "\n" + first + "\n" + second + "\n").encode())
        with patch("windows_control.subprocess.run", return_value=result):
            hits = desktop.find_text(Image.new("RGB", (200, 150)), "cookie", TESSERACT)
        self.assertEqual([(hit.text, hit.center) for hit in hits], [("Cookie(s)", (110, 97))])

    def test_cmd_output_and_exit_code(self):
        bot = Bot(Config("unused", 1, 10, TESSERACT))
        try:
            text = bot.cmd("echo hello & exit /b 7", 1)
            self.assertIn("hello", text)
            self.assertIn("7", text.splitlines()[1])
        finally:
            bot.executor.shutdown(wait=True)

    def test_console_session_keeps_directory_and_environment(self):
        session = ConsoleSession()
        try:
            output, status, cwd = session.execute(r"cd /d C:\Windows", 5)
            self.assertEqual(status, "0")
            self.assertEqual(cwd.casefold(), r"c:\windows")
            output, status, cwd = session.execute("set RPC_TEST=working", 5)
            self.assertEqual(status, "0")
            output, status, cwd = session.execute("echo %RPC_TEST%", 5)
            self.assertIn("working", output)
            self.assertEqual(cwd.casefold(), r"c:\windows")
        finally:
            session.close()

    def test_bot_routes_plain_messages_to_console_mode(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        try:
            bot.handle_command(123, 123, "/cmd")
            self.assertIsNotNone(bot.console)
            bot.handle_update({"message": {
                "from": {"id": 123}, "chat": {"id": 123, "type": "private"},
                "text": r"cd /d C:\Windows",
            }})
            bot.future.result(timeout=5)
            output, _, cwd = bot.console.execute("cd", 5)
            self.assertEqual(cwd.casefold(), r"c:\windows")
            bot.handle_command(123, 123, "/cmd off")
            self.assertIsNone(bot.console)
        finally:
            if bot.console is not None:
                bot.console.close()
            bot.executor.shutdown(wait=True)

    def test_key_command_handles_single_and_combination(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        bot.send_screen = Mock()
        try:
            with patch("bot.desktop.key") as press, patch("bot.desktop.hotkey") as combo:
                bot.handle_command(123, 123, "/key enter")
                bot.future.result(timeout=5)
                press.assert_called_once_with("enter")
                bot.handle_command(123, 123, "/key ctrl+shift+s")
                bot.future.result(timeout=5)
                combo.assert_called_once_with(["ctrl", "shift", "s"])
        finally:
            bot.executor.shutdown(wait=True)

    def test_find_reports_center_coordinates(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        bot.api.photo = Mock()
        hit = desktop.TextHit("Target", 100, 50, 40, 20)
        try:
            with (patch("bot.desktop.screen_image", return_value=Image.new("RGB", (200, 100))),
                  patch("bot.desktop.find_text", return_value=[hit]),
                  patch("bot.desktop.annotate_hits", return_value=b"png"),
                  patch("bot.desktop.active_window", return_value=None)):
                bot.find("Target", 123)
            self.assertIn("Target — центр (120, 60)", bot.send.call_args.args[1])
        finally:
            bot.executor.shutdown(wait=True)

    def test_merged_screen_and_click_commands(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        bot.send_screen = Mock()
        try:
            bot.handle_command(123, 123, "/screen grid")
            bot.future.result(timeout=5)
            bot.send_screen.assert_called_with(123, grid=True, raw_file=False)
            with patch("bot.desktop.click") as click:
                bot.handle_command(123, 123, "/click right 10 20")
                bot.future.result(timeout=5)
                click.assert_called_once_with(10, 20, "right", 1)
                bot.send_screen.assert_called_with(123, "click", marker=(10, 20))
        finally:
            bot.executor.shutdown(wait=True)

    def test_marked_click_and_zoom_near_screen_edge(self):
        image = Image.new("RGB", (400, 300), "#222222")
        marked = Image.open(io.BytesIO(desktop.marked_png(image, 200, 150)))
        self.assertEqual(marked.getpixel((212, 150)), (227, 34, 34))
        self.assertEqual(marked.getpixel((200, 150)), (34, 34, 34))
        self.assertEqual(image.getpixel((212, 150)), (34, 34, 34))
        zoom, bounds = desktop.zoom_png(image, 390, 290, 200)
        self.assertEqual(bounds, (200, 100, 400, 300))
        self.assertEqual(Image.open(io.BytesIO(zoom)).size, (400, 400))
        with self.assertRaises(ValueError):
            desktop.zoom_png(image, 400, 290)

    def test_zoom_command_sends_cropped_image(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.api.photo = Mock()
        try:
            with patch("bot.desktop.screen_image", return_value=Image.new("RGB", (400, 300))):
                bot.handle_command(123, 123, "/zoom 390 290 200")
                bot.future.result(timeout=5)
            self.assertIn("X 200–399, Y 100–299", bot.api.photo.call_args.args[2])
        finally:
            bot.executor.shutdown(wait=True)

    def test_window_and_clipboard_commands(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        bot.send_screen = Mock()
        try:
            with patch("bot.desktop.set_window_state") as state:
                bot.handle_command(123, 123, "/window minimize 99")
                bot.future.result(timeout=5)
                state.assert_called_once_with(99, "minimize")
            with patch("bot.desktop.set_clipboard_text") as setclip:
                bot.handle_command(123, 123, "/clipboard set hello")
                bot.future.result(timeout=5)
                setclip.assert_called_once_with("hello")
        finally:
            bot.executor.shutdown(wait=True)

    def test_click_button_prompt_uses_merged_command(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.send = Mock()
        bot.send_screen = Mock()
        try:
            bot.prompt(123, "click:double")
            with patch("bot.desktop.click") as click:
                bot.handle_update({"message": {
                    "from": {"id": 123}, "chat": {"id": 123, "type": "private"},
                    "text": "10 20",
                }})
                bot.future.result(timeout=5)
                click.assert_called_once_with(10, 20, "left", 2)
        finally:
            bot.executor.shutdown(wait=True)

    def test_unauthorized_update_cannot_execute(self):
        bot = Bot(Config("unused", 123, 10, TESSERACT))
        try:
            bot.handle_update({
                "message": {
                    "from": {"id": 456}, "chat": {"id": 456, "type": "private"},
                    "text": "/cmd echo forbidden",
                }
            })
            self.assertIsNone(bot.future)
        finally:
            bot.executor.shutdown(wait=True)

    def test_screenshot_matches_monitor(self):
        image = desktop.screen_image()
        self.assertGreater(image.width, 0)
        self.assertGreater(image.height, 0)

    def test_close_window_kills_process_left_in_tray(self):
        target = desktop.Window(99, 5432, "Tray app", "tray.exe")
        process = Mock()
        process.create_time.return_value = 123.0
        process.is_running.return_value = True
        calls = [0]

        def visible_windows(**_):
            calls[0] += 1
            return [target] if calls[0] == 1 else []

        with (patch("windows_control.windows", side_effect=visible_windows),
              patch("windows_control.user32") as user32,
              patch("windows_control.psutil.Process", return_value=process),
              patch("windows_control.kill_process") as kill):
            user32.PostMessageW.return_value = True
            result = desktop.close_window(99, grace_seconds=0.02)
        kill.assert_called_once_with(5432, expected_create_time=123.0)
        self.assertIn("завершён", result)

    def test_close_window_keeps_process_if_window_stays_open(self):
        target = desktop.Window(99, 5432, "Unsaved document", "editor.exe")
        process = Mock()
        process.create_time.return_value = 123.0
        process.is_running.return_value = True
        with (patch("windows_control.windows", return_value=[target]),
              patch("windows_control.user32") as user32,
              patch("windows_control.psutil.Process", return_value=process),
              patch("windows_control.kill_process") as kill):
            user32.PostMessageW.return_value = True
            result = desktop.close_window(99, grace_seconds=0.02)
        kill.assert_not_called()
        self.assertIn("не закрылось", result)

    def test_kill_rejects_reused_pid(self):
        process = Mock()
        process.create_time.return_value = 200.0
        with patch("windows_control.psutil.Process", return_value=process):
            with self.assertRaises(RuntimeError):
                desktop.kill_process(5432, expected_create_time=100.0)
        process.kill.assert_not_called()

    def test_background_components_are_hidden_by_default(self):
        for name in ("TextInputHost.exe", "NVIDIA Overlay.exe", "nvcontainer.exe",
                     "NVDisplay.Container.exe", "RuntimeBroker.exe"):
            self.assertTrue(desktop.is_background_process(name), name)
        for name in ("NVIDIA app.exe", "opera.exe", "explorer.exe"):
            self.assertFalse(desktop.is_background_process(name), name)
        self.assertFalse(any(desktop.is_background_process(w.process) for w in desktop.windows()))

    def test_full_list_option(self):
        self.assertEqual(Bot.listing_options(""), (0, False))
        self.assertEqual(Bot.listing_options("all 2"), (1, True))

    def test_window_button_edits_original_list_after_close(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        target = desktop.Window(99, 5432, "Test window", "test.exe")
        bot.api.message = Mock(return_value={"message_id": 50})
        bot.api.edit_message = Mock(return_value={"message_id": 50})
        bot.api.answer_callback = Mock()
        bot.send_screen = Mock()
        try:
            with (patch("bot.desktop.windows", side_effect=[[target], [target], []]),
                  patch("bot.desktop.close_window", return_value="Окно закрыто") as close):
                bot.list_windows(123)
                bot.handle_callback({"id": "callback", "message": {"message_id": 50},
                                     "data": "win:close:99:5432:0:0"}, 123, 123)
                bot.future.result(timeout=5)
            close.assert_called_once_with(99)
            self.assertEqual(bot.api.edit_message.call_args.args[:2], (123, 50))
            self.assertIn("Видимых окон нет", bot.api.edit_message.call_args.args[2])
            buttons = bot.api.edit_message.call_args.args[3]["inline_keyboard"]
            self.assertTrue(any(button["callback_data"].startswith("windows-refresh:")
                                for row in buttons for button in row))
        finally:
            bot.executor.shutdown(wait=True)

    def test_window_page_clamps_after_last_window_closes(self):
        bot = Bot(Config("unused", 123, 5, TESSERACT))
        bot.api.edit_message = Mock(return_value={"message_id": 50})
        bot.api.message = Mock(return_value={"message_id": 50})
        try:
            windows = [desktop.Window(i, i, f"Window {i}", "test.exe") for i in range(15)]
            with patch("bot.desktop.windows", return_value=windows):
                bot.list_windows(123, page=1, message_id=50)
            self.assertIn("страница 1", bot.api.edit_message.call_args.args[2])
            self.assertEqual(bot.window_list_state[123], (50, 0, False))
        finally:
            bot.executor.shutdown(wait=True)

    def test_edit_message_sends_text_and_keyboard_together(self):
        api = TelegramAPI("fake-token")
        api.call = Mock(return_value={"message_id": 50})
        keyboard = {"inline_keyboard": [[{"text": "Обновить", "callback_data": "refresh"}]]}
        api.edit_message(123, 50, "Новый список", keyboard)
        kwargs = api.call.call_args.kwargs
        self.assertEqual(api.call.call_args.args, ("editMessageText",))
        self.assertEqual(kwargs["message_id"], 50)
        self.assertIn("refresh", kwargs["reply_markup"])

    def test_proxy_used_for_telegram_calls(self):
        proxy = "socks5h://user:secret@localhost:1080"
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {"username": "test_bot"}}
        with patch("telegram_api.requests.post", return_value=response) as post:
            result = TelegramAPI("fake-token", proxy).call("getMe")
        self.assertEqual(result["username"], "test_bot")
        self.assertEqual(post.call_args.kwargs["proxies"], {"http": proxy, "https": proxy})

    def test_long_poll_keeps_api_and_http_timeouts_separate(self):
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": []}
        with patch("telegram_api.requests.post", return_value=response) as post:
            self.assertEqual(TelegramAPI("fake-token").updates(42), [])
        self.assertEqual(post.call_args.kwargs["data"]["timeout"], 25)
        self.assertEqual(post.call_args.kwargs["timeout"], 35)
        self.assertEqual(post.call_args.kwargs["data"]["offset"], 42)

    def test_photo_upload_uses_proxy(self):
        response = Mock(ok=True)
        response.json.return_value = {"ok": True, "result": {}}
        proxy = "socks5h://localhost:1080"
        with patch("telegram_api.requests.post", return_value=response) as post:
            TelegramAPI("fake-token", proxy).photo(123, b"png-data")
        self.assertEqual(post.call_args.kwargs["proxies"]["https"], proxy)
        self.assertEqual(post.call_args.kwargs["timeout"], 60)
        self.assertIn("photo", post.call_args.kwargs["files"])

    def test_proxy_error_does_not_expose_credentials(self):
        proxy = "socks5h://user:secret@localhost:1080"
        with patch("telegram_api.requests.post", side_effect=requests.exceptions.ProxyError(proxy)):
            with self.assertRaises(TelegramError) as error:
                TelegramAPI("fake-token", proxy).call("getMe")
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("fake-token", str(error.exception))

    def test_proxy_url_validation(self):
        self.assertEqual(validate_proxy_url("socks5h://127.0.0.1:1080"),
                         "socks5h://127.0.0.1:1080")
        self.assertIsNone(validate_proxy_url(""))
        for invalid in ("localhost:1080", "socks5://localhost", "ftp://localhost:21"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_proxy_url(invalid)


if __name__ == "__main__":
    unittest.main()

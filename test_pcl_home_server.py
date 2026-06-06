import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path

import pcl_home_server as app


class PclHomeServerTests(unittest.TestCase):
    def setUp(self):
        self.original_config = deepcopy(app.CONFIG)
        self.original_config_path = app.CONFIG_PATH
        self.original_config_warnings = list(app.CONFIG_WARNINGS)
        self.original_config_file_state = deepcopy(app.CONFIG_FILE_STATE)
        self.original_status_cache = deepcopy(app.STATUS_CACHE)
        self.original_github_cache = deepcopy(app.GITHUB_CACHE)
        self.original_blog_cache = deepcopy(app.BLOG_CACHE)
        self.original_latency_history = list(app.LATENCY_HISTORY)
        self.original_runtime_log_path = app.RUNTIME_LOG_PATH
        self.original_next_cleanup = app.RATE_LIMIT_NEXT_CLEANUP_AT
        self.original_last_connect_host = app.LAST_SUCCESSFUL_CONNECT_HOST["host"]
        self.original_log = app.append_runtime_log
        app.RATE_LIMIT_HITS.clear()
        app.STATUS_CACHE.update({"status": None, "expires_at": 0, "refreshing": False})
        app.GITHUB_CACHE.update({"status": None, "expires_at": 0, "refreshing": False})
        app.BLOG_CACHE.update({"status": None, "expires_at": 0, "refreshing": False})
        app.LATENCY_HISTORY.clear()
        app.RATE_LIMIT_NEXT_CLEANUP_AT = 0
        app.LAST_SUCCESSFUL_CONNECT_HOST["host"] = ""
        app.CONFIG_WARNINGS.clear()
        app.append_runtime_log = lambda _message: None

    def tearDown(self):
        app.CONFIG.clear()
        app.CONFIG.update(self.original_config)
        app.CONFIG_PATH = self.original_config_path
        app.CONFIG_WARNINGS[:] = self.original_config_warnings
        app.CONFIG_FILE_STATE.clear()
        app.CONFIG_FILE_STATE.update(self.original_config_file_state)
        app.STATUS_CACHE.clear()
        app.STATUS_CACHE.update(self.original_status_cache)
        app.GITHUB_CACHE.clear()
        app.GITHUB_CACHE.update(self.original_github_cache)
        app.BLOG_CACHE.clear()
        app.BLOG_CACHE.update(self.original_blog_cache)
        app.LATENCY_HISTORY.clear()
        app.LATENCY_HISTORY.extend(self.original_latency_history)
        app.RUNTIME_LOG_PATH = self.original_runtime_log_path
        app.RATE_LIMIT_HITS.clear()
        app.RATE_LIMIT_NEXT_CLEANUP_AT = self.original_next_cleanup
        app.LAST_SUCCESSFUL_CONNECT_HOST["host"] = self.original_last_connect_host
        app.append_runtime_log = self.original_log

    def test_config_int_falls_back_and_applies_bounds(self):
        app.CONFIG["refreshSeconds"] = "not-a-number"
        self.assertEqual(app.config_int("refreshSeconds", 30, 5), 30)

        app.CONFIG["refreshSeconds"] = -1
        self.assertEqual(app.config_int("refreshSeconds", 30, 5), 5)

        app.CONFIG["listenPort"] = 99999
        self.assertEqual(app.config_int("listenPort", 3000, 1, 65535), 65535)

        app.CONFIG["listenPort"] = float("inf")
        self.assertEqual(app.config_int("listenPort", 3000, 1, 65535), 3000)

    def test_read_response_text_rejects_oversized_response(self):
        app.CONFIG["remoteFetchMaxBytes"] = 1024

        with self.assertRaisesRegex(ValueError, "remote response exceeds 1024 bytes"):
            app.read_response_text(io.BytesIO(b"x" * 1025))

    def test_read_response_text_decodes_with_error_policy(self):
        app.CONFIG["remoteFetchMaxBytes"] = 16

        text = app.read_response_text(io.BytesIO(b"ok\xff"), errors="replace")

        self.assertEqual(text, "ok\ufffd")

    def test_load_config_falls_back_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text("{bad json", encoding="utf-8")
            app.CONFIG_PATH = str(config_path)

            config = app.load_config()

        self.assertEqual(config["listenPort"], app.DEFAULT_CONFIG["listenPort"])
        self.assertEqual(config["home"]["displayVersion"], app.DEFAULT_CONFIG["home"]["displayVersion"])
        self.assertTrue(app.CONFIG_WARNINGS)
        self.assertIn("JSON error", app.CONFIG_WARNINGS[0])

    def test_load_config_clears_stale_warnings_after_valid_reload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            app.CONFIG_PATH = str(config_path)

            config_path.write_text("{bad json", encoding="utf-8")
            app.load_config()
            self.assertTrue(app.CONFIG_WARNINGS)

            config_path.write_text('{"listenPort": 3010}', encoding="utf-8")
            config = app.load_config()

        self.assertEqual(config["listenPort"], 3010)
        self.assertEqual(app.CONFIG_WARNINGS, [])

    def test_config_check_result_reports_warnings(self):
        app.CONFIG_WARNINGS.append("config broken")

        exit_code, lines = app.config_check_result()

        self.assertEqual(exit_code, 1)
        self.assertEqual(lines[0], "config.json has problems:")
        self.assertIn("- config broken", lines)

    def test_config_check_result_passes_without_warnings(self):
        exit_code, lines = app.config_check_result()

        self.assertEqual(exit_code, 0)
        self.assertEqual(lines, ["config.json OK"])

    def test_load_config_reports_home_maintenance_warnings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            app.CONFIG_PATH = str(config_path)
            config_path.write_text(
                json.dumps(
                    {
                        "home": {
                            "customCards": [
                                {
                                    "enabled": True,
                                    "title": "Bad Card",
                                    "text": "",
                                    "background": "red",
                                    "fontSize": "huge",
                                },
                                "not-a-card",
                                {"enabled": False, "background": "red"},
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )

            app.load_config()

        warnings = "\n".join(app.CONFIG_WARNINGS)
        self.assertIn("home.customCards[0].text is empty", warnings)
        self.assertIn("home.customCards[0].background must use #RRGGBB", warnings)
        self.assertIn("home.customCards[0].fontSize must be a number", warnings)
        self.assertIn("home.customCards[1] must be a JSON object", warnings)
        self.assertNotIn("home.customCards[2].background", warnings)
        self.assertEqual(app.CONFIG_FILE_STATE["lastReloadStatus"], "ok_with_warnings")

    def test_load_config_reports_bad_remote_urls_and_home_warnings_together(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            app.CONFIG_PATH = str(config_path)
            config_path.write_text(
                json.dumps(
                    {
                        "sscfgIndexUrl": "file:///tmp/index.json",
                        "blogUrl": "not-a-url",
                        "githubProxyPrefix": "ftp://proxy.example/",
                        "home": {"customCards": "not-a-list"},
                    }
                ),
                encoding="utf-8",
            )

            app.load_config()

        warnings = "\n".join(app.CONFIG_WARNINGS)
        self.assertIn("sscfgIndexUrl must be an http:// or https:// URL", warnings)
        self.assertIn("blogUrl must be an http:// or https:// URL", warnings)
        self.assertIn("githubProxyPrefix must be an http:// or https:// URL", warnings)
        self.assertIn("home.customCards must be a list", warnings)
        self.assertEqual(app.CONFIG_FILE_STATE["lastReloadStatus"], "ok_with_warnings")

    def test_load_config_normalizes_bad_minecraft_host_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            app.CONFIG_PATH = str(config_path)
            config_path.write_text(
                json.dumps({"minecraftHost": "", "minecraftConnectHosts": {"bad": "type"}}),
                encoding="utf-8",
            )

            config = app.load_config()

        warnings = "\n".join(app.CONFIG_WARNINGS)
        self.assertIn("minecraftHost is empty", warnings)
        self.assertIn("minecraftConnectHosts must be a string or list", warnings)
        self.assertEqual(config["minecraftHost"], app.DEFAULT_CONFIG["minecraftHost"])
        self.assertEqual(config["minecraftConnectHosts"], [app.DEFAULT_CONFIG["minecraftHost"]])

    def test_load_config_skips_empty_and_non_string_minecraft_connect_hosts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            app.CONFIG_PATH = str(config_path)
            config_path.write_text(
                json.dumps(
                    {
                        "minecraftHost": "display.example",
                        "minecraftConnectHosts": [" first.example ", "", 123, "first.example", "second.example"],
                    }
                ),
                encoding="utf-8",
            )

            config = app.load_config()

        warnings = "\n".join(app.CONFIG_WARNINGS)
        self.assertIn("minecraftConnectHosts[1] is empty", warnings)
        self.assertIn("minecraftConnectHosts[2] must be a string", warnings)
        self.assertEqual(config["minecraftConnectHosts"], ["first.example", "second.example"])

    def test_config_status_reports_restart_required_for_startup_settings(self):
        running_port = app.STARTUP_RUNTIME_CONFIG["listenPort"]
        configured_port = running_port + 1 if running_port < 65535 else running_port - 1
        app.CONFIG["listenPort"] = configured_port
        app.CONFIG["maxRequestThreads"] = app.STARTUP_RUNTIME_CONFIG["maxRequestThreads"] + 1

        payload = app.config_status_payload()

        self.assertTrue(payload["restartRequired"])
        self.assertEqual(payload["restartRequiredKeys"], ["listenPort", "maxRequestThreads"])
        details_by_key = {detail["key"]: detail for detail in payload["restartRequiredDetails"]}
        self.assertEqual(details_by_key["listenPort"]["running"], running_port)
        self.assertEqual(details_by_key["listenPort"]["configured"], configured_port)
        self.assertIn("Restart", details_by_key["maxRequestThreads"]["message"])

    def test_config_status_reports_no_restart_required_for_hot_reload_settings(self):
        app.CONFIG["home"]["displayVersion"] = "hot-only"
        app.CONFIG["refreshSeconds"] = 45

        payload = app.config_status_payload()

        self.assertFalse(payload["restartRequired"])
        self.assertEqual(payload["restartRequiredKeys"], [])
        self.assertEqual(payload["restartRequiredDetails"], [])

    def test_cache_status_payload_reports_fresh_and_refreshing_states(self):
        with app.STATUS_LOCK:
            app.STATUS_CACHE.update({"status": {"online": True}, "expires_at": time.time() + 30, "refreshing": False})

        fresh = app.cache_statuses_payload()["minecraft"]

        self.assertEqual(fresh["state"], "fresh")
        self.assertTrue(fresh["hasValue"])
        self.assertFalse(fresh["refreshing"])
        self.assertGreater(fresh["expiresInSeconds"], 0)
        self.assertTrue(fresh["expiresAt"].endswith("Z"))

        with app.STATUS_LOCK:
            app.STATUS_CACHE.update({"status": {"online": True}, "expires_at": time.time() - 1, "refreshing": True})

        stale_refreshing = app.cache_statuses_payload()["minecraft"]

        self.assertEqual(stale_refreshing["state"], "stale_refreshing")
        self.assertTrue(stale_refreshing["hasValue"])
        self.assertTrue(stale_refreshing["refreshing"])
        self.assertEqual(stale_refreshing["expiresInSeconds"], 0)

    def test_homepage_reloads_config_file_without_restart(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                app.CONFIG_PATH = str(config_path)
                config_path.write_text(
                    json.dumps(
                        {
                            "serverName": "Hot Reload Server",
                            "home": {
                                "displayVersion": "hot-2.0",
                                "playersLabel": "Live Players",
                                "customCards": [],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                app.CONFIG_FILE_STATE["signature"] = ("before-edit",)
                app.get_status = self._online_status
                app.get_github_status = self._github_status
                app.get_blog_status = self._blog_status

                response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        body = response.split(b"\r\n\r\n", 1)[1].decode("utf-8")
        self.assertIn('Title="Hot Reload Server"', body)
        self.assertIn('Text="hot-2.0"', body)
        self.assertIn('Text="Live Players"', body)
        self.assertEqual(app.CONFIG_WARNINGS, [])
        self.assertEqual(app.CONFIG_FILE_STATE["lastReloadStatus"], "ok")

    def test_homepage_reloads_warning_config_and_shows_warning_card(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                app.CONFIG_PATH = str(config_path)
                config_path.write_text(
                    json.dumps(
                        {
                            "serverName": "Warning Reload Server",
                            "home": {
                                "displayVersion": "warn-2.0",
                                "customCards": [
                                    {
                                        "enabled": True,
                                        "title": "Warning Card",
                                        "text": "still visible",
                                        "background": "red",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                app.CONFIG_FILE_STATE["signature"] = ("before-warning-edit",)
                app.get_status = self._online_status
                app.get_github_status = self._github_status
                app.get_blog_status = self._blog_status

                response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        body = response.split(b"\r\n\r\n", 1)[1].decode("utf-8")
        self.assertIn('Title="Warning Reload Server"', body)
        self.assertIn('Text="warn-2.0"', body)
        self.assertIn('Title="Warning Card"', body)
        self.assertIn('Text="still visible"', body)
        self.assertIn('Title="\u914d\u7f6e\u63d0\u9192"', body)
        self.assertIn("home.customCards[0].background must use #RRGGBB", body)
        self.assertIn('Background="#F4F8FF"', body)
        self.assertEqual(app.CONFIG_FILE_STATE["lastReloadStatus"], "ok_with_warnings")

    def test_bad_hot_reload_keeps_last_valid_homepage(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            app.CONFIG = app.merge_config(
                app.DEFAULT_CONFIG,
                {
                    "serverName": "Last Good Server",
                    "home": {"displayVersion": "last-good", "customCards": []},
                },
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                config_path = Path(temp_dir) / "config.json"
                config_path.write_text("{bad json", encoding="utf-8")
                app.CONFIG_PATH = str(config_path)
                app.CONFIG_FILE_STATE["signature"] = ("before-bad-edit",)
                app.get_status = self._online_status
                app.get_github_status = self._github_status
                app.get_blog_status = self._blog_status

                response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        body = response.split(b"\r\n\r\n", 1)[1].decode("utf-8")
        self.assertIn('Title="Last Good Server"', body)
        self.assertIn('Text="last-good"', body)
        self.assertIn('Title="\u914d\u7f6e\u63d0\u9192"', body)
        self.assertIn("JSON error", app.CONFIG_WARNINGS[0])
        self.assertEqual(app.CONFIG_FILE_STATE["lastReloadStatus"], "error_kept_last_valid")

    def test_load_config_falls_back_when_root_is_not_object(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text("[]", encoding="utf-8")
            app.CONFIG_PATH = str(config_path)

            config = app.load_config()

        self.assertEqual(config["listenPort"], app.DEFAULT_CONFIG["listenPort"])
        self.assertIn("root must be a JSON object", app.CONFIG_WARNINGS[0])

    def test_parse_http_request_normalizes_header_names(self):
        client, server = socket.socketpair()
        try:
            client.sendall(
                b"GET /api/status HTTP/1.1\r\n"
                b"Host: example.test\r\n"
                b"user-agent: unit-test\r\n"
                b"X-Forwarded-For: 203.0.113.7\r\n"
                b"\r\n"
            )

            request = app.parse_http_request(server)
        finally:
            client.close()
            server.close()

        self.assertEqual(request["path"], "/api/status")
        self.assertEqual(request["headers"]["user-agent"], "unit-test")
        self.assertEqual(request["headers"]["x-forwarded-for"], "203.0.113.7")

    def test_parse_http_request_rejects_long_request_target(self):
        client, server = socket.socketpair()
        try:
            long_path = "/" + ("x" * app.MAX_REQUEST_TARGET_LENGTH)
            client.sendall(f"GET {long_path} HTTP/1.1\r\nHost: test\r\n\r\n".encode("ascii"))

            with self.assertRaisesRegex(app.BadRequestError, "request target"):
                app.parse_http_request(server)
        finally:
            client.close()
            server.close()

    def test_parse_http_request_rejects_bad_http_version(self):
        client, server = socket.socketpair()
        try:
            client.sendall(b"GET /api/status HTTP/2.0\r\nHost: test\r\n\r\n")

            with self.assertRaisesRegex(app.BadRequestError, "HTTP version"):
                app.parse_http_request(server)
        finally:
            client.close()
            server.close()

    def test_parse_http_request_rejects_unsupported_absolute_target(self):
        client, server = socket.socketpair()
        try:
            client.sendall(b"GET ftp://example.test/Custom.xaml HTTP/1.1\r\nHost: test\r\n\r\n")

            with self.assertRaisesRegex(app.BadRequestError, "absolute request target"):
                app.parse_http_request(server)
        finally:
            client.close()
            server.close()

    def test_parse_http_request_normalizes_http_absolute_target(self):
        client, server = socket.socketpair()
        try:
            client.sendall(b"GET https://example.test/api/status?x=1 HTTP/1.1\r\nHost: test\r\n\r\n")

            request = app.parse_http_request(server)
        finally:
            client.close()
            server.close()

        self.assertEqual(request["path"], "/api/status")

    def test_bad_http_request_returns_400(self):
        client, server = socket.socketpair()
        try:
            client.settimeout(2)
            client.sendall(b"GET /api/status HTTP/2.0\r\nHost: test\r\n\r\n")
            with contextlib.redirect_stdout(io.StringIO()):
                app.handle_http_request(server, ("127.0.0.1", 12345))
            response = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                response += chunk
        finally:
            client.close()

        self.assertTrue(response.startswith(b"HTTP/1.1 400 Bad Request"))
        self.assertEqual(response.split(b"\r\n\r\n", 1)[1], b"Bad request")

    def test_rate_limit_prunes_stale_clients(self):
        app.CONFIG["rateLimitEnabled"] = True
        app.CONFIG["rateLimitWindowSeconds"] = 1
        app.CONFIG["rateLimitMaxRequests"] = 2
        app.RATE_LIMIT_HITS["stale-client"].append(time.time() - 10)

        allowed, retry_after, _message = app.check_rate_limit("active-client")

        self.assertTrue(allowed)
        self.assertEqual(retry_after, 0)
        self.assertNotIn("stale-client", app.RATE_LIMIT_HITS)
        self.assertIn("active-client", app.RATE_LIMIT_HITS)

    def test_connect_hosts_accept_string_and_prioritize_last_success(self):
        app.CONFIG["minecraftConnectHosts"] = "single.example"
        self.assertEqual(app.get_connect_hosts("display.example"), ["single.example"])

        app.CONFIG["minecraftConnectHosts"] = ["first.example", "second.example"]
        app.LAST_SUCCESSFUL_CONNECT_HOST["host"] = "second.example"
        self.assertEqual(app.get_connect_hosts("display.example"), ["second.example", "first.example"])

    def test_ping_remembers_successful_connect_host(self):
        original_ping_one_host = app.ping_one_host
        calls = []
        try:
            app.CONFIG["minecraftHost"] = "display.example"
            app.CONFIG["minecraftConnectHosts"] = ["first.example", "second.example"]
            app.LAST_SUCCESSFUL_CONNECT_HOST["host"] = "second.example"

            def fake_ping_one_host(connect_host, display_host, port):
                calls.append((connect_host, display_host, port))
                return {"online": True, "connectHost": connect_host}

            app.ping_one_host = fake_ping_one_host
            status = app.ping_minecraft_server()
        finally:
            app.ping_one_host = original_ping_one_host

        self.assertEqual(status["connectHost"], "second.example")
        self.assertEqual(calls, [("second.example", "display.example", 25565)])

    def test_ping_one_host_uses_configured_timeout(self):
        original_create_connection = app.socket.create_connection
        original_make_handshake = app.make_handshake
        original_make_packet = app.make_packet
        original_read_varint = app.read_varint
        original_recv_exact = app.recv_exact
        timeouts = []
        try:
            app.CONFIG["minecraftPingTimeoutSeconds"] = 2
            app.make_handshake = lambda _host, _port: b"handshake"
            app.make_packet = lambda _packet_id, _payload=b"": b"packet"
            app.read_varint = lambda _sock: 0
            app.recv_exact = lambda _sock, _size: b'{"description":"ok","players":{"online":1,"max":20},"version":{"name":"1.20","protocol":763}}'

            class FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def settimeout(self, timeout):
                    timeouts.append(("settimeout", timeout))

                def sendall(self, _data):
                    pass

            def fake_create_connection(_address, timeout):
                timeouts.append(("create_connection", timeout))
                return FakeSocket()

            app.socket.create_connection = fake_create_connection
            status = app.ping_one_host("connect.example", "display.example", 25565)
        finally:
            app.socket.create_connection = original_create_connection
            app.make_handshake = original_make_handshake
            app.make_packet = original_make_packet
            app.read_varint = original_read_varint
            app.recv_exact = original_recv_exact

        self.assertTrue(status["online"])
        self.assertEqual(timeouts, [("create_connection", 2), ("settimeout", 2)])

    def test_ping_one_host_rejects_oversized_status_payload(self):
        original_create_connection = app.socket.create_connection
        original_make_handshake = app.make_handshake
        original_make_packet = app.make_packet
        original_read_varint = app.read_varint
        original_recv_exact = app.recv_exact
        try:
            app.make_handshake = lambda _host, _port: b"handshake"
            app.make_packet = lambda _packet_id, _payload=b"": b"packet"
            varints = iter([0, 0, app.MAX_MINECRAFT_STATUS_BYTES + 1])
            app.read_varint = lambda _sock: next(varints)
            app.recv_exact = lambda _sock, _size: self.fail("oversized payload must not be read")

            class FakeSocket:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, traceback):
                    return False

                def settimeout(self, _timeout):
                    pass

                def sendall(self, _data):
                    pass

            app.socket.create_connection = lambda _address, timeout: FakeSocket()

            with self.assertRaisesRegex(ValueError, "minecraft status response exceeds"):
                app.ping_one_host("connect.example", "display.example", 25565)
        finally:
            app.socket.create_connection = original_create_connection
            app.make_handshake = original_make_handshake
            app.make_packet = original_make_packet
            app.read_varint = original_read_varint
            app.recv_exact = original_recv_exact

    def test_latency_history_is_bounded_and_sanitizes_bad_latency(self):
        for latency_ms in range(app.MAX_LATENCY_SAMPLES + 25):
            app.record_latency_sample({"online": True, "latencyMs": latency_ms})
        app.record_latency_sample({"online": True, "latencyMs": float("inf")})

        self.assertEqual(len(app.LATENCY_HISTORY), app.MAX_LATENCY_SAMPLES)
        self.assertEqual(app.LATENCY_HISTORY[-1]["latency"], 0)

        status = {"online": True, "latencyMs": 42}
        app.enrich_latency_metrics(status)
        self.assertEqual(status["latencySampleCount"], app.MAX_LATENCY_SAMPLES)
        self.assertGreaterEqual(status["latencyStability"], 0)
        self.assertLessEqual(status["latencyStability"], 100)

    def test_get_status_returns_stale_cache_while_refreshing_once(self):
        original_ping_minecraft_server = app.ping_minecraft_server
        refresh_started = threading.Event()
        allow_refresh = threading.Event()
        calls = []
        try:
            app.CONFIG["refreshSeconds"] = 30
            app.STATUS_CACHE.update(
                {
                    "status": {
                        "online": True,
                        "latencyMs": 100,
                        "latencyAverageMs": 100,
                        "latencyStability": 90,
                        "playersOnline": 3,
                    },
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )

            def fake_ping_minecraft_server():
                calls.append("refresh")
                refresh_started.set()
                self.assertTrue(allow_refresh.wait(2))
                return {
                    "online": True,
                    "latencyMs": 20,
                    "playersOnline": 4,
                    "playersMax": 20,
                }

            app.ping_minecraft_server = fake_ping_minecraft_server

            first = app.get_status()
            second = app.get_status()
            self.assertTrue(refresh_started.wait(2))
            allow_refresh.set()
            self._wait_for_status_refresh()
        finally:
            allow_refresh.set()
            app.ping_minecraft_server = original_ping_minecraft_server

        self.assertEqual(first["playersOnline"], 3)
        self.assertEqual(first["cacheState"], "stale_refreshing")
        self.assertEqual(second["playersOnline"], 3)
        self.assertEqual(calls, ["refresh"])
        self.assertEqual(app.STATUS_CACHE["status"]["playersOnline"], 4)

    def test_get_github_status_returns_stale_cache_while_refreshing_once(self):
        original_refresh_github_status = app.refresh_github_status
        refresh_started = threading.Event()
        allow_refresh = threading.Event()
        calls = []
        try:
            app.GITHUB_CACHE.update(
                {
                    "status": {"online": True, "packCount": 1},
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )

            def fake_refresh_github_status():
                calls.append("refresh")
                refresh_started.set()
                self.assertTrue(allow_refresh.wait(2))
                status = {"online": True, "packCount": 2}
                with app.GITHUB_LOCK:
                    app.GITHUB_CACHE["status"] = status
                    app.GITHUB_CACHE["expires_at"] = time.time() + 60
                    app.GITHUB_CACHE["refreshing"] = False
                return status

            app.refresh_github_status = fake_refresh_github_status

            first = app.get_github_status()
            second = app.get_github_status()
            self.assertTrue(refresh_started.wait(2))
            allow_refresh.set()
            self._wait_for_cache_refresh(app.GITHUB_CACHE)
        finally:
            allow_refresh.set()
            app.refresh_github_status = original_refresh_github_status

        self.assertEqual(first["packCount"], 1)
        self.assertEqual(first["cacheState"], "stale_refreshing")
        self.assertEqual(second["packCount"], 1)
        self.assertEqual(calls, ["refresh"])
        self.assertEqual(app.GITHUB_CACHE["status"]["packCount"], 2)

    def test_get_blog_status_returns_stale_cache_while_refreshing_once(self):
        original_refresh_blog_status = app.refresh_blog_status
        refresh_started = threading.Event()
        allow_refresh = threading.Event()
        calls = []
        try:
            app.BLOG_CACHE.update(
                {
                    "status": {"online": True, "title": "old"},
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )

            def fake_refresh_blog_status():
                calls.append("refresh")
                refresh_started.set()
                self.assertTrue(allow_refresh.wait(2))
                status = {"online": True, "title": "new"}
                with app.BLOG_LOCK:
                    app.BLOG_CACHE["status"] = status
                    app.BLOG_CACHE["expires_at"] = time.time() + 60
                    app.BLOG_CACHE["refreshing"] = False
                return status

            app.refresh_blog_status = fake_refresh_blog_status

            first = app.get_blog_status()
            second = app.get_blog_status()
            self.assertTrue(refresh_started.wait(2))
            allow_refresh.set()
            self._wait_for_cache_refresh(app.BLOG_CACHE)
        finally:
            allow_refresh.set()
            app.refresh_blog_status = original_refresh_blog_status

        self.assertEqual(first["title"], "old")
        self.assertEqual(first["cacheState"], "stale_refreshing")
        self.assertEqual(second["title"], "old")
        self.assertEqual(calls, ["refresh"])
        self.assertEqual(app.BLOG_CACHE["status"]["title"], "new")

    def test_background_refresh_start_failure_clears_refreshing_flags(self):
        original_thread = app.threading.Thread
        try:
            class FailingThread:
                def __init__(self, *args, **kwargs):
                    pass

                def start(self):
                    raise RuntimeError("thread start failed")

            app.threading.Thread = FailingThread
            app.STATUS_CACHE.update(
                {
                    "status": {"online": True, "playersOnline": 1},
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )
            app.GITHUB_CACHE.update(
                {
                    "status": {"online": True, "packCount": 1},
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )
            app.BLOG_CACHE.update(
                {
                    "status": {"online": True, "title": "old"},
                    "expires_at": time.time() - 1,
                    "refreshing": False,
                }
            )

            minecraft_status = app.get_status()
            github_status = app.get_github_status()
            blog_status = app.get_blog_status()
        finally:
            app.threading.Thread = original_thread

        self.assertEqual(minecraft_status["cacheState"], "stale_refreshing")
        self.assertEqual(github_status["cacheState"], "stale_refreshing")
        self.assertEqual(blog_status["cacheState"], "stale_refreshing")
        self.assertFalse(app.STATUS_CACHE["refreshing"])
        self.assertFalse(app.GITHUB_CACHE["refreshing"])
        self.assertFalse(app.BLOG_CACHE["refreshing"])

    def test_safe_monitor_refresh_logs_and_clears_refreshing_on_unexpected_error(self):
        logged_lines = []
        app.append_runtime_log = logged_lines.append
        app.GITHUB_CACHE["refreshing"] = True

        result = app.safe_monitor_refresh(
            lambda: (_ for _ in ()).throw(RuntimeError("monitor failed")),
            app.GITHUB_CACHE,
            app.GITHUB_LOCK,
            "GITHUB_MONITOR",
        )

        self.assertIsNone(result)
        self.assertFalse(app.GITHUB_CACHE["refreshing"])
        self.assertTrue(any("monitor failed" in line for line in logged_lines))
        self.assertTrue(any("[GITHUB_MONITOR]" in line for line in logged_lines))

    def test_monitor_interval_seconds_falls_back_after_config_error(self):
        original_config_int = app.config_int
        logged_lines = []
        try:
            app.append_runtime_log = logged_lines.append
            app.config_int = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("bad interval"))

            interval = app.monitor_interval_seconds("blogRefreshSeconds", 900, 60, "BLOG_MONITOR")
        finally:
            app.config_int = original_config_int

        self.assertEqual(interval, 900)
        self.assertTrue(any("bad interval" in line for line in logged_lines))

    def test_home_config_merges_partial_overrides(self):
        app.CONFIG["home"] = {"displayVersion": "2.0-test"}

        home_config = app.get_home_config()

        self.assertEqual(home_config["displayVersion"], "2.0-test")
        self.assertEqual(home_config["refreshButtonText"], app.DEFAULT_CONFIG["home"]["refreshButtonText"])
        self.assertEqual(home_config["customCards"], app.DEFAULT_CONFIG["home"]["customCards"])

    def test_home_xaml_uses_configurable_content_and_cards(self):
        app.CONFIG["home"] = app.merge_config(
            app.DEFAULT_CONFIG["home"],
            {
                "displayVersion": "2.0-maint",
                "playersLabel": "Players Now",
                "latestUpdateTitle": "Latest News",
                "refreshButtonText": "Reload",
                "sscfgCardTitle": "Config Packs",
                "showAppVersion": False,
                "customCards": [
                    {
                        "enabled": True,
                        "title": "Notice",
                        "text": "Read & check <rules>",
                        "background": "#ABCDEF",
                        "fontSize": 14,
                        "bold": False,
                    },
                    {
                        "enabled": False,
                        "title": "Hidden Card",
                        "text": "hidden",
                    },
                ],
            },
        )

        xaml = app.make_home_xaml(self._online_status(), self._github_status(), self._blog_status())

        self.assertIn('Text="2.0-maint"', xaml)
        self.assertIn('Text="Players Now"', xaml)
        self.assertIn('Text="Latest News"', xaml)
        self.assertIn('Text="Reload"', xaml)
        self.assertIn('Title="Config Packs"', xaml)
        self.assertIn('Title="Notice"', xaml)
        self.assertIn('Text="Read &amp; check &lt;rules&gt;"', xaml)
        self.assertIn('Background="#ABCDEF"', xaml)
        self.assertIn('FontWeight="Normal"', xaml)
        self.assertNotIn("Hidden Card", xaml)
        self.assertNotIn(f'v{app.APP_VERSION}', xaml)

    def test_custom_card_rejects_bad_color(self):
        card_xaml = app.make_custom_card_xaml(
            {"enabled": True, "title": "Bad Color", "text": "ok", "background": "red"}
        )

        self.assertIn('Background="#F4F8FF"', card_xaml)

    def test_config_warning_card_is_rendered_on_homepage(self):
        app.CONFIG_WARNINGS.append("config broken")

        xaml = app.make_home_xaml(self._online_status(), self._github_status(), self._blog_status())

        self.assertIn('Title="\u914d\u7f6e\u63d0\u9192"', xaml)
        self.assertIn('Text="config broken"', xaml)
        self.assertIn('Background="#FFF4CE"', xaml)

    def test_current_config_warnings_returns_copy(self):
        app.CONFIG_WARNINGS[:] = ["config broken"]

        warnings = app.current_config_warnings()
        warnings.append("caller mutation")

        self.assertEqual(app.CONFIG_WARNINGS, ["config broken"])

    def test_home_xaml_uses_request_config_snapshot(self):
        snapshot = app.merge_config(
            app.DEFAULT_CONFIG,
            {
                "serverName": "Snapshot Server",
                "minecraftHost": "snapshot.example",
                "minecraftPort": 25566,
                "home": {"displayVersion": "snapshot-version", "customCards": []},
            },
        )
        app.CONFIG = app.merge_config(
            app.DEFAULT_CONFIG,
            {
                "serverName": "Global Server",
                "minecraftHost": "global.example",
                "minecraftPort": 25567,
                "home": {"displayVersion": "global-version", "customCards": []},
            },
        )
        config_token = app.REQUEST_CONFIG.set(snapshot)
        warnings_token = app.REQUEST_CONFIG_WARNINGS.set(["snapshot warning"])
        try:
            xaml = app.make_home_xaml(self._online_status(), self._github_status(), self._blog_status())
        finally:
            app.REQUEST_CONFIG_WARNINGS.reset(warnings_token)
            app.REQUEST_CONFIG.reset(config_token)

        self.assertIn('Title="Snapshot Server"', xaml)
        self.assertIn('Text="snapshot.example:25566"', xaml)
        self.assertIn('Text="snapshot-version"', xaml)
        self.assertIn('Text="snapshot warning"', xaml)
        self.assertNotIn("Global Server", xaml)
        self.assertNotIn("global-version", xaml)

    def test_http_request_keeps_config_snapshot_after_mid_request_reload(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            app.CONFIG = app.merge_config(
                app.DEFAULT_CONFIG,
                {
                    "serverName": "Before Reload",
                    "minecraftHost": "before.example",
                    "home": {"displayVersion": "before-version", "customCards": []},
                },
            )
            app.CONFIG_WARNINGS[:] = ["before warning"]

            def fake_get_status():
                app.CONFIG = app.merge_config(
                    app.DEFAULT_CONFIG,
                    {
                        "serverName": "After Reload",
                        "minecraftHost": "after.example",
                        "home": {"displayVersion": "after-version", "customCards": []},
                    },
                )
                app.CONFIG_WARNINGS[:] = ["after warning"]
                return self._online_status()

            app.get_status = fake_get_status
            app.get_github_status = self._github_status
            app.get_blog_status = self._blog_status

            response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        body = response.split(b"\r\n\r\n", 1)[1].decode("utf-8")
        self.assertIn('Title="Before Reload"', body)
        self.assertIn('Text="before.example:25565"', body)
        self.assertIn('Text="before-version"', body)
        self.assertIn('Text="before warning"', body)
        self.assertNotIn("After Reload", body)
        self.assertNotIn("after-version", body)
        self.assertIsNone(app.REQUEST_CONFIG.get())
        self.assertIsNone(app.REQUEST_CONFIG_WARNINGS.get())

    def test_custom_xaml_is_rate_limited_when_enabled(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            app.CONFIG["rateLimitEnabled"] = True
            app.CONFIG["rateLimitWindowSeconds"] = 60
            app.CONFIG["rateLimitMaxRequests"] = 1
            app.CONFIG["requestTimeoutSeconds"] = 1
            app.get_status = lambda: {
                "online": True,
                "latencyMs": 42,
                "latencyAverageMs": 42,
                "latencyStability": 100,
                "playersOnline": 1,
                "playersMax": 20,
                "motd": "ok",
            }
            app.get_github_status = lambda: {
                "online": True,
                "name": "SsCfg",
                "packCount": 1,
                "latestPack": {
                    "name": "Pack",
                    "version": "1.0",
                    "date": "2026-06-06",
                    "author": "unit",
                },
            }
            app.get_blog_status = lambda: {
                "online": True,
                "date": "2026-06-06",
                "title": "\u66f4\u65b0\u65e5\u5fd7",
                "summary": "ok",
            }

            first_response = self._request_path("/Custom.xaml")
            second_response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        self.assertTrue(first_response.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(second_response.startswith(b"HTTP/1.1 429 Too Many Requests"))

    def test_api_status_includes_config_warnings(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        try:
            app.CONFIG["rateLimitEnabled"] = False
            app.CONFIG["requestTimeoutSeconds"] = 1
            app.CONFIG_WARNINGS.append("config broken")
            app.STATUS_CACHE.update({"status": self._online_status(), "expires_at": time.time() + 30, "refreshing": False})
            app.GITHUB_CACHE.update({"status": self._github_status(), "expires_at": time.time() - 1, "refreshing": True})
            app.BLOG_CACHE.update({"status": self._blog_status(), "expires_at": 0, "refreshing": False})
            app.get_status = self._online_status
            app.get_github_status = self._github_status
            app.get_blog_status = self._blog_status

            response = self._request_path("/api/status")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        headers, body_bytes = response.split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Type: application/json; charset=utf-8", headers)
        body = body_bytes.decode("utf-8")
        payload = json.loads(body)
        self.assertEqual(payload["configWarnings"], ["config broken"])
        self.assertEqual(payload["cache"]["minecraft"]["state"], "fresh")
        self.assertEqual(payload["cache"]["github"]["state"], "stale_refreshing")
        self.assertEqual(payload["cache"]["blog"]["state"], "stale")
        self.assertIn("expiresInSeconds", payload["cache"]["minecraft"])

    def test_healthz_is_not_request_logged_by_default(self):
        logged_lines = []
        app.append_runtime_log = logged_lines.append
        app.CONFIG["requestLoggingEnabled"] = True
        app.CONFIG["logHealthChecks"] = False

        response = self._request_path("/healthz")

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(logged_lines, [])

    def test_head_healthz_returns_headers_without_body(self):
        response = self._request_path("/healthz", method="HEAD")

        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"HTTP/1.1 200 OK"))
        self.assertIn(b"Content-Length: 2", headers)
        self.assertEqual(body, b"")

    def test_post_custom_xaml_is_rejected_without_rendering_homepage(self):
        original_get_status = app.get_status
        try:
            app.get_status = lambda: self.fail("POST must not render homepage")

            response = self._request_path("/Custom.xaml", method="POST")
        finally:
            app.get_status = original_get_status

        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"HTTP/1.1 405 Method Not Allowed"))
        self.assertIn(b"Allow: GET, HEAD", headers)
        self.assertEqual(body, b"Method not allowed")

    def test_unexpected_error_returns_generic_500_without_traceback(self):
        original_get_status = app.get_status
        logged_lines = []
        try:
            app.append_runtime_log = logged_lines.append
            app.CONFIG["requestTimeoutSeconds"] = 1
            app.get_status = lambda: (_ for _ in ()).throw(RuntimeError("secret failure"))

            response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status

        headers, body = response.split(b"\r\n\r\n", 1)
        self.assertTrue(headers.startswith(b"HTTP/1.1 500 Internal Server Error"))
        self.assertEqual(body, b"Internal server error")
        self.assertNotIn(b"secret failure", body)
        self.assertTrue(any("secret failure" in line for line in logged_lines))

    def test_request_logging_can_be_disabled(self):
        original_get_status = app.get_status
        original_get_github_status = app.get_github_status
        original_get_blog_status = app.get_blog_status
        logged_lines = []
        try:
            app.append_runtime_log = logged_lines.append
            app.CONFIG["requestLoggingEnabled"] = False
            app.CONFIG["requestTimeoutSeconds"] = 1
            app.CONFIG["home"]["customCards"] = []
            app.get_status = self._online_status
            app.get_github_status = self._github_status
            app.get_blog_status = self._blog_status

            response = self._request_path("/Custom.xaml")
        finally:
            app.get_status = original_get_status
            app.get_github_status = original_get_github_status
            app.get_blog_status = original_get_blog_status

        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(logged_lines, [])

    def test_request_logging_sanitizes_control_chars_and_long_fields(self):
        client, server = socket.socketpair()
        logged_lines = []
        try:
            app.append_runtime_log = logged_lines.append
            app.CONFIG["requestLoggingEnabled"] = True
            app.CONFIG["logHealthChecks"] = True
            app.CONFIG["requestTimeoutSeconds"] = 1
            path = "/missing?x=" + ("a" * 400)
            user_agent = "unit\x01test" + ("b" * 400)
            request = (
                f"GET {path} HTTP/1.1\r\n"
                "Host: test\r\n"
                f"User-Agent: {user_agent}\r\n"
                "\r\n"
            ).encode("iso-8859-1")

            client.sendall(request)
            with contextlib.redirect_stdout(io.StringIO()):
                app.handle_http_request(server, ("127.0.0.1", 12345))
            response = client.recv(8192)
        finally:
            client.close()

        self.assertTrue(response.startswith(b"HTTP/1.1 404 Not Found"))
        self.assertEqual(len(logged_lines), 1)
        log_line = logged_lines[0]
        self.assertIn("unit?test", log_line)
        self.assertNotIn("\x01", log_line)
        self.assertIn("...", log_line)
        self.assertLess(len(log_line), 620)

    def test_runtime_log_rotates_when_it_reaches_limit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime_log_path = Path(temp_dir) / "server.runtime.log"
            runtime_log_path.write_text("old-log-content", encoding="utf-8")
            app.RUNTIME_LOG_PATH = str(runtime_log_path)
            app.CONFIG["runtimeLogMaxBytes"] = 4
            app.append_runtime_log = self.original_log

            app.append_runtime_log("new line")

            backup_path = Path(f"{runtime_log_path}.1")
            self.assertEqual(backup_path.read_text(encoding="utf-8"), "old-log-content")
            self.assertIn("new line", runtime_log_path.read_text(encoding="utf-8"))

    def _online_status(self):
        return {
            "online": True,
            "latencyMs": 42,
            "latencyAverageMs": 42,
            "latencyStability": 100,
            "playersOnline": 1,
            "playersMax": 20,
            "motd": "ok",
        }

    def _github_status(self):
        return {
            "online": True,
            "name": "SsCfg",
            "packCount": 1,
            "latestPack": {
                "name": "Pack",
                "version": "1.0",
                "date": "2026-06-06",
                "author": "unit",
            },
        }

    def _blog_status(self):
        return {
            "online": True,
            "date": "2026-06-06",
            "title": "\u66f4\u65b0\u65e5\u5fd7",
            "summary": "ok",
        }

    def _request_path(self, path, method="GET"):
        client, server = socket.socketpair()
        try:
            client.settimeout(2)
            client.sendall(f"{method} {path} HTTP/1.1\r\nHost: test\r\n\r\n".encode("ascii"))
            with contextlib.redirect_stdout(io.StringIO()):
                app.handle_http_request(server, ("127.0.0.1", 12345))
            return client.recv(8192)
        finally:
            client.close()

    def _wait_for_status_refresh(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            if not app.STATUS_CACHE["refreshing"]:
                return
            time.sleep(0.01)
        self.fail("status refresh did not finish")

    def _wait_for_cache_refresh(self, cache):
        deadline = time.time() + 2
        while time.time() < deadline:
            if not cache["refreshing"]:
                return
            time.sleep(0.01)
        self.fail("cache refresh did not finish")


if __name__ == "__main__":
    unittest.main()

import json
import html as html_lib
import math
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
import traceback
from collections import defaultdict, deque
from contextvars import ContextVar
from copy import deepcopy
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_CONFIG = {
    "listenHost": "0.0.0.0",
    "listenPort": 3000,
    "serverName": "\u6211\u7684\u4e16\u754c\u5e03\u5409\u5c9b\u670d\u52a1\u5668",
    "minecraftHost": "play.bjd-mc.com",
    "minecraftConnectHosts": [
        "play.bjd-mc.com",
        "115.236.116.140",
        "115.236.126.67",
        "106.2.37.76",
        "106.2.37.13",
        "115.236.116.141",
    ],
    "minecraftPort": 25565,
    "minecraftPingTimeoutSeconds": 5,
    "joinHint": "\u628a play.bjd-mc.com \u52a0\u5165\u591a\u4eba\u6e38\u620f\u5373\u53ef\u8fdb\u5165\u5e03\u5409\u5c9b\u3002",
    "refreshSeconds": 30,
    "githubName": "SsCfg",
    "sscfgIndexUrl": "https://raw.githubusercontent.com/bedkillerspacex-boop/SsCfg/master/index.json",
    "githubProxyPrefix": "",
    "sscfgRefreshSeconds": 900,
    "blogUrl": "https://mcbjd.net/blog/",
    "blogRefreshSeconds": 900,
    "home": {
        "displayVersion": "1.20.5-6",
        "playersLabel": "\u5728\u7ebf\u4eba\u6570",
        "latencyLabel": "\u5e03\u5409\u5c9b\u5ef6\u8fdf\u7a33\u5b9a\u6027",
        "versionLabel": "\u534f\u8bae\u7248\u672c",
        "latestUpdateTitle": "\u5e03\u5409\u5c9b\u6700\u8fd1\u66f4\u65b0",
        "refreshButtonText": "\u5237\u65b0",
        "refreshButtonTooltip": "\u91cd\u65b0\u52a0\u8f7d\u4e3b\u9875\u5e76\u91cd\u65b0\u67e5\u8be2 Minecraft MOTD \u548c GitHub \u72b6\u6001\u3002",
        "sscfgCardTitle": "SsCfg",
        "showAppVersion": True,
        "customCards": [
            {
                "enabled": True,
                "title": "\u5e7f\u544a",
                "text": "Kam\u5ba2\u6237\u7aef\uff01\u5e03\u5409\u5c9b\u6700\u5f3a\u516c\u76ca\u5ba2\u6237\u7aef \u8fdb\u7fa41106439778",
                "background": "#F4F8FF",
            }
        ],
    },
    "rateLimitEnabled": False,
    "rateLimitWindowSeconds": 5,
    "rateLimitMaxRequests": 3,
    "requestTimeoutSeconds": 10,
    "maxRequestThreads": 64,
    "requestLoggingEnabled": True,
    "logHealthChecks": False,
    "runtimeLogMaxBytes": 1048576,
    "remoteFetchMaxBytes": 1048576,
    "trustProxyHeaders": False,
    "verifyHttps": False,
}
APP_VERSION = "1.0.0"

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
RUNTIME_LOG_PATH = os.path.join(os.path.dirname(__file__), "server.runtime.log")
CONFIG_LOCK = threading.RLock()
CONFIG_RELOAD_LOCK = threading.Lock()
CONFIG_WARNINGS = []
CONFIG_FILE_STATE = {
    "signature": None,
    "lastCheckedAt": "",
    "lastLoadedAt": "",
    "lastReloadStatus": "not_loaded",
    "reloadCount": 0,
}
STATUS_CACHE = {"status": None, "expires_at": 0, "refreshing": False}
STATUS_LOCK = threading.Lock()
LAST_SUCCESSFUL_CONNECT_HOST = {"host": ""}
LATENCY_HISTORY = deque()
LATENCY_LOCK = threading.Lock()
GITHUB_CACHE = {"status": None, "expires_at": 0, "refreshing": False}
GITHUB_LOCK = threading.Lock()
GITHUB_STOP = threading.Event()
BLOG_CACHE = {"status": None, "expires_at": 0, "refreshing": False}
BLOG_LOCK = threading.Lock()
BLOG_STOP = threading.Event()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_HITS = defaultdict(deque)
RATE_LIMIT_NEXT_CLEANUP_AT = 0
RUNTIME_LOG_LOCK = threading.Lock()
HTTP_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
MAX_HTTP_HEADER_BYTES = 16384
MAX_REQUEST_TARGET_LENGTH = 2048
MAX_LOG_FIELD_LENGTH = 240
MAX_MINECRAFT_STATUS_BYTES = 262144
MAX_LATENCY_SAMPLES = 512


def current_utc_timestamp():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def utc_timestamp_from_epoch(epoch_seconds):
    if not epoch_seconds:
        return ""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))
    except (OverflowError, OSError, ValueError):
        return ""


def config_file_signature():
    try:
        stat_result = os.stat(CONFIG_PATH)
        return ("file", stat_result.st_mtime_ns, stat_result.st_size)
    except FileNotFoundError:
        return ("missing",)
    except OSError as error:
        return ("error", str(error))


def is_xaml_color_value(value):
    return bool(re.fullmatch(r"#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?", str(value or "").strip()))


def is_false_like(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, str):
        return value.strip().lower() in ("0", "false", "no", "n", "off")
    return False


def is_int_like(value):
    try:
        int(value)
        return True
    except (TypeError, ValueError, OverflowError):
        return False


def is_http_url_value(value):
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate_http_url(warnings, config, key, allow_empty=False):
    value = str(config.get(key, "") or "").strip()
    if not value and allow_empty:
        return
    if not is_http_url_value(value):
        warnings.append(f"{key} must be an http:// or https:// URL; remote status will show an error")


def normalized_host_list(value):
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []

    hosts = []
    for host in values:
        if not isinstance(host, str):
            continue
        host_text = host.strip()
        if host_text and host_text not in hosts:
            hosts.append(host_text)
    return hosts


def normalize_minecraft_config(config):
    minecraft_host = str(config.get("minecraftHost") or "").strip() or DEFAULT_CONFIG["minecraftHost"]
    config["minecraftHost"] = minecraft_host

    hosts = normalized_host_list(config.get("minecraftConnectHosts"))
    if not hosts:
        hosts = [minecraft_host]
    config["minecraftConnectHosts"] = hosts


def validate_minecraft_config(warnings, config):
    if not str(config.get("minecraftHost") or "").strip():
        warnings.append("minecraftHost is empty; default minecraft host will be used")

    configured_hosts = config.get("minecraftConnectHosts")
    if configured_hosts is None:
        warnings.append("minecraftConnectHosts is missing; minecraftHost will be used")
    elif not isinstance(configured_hosts, (str, list)):
        warnings.append("minecraftConnectHosts must be a string or list; minecraftHost will be used")
    else:
        if isinstance(configured_hosts, list):
            for index, host in enumerate(configured_hosts):
                if not isinstance(host, str):
                    warnings.append(f"minecraftConnectHosts[{index}] must be a string; entry will be skipped")
                elif not host.strip():
                    warnings.append(f"minecraftConnectHosts[{index}] is empty; entry will be skipped")
        if not normalized_host_list(configured_hosts):
            warnings.append("minecraftConnectHosts has no usable hosts; minecraftHost will be used")


def validate_config(config):
    warnings = []
    validate_minecraft_config(warnings, config)
    validate_http_url(warnings, config, "sscfgIndexUrl")
    validate_http_url(warnings, config, "blogUrl")
    validate_http_url(warnings, config, "githubProxyPrefix", allow_empty=True)

    home = config.get("home")
    if not isinstance(home, dict):
        warnings.append("home must be a JSON object; using built-in homepage defaults")
    else:
        custom_cards = home.get("customCards", [])
        if not isinstance(custom_cards, list):
            warnings.append("home.customCards must be a list; custom cards will be hidden")
        else:
            for index, card in enumerate(custom_cards):
                label = f"home.customCards[{index}]"
                if not isinstance(card, dict):
                    warnings.append(f"{label} must be a JSON object; card will be skipped")
                    continue
                if is_false_like(card.get("enabled", True)):
                    continue
                if not str(card.get("text", "")).strip():
                    warnings.append(f"{label}.text is empty; card will be skipped")
                if "background" in card and not is_xaml_color_value(card.get("background")):
                    warnings.append(f"{label}.background must use #RRGGBB; fallback color will be used")
                if "fontSize" in card:
                    font_size = card.get("fontSize")
                    if not is_int_like(font_size):
                        warnings.append(f"{label}.fontSize must be a number from 10 to 24; fallback size will be used")
                    elif not 10 <= int(font_size) <= 24:
                        warnings.append(f"{label}.fontSize should be from 10 to 24; it will be clamped")
    return warnings


def read_config_file(fallback_action):
    if not os.path.exists(CONFIG_PATH):
        return None, [f"config.json not found; {fallback_action}"], config_file_signature()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            user_config = json.load(file)
    except OSError as error:
        return None, [f"config.json read failed: {error}; {fallback_action}"], config_file_signature()
    except json.JSONDecodeError as error:
        return (
            None,
            [f"config.json JSON error at line {error.lineno}, column {error.colno}: {error.msg}; {fallback_action}"],
            config_file_signature(),
        )

    if not isinstance(user_config, dict):
        return None, [f"config.json root must be a JSON object; {fallback_action}"], config_file_signature()

    config = merge_config(DEFAULT_CONFIG, user_config)
    warnings = validate_config(config)
    normalize_minecraft_config(config)
    return config, warnings, config_file_signature()


def load_config():
    config, warnings, signature = read_config_file("using built-in defaults")
    CONFIG_WARNINGS[:] = warnings
    CONFIG_FILE_STATE.update(
        {
            "signature": signature,
            "lastCheckedAt": current_utc_timestamp(),
            "lastLoadedAt": current_utc_timestamp(),
            "lastReloadStatus": (
                "ok_with_warnings" if config is not None and warnings else ("ok" if config is not None else "fallback")
            ),
            "reloadCount": 0,
        }
    )
    return config if config is not None else deepcopy(DEFAULT_CONFIG)


def merge_config(defaults, overrides):
    config = deepcopy(defaults)
    if not isinstance(overrides, dict):
        return config
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key] = merge_config(config[key], value)
        else:
            config[key] = value
    return config


CONFIG = load_config()
UNVERIFIED_SSL_CONTEXT = ssl._create_unverified_context()
REQUEST_CONFIG = ContextVar("REQUEST_CONFIG", default=None)
REQUEST_CONFIG_WARNINGS = ContextVar("REQUEST_CONFIG_WARNINGS", default=None)


def current_config():
    request_config = REQUEST_CONFIG.get()
    return request_config if request_config is not None else CONFIG


def current_config_warnings():
    request_warnings = REQUEST_CONFIG_WARNINGS.get()
    if request_warnings is not None:
        return list(request_warnings)
    with CONFIG_LOCK:
        return list(CONFIG_WARNINGS)


def get_request_config_snapshot():
    with CONFIG_LOCK:
        return deepcopy(CONFIG), list(CONFIG_WARNINGS)


def reload_config_if_needed(force=False):
    with CONFIG_RELOAD_LOCK:
        signature = config_file_signature()
        with CONFIG_LOCK:
            if not force and signature == CONFIG_FILE_STATE["signature"]:
                CONFIG_FILE_STATE["lastCheckedAt"] = current_utc_timestamp()
                return False

        new_config, warnings, loaded_signature = read_config_file("keeping last valid config")
        with CONFIG_LOCK:
            CONFIG_FILE_STATE["signature"] = loaded_signature
            CONFIG_FILE_STATE["lastCheckedAt"] = current_utc_timestamp()
            if new_config is None:
                CONFIG_WARNINGS[:] = warnings
                CONFIG_FILE_STATE["lastReloadStatus"] = "error_kept_last_valid"
                for warning in warnings:
                    append_runtime_log(f"[CONFIG] {warning}")
                return False

            global CONFIG
            old_config = CONFIG
            CONFIG = new_config
            CONFIG_WARNINGS[:] = warnings
            CONFIG_FILE_STATE["lastLoadedAt"] = current_utc_timestamp()
            CONFIG_FILE_STATE["lastReloadStatus"] = "ok_with_warnings" if warnings else "ok"
            CONFIG_FILE_STATE["reloadCount"] = int(CONFIG_FILE_STATE.get("reloadCount", 0)) + 1

    expire_caches_after_config_reload(old_config, new_config)
    for warning in warnings:
        append_runtime_log(f"[CONFIG] {warning}")
    append_runtime_log("[CONFIG] config.json reloaded")
    return True


def expire_caches_after_config_reload(old_config, new_config):
    if old_config.get("minecraftHost") != new_config.get("minecraftHost") or old_config.get("minecraftPort") != new_config.get("minecraftPort"):
        with STATUS_LOCK:
            STATUS_CACHE["expires_at"] = 0
            LAST_SUCCESSFUL_CONNECT_HOST["host"] = ""

    github_keys = ("sscfgIndexUrl", "githubProxyPrefix", "githubName", "sscfgRefreshSeconds")
    if any(old_config.get(key) != new_config.get(key) for key in github_keys):
        with GITHUB_LOCK:
            GITHUB_CACHE["expires_at"] = 0

    blog_keys = ("blogUrl", "blogRefreshSeconds")
    if any(old_config.get(key) != new_config.get(key) for key in blog_keys):
        with BLOG_LOCK:
            BLOG_CACHE["expires_at"] = 0

    rate_limit_keys = ("rateLimitEnabled", "rateLimitWindowSeconds", "rateLimitMaxRequests")
    if any(old_config.get(key) != new_config.get(key) for key in rate_limit_keys):
        with RATE_LIMIT_LOCK:
            RATE_LIMIT_HITS.clear()


def config_status_payload():
    with CONFIG_LOCK:
        payload = {
            "path": CONFIG_PATH,
            "lastCheckedAt": CONFIG_FILE_STATE["lastCheckedAt"],
            "lastLoadedAt": CONFIG_FILE_STATE["lastLoadedAt"],
            "lastReloadStatus": CONFIG_FILE_STATE["lastReloadStatus"],
            "reloadCount": CONFIG_FILE_STATE["reloadCount"],
            "warnings": [],
        }
    payload["warnings"] = current_config_warnings()
    restart_required = restart_required_details()
    payload["restartRequired"] = bool(restart_required)
    payload["restartRequiredKeys"] = [detail["key"] for detail in restart_required]
    payload["restartRequiredDetails"] = restart_required
    return payload


def cache_status_payload(cache, lock):
    now = time.time()
    with lock:
        has_value = cache.get("status") is not None
        expires_at = cache.get("expires_at", 0) or 0
        refreshing = bool(cache.get("refreshing"))

    expires_in = max(0, int(math.ceil(expires_at - now))) if expires_at else 0
    if not has_value:
        state = "empty_refreshing" if refreshing else "empty"
    elif refreshing:
        state = "stale_refreshing"
    elif expires_at > now:
        state = "fresh"
    else:
        state = "stale"

    return {
        "state": state,
        "hasValue": has_value,
        "refreshing": refreshing,
        "expiresAt": utc_timestamp_from_epoch(expires_at),
        "expiresInSeconds": expires_in,
    }


def cache_statuses_payload():
    return {
        "minecraft": cache_status_payload(STATUS_CACHE, STATUS_LOCK),
        "github": cache_status_payload(GITHUB_CACHE, GITHUB_LOCK),
        "blog": cache_status_payload(BLOG_CACHE, BLOG_LOCK),
    }


def api_status_payload():
    return {
        "minecraft": get_status(),
        "github": get_github_status(),
        "blog": get_blog_status(),
        "cache": cache_statuses_payload(),
        "configWarnings": current_config_warnings(),
        "config": config_status_payload(),
    }


def safe_int(value, default, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def safe_bool(value, default):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "yes", "y", "on"):
            return True
        if normalized in ("0", "false", "no", "n", "off"):
            return False
    return default


def config_int(key, default, minimum=None, maximum=None):
    return safe_int(current_config().get(key, default), default, minimum, maximum)


REQUEST_SEMAPHORE = threading.BoundedSemaphore(config_int("maxRequestThreads", 64, 1))
STARTUP_RUNTIME_CONFIG = {
    "listenHost": str(CONFIG.get("listenHost", DEFAULT_CONFIG["listenHost"])),
    "listenPort": safe_int(CONFIG.get("listenPort"), DEFAULT_CONFIG["listenPort"], 1, 65535),
    "maxRequestThreads": safe_int(CONFIG.get("maxRequestThreads"), DEFAULT_CONFIG["maxRequestThreads"], 1),
}


def runtime_config_values(config=None):
    config = config or current_config()
    return {
        "listenHost": str(config.get("listenHost", DEFAULT_CONFIG["listenHost"])),
        "listenPort": safe_int(config.get("listenPort"), DEFAULT_CONFIG["listenPort"], 1, 65535),
        "maxRequestThreads": safe_int(config.get("maxRequestThreads"), DEFAULT_CONFIG["maxRequestThreads"], 1),
    }


def restart_required_details(config=None):
    configured = runtime_config_values(config)
    details = []
    for key, running_value in STARTUP_RUNTIME_CONFIG.items():
        configured_value = configured.get(key)
        if configured_value != running_value:
            details.append(
                {
                    "key": key,
                    "running": running_value,
                    "configured": configured_value,
                    "message": "Restart the Python server for this setting to take effect.",
                }
            )
    return details


class BadRequestError(Exception):
    pass


def validate_http_version(version):
    if version not in ("HTTP/1.0", "HTTP/1.1"):
        raise BadRequestError("HTTP version must be HTTP/1.0 or HTTP/1.1")


def normalize_request_target(target):
    if not target:
        raise BadRequestError("HTTP request target is empty")
    if len(target) > MAX_REQUEST_TARGET_LENGTH:
        raise BadRequestError("HTTP request target is too long")
    if any(ord(char) < 32 or ord(char) == 127 for char in target):
        raise BadRequestError("HTTP request target contains control characters")

    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise BadRequestError("HTTP absolute request target must use http:// or https://")
        return parsed.path or "/"
    if not target.startswith("/"):
        raise BadRequestError("HTTP request target must start with /")
    return target


def clean_log_field(value, max_length=MAX_LOG_FIELD_LENGTH):
    text = str(value if value is not None else "")
    text = "".join(char if char.isprintable() and char not in "\r\n\t" else "?" for char in text)
    if len(text) > max_length:
        return text[: max(0, max_length - 3)] + "..."
    return text


def clean_client_ip(value):
    return clean_log_field(str(value or "").strip() or "-", 128)


def append_runtime_log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    try:
        with RUNTIME_LOG_LOCK:
            rotate_runtime_log_if_needed()
            with open(RUNTIME_LOG_PATH, "a", encoding="utf-8") as log_file:
                log_file.write(f"[{timestamp}] {message}\n")
    except Exception:
        pass


def rotate_runtime_log_if_needed():
    max_bytes = config_int("runtimeLogMaxBytes", 1048576, 0)
    if max_bytes <= 0:
        return
    if not os.path.exists(RUNTIME_LOG_PATH):
        return
    if os.path.getsize(RUNTIME_LOG_PATH) < max_bytes:
        return

    backup_path = f"{RUNTIME_LOG_PATH}.1"
    try:
        if os.path.exists(backup_path):
            os.remove(backup_path)
        os.replace(RUNTIME_LOG_PATH, backup_path)
    except OSError:
        pass


def write_varint(value):
    data = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        data.append(byte)
        if not value:
            return bytes(data)


def read_varint(sock):
    value = 0
    position = 0
    while True:
        data = sock.recv(1)
        if not data:
            raise ValueError("connection closed")
        byte = data[0]
        value |= (byte & 0x7F) << position
        if not byte & 0x80:
            return value
        position += 7
        if position >= 35:
            raise ValueError("varint too large")


def make_packet(packet_id, payload=b""):
    body = write_varint(packet_id) + payload
    return write_varint(len(body)) + body


def make_handshake(host, port):
    host_bytes = host.encode("utf-8")
    payload = b"".join(
        [
            write_varint(763),
            write_varint(len(host_bytes)),
            host_bytes,
            struct.pack(">H", int(port)),
            write_varint(1),
        ]
    )
    return make_packet(0x00, payload)


def recv_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("connection closed")
        data.extend(chunk)
    return bytes(data)


def strip_minecraft_formatting(text):
    output = []
    skip_next = False
    for char in str(text or ""):
        if skip_next:
            skip_next = False
            continue
        if char == "\u00a7":
            skip_next = True
            continue
        output.append(char)
    return "".join(output)


def component_to_plain(value):
    if isinstance(value, str):
        return strip_minecraft_formatting(value)
    if not isinstance(value, dict):
        return ""

    text = str(value.get("text", ""))
    extra = value.get("extra")
    if isinstance(extra, list):
        text += "".join(component_to_plain(item) for item in extra)
    return strip_minecraft_formatting(text)


def ping_one_host(connect_host, display_host, port):
    started_at = time.time()
    timeout = config_int("minecraftPingTimeoutSeconds", 5, 1, 30)
    with socket.create_connection((connect_host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(make_handshake(display_host, port))
        sock.sendall(make_packet(0x00))

        _packet_length = read_varint(sock)
        packet_id = read_varint(sock)
        if packet_id != 0x00:
            raise ValueError("invalid status packet")

        json_length = read_varint(sock)
        if json_length > MAX_MINECRAFT_STATUS_BYTES:
            raise ValueError(f"minecraft status response exceeds {MAX_MINECRAFT_STATUS_BYTES} bytes")
        payload = recv_exact(sock, json_length).decode("utf-8")
        response = json.loads(payload)

    return {
        "online": True,
        "latencyMs": int((time.time() - started_at) * 1000),
        "motd": component_to_plain(response.get("description")) or "\u670d\u52a1\u5668\u5728\u7ebf",
        "playersOnline": response.get("players", {}).get("online", 0),
        "playersMax": response.get("players", {}).get("max", 0),
        "version": response.get("version", {}).get("name", "\u672a\u77e5\u7248\u672c"),
        "protocolVersion": response.get("version", {}).get("protocol"),
        "connectHost": connect_host,
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def get_connect_hosts(display_host):
    config = current_config()
    display_host = str(display_host or DEFAULT_CONFIG["minecraftHost"]).strip() or DEFAULT_CONFIG["minecraftHost"]
    hosts = normalized_host_list(config.get("minecraftConnectHosts"))
    if not hosts:
        hosts.append(display_host)

    last_successful_host = LAST_SUCCESSFUL_CONNECT_HOST.get("host")
    if last_successful_host in hosts:
        return [last_successful_host] + [host for host in hosts if host != last_successful_host]
    return hosts


def ping_minecraft_server():
    config = current_config()
    display_host = str(config.get("minecraftHost") or DEFAULT_CONFIG["minecraftHost"]).strip() or DEFAULT_CONFIG["minecraftHost"]
    port = config_int("minecraftPort", DEFAULT_CONFIG["minecraftPort"], 1, 65535)
    errors = []

    for connect_host in get_connect_hosts(display_host):
        try:
            status = ping_one_host(connect_host, display_host, port)
            LAST_SUCCESSFUL_CONNECT_HOST["host"] = connect_host
            return status
        except Exception as error:
            errors.append(f"{connect_host}: {error}")

    LAST_SUCCESSFUL_CONNECT_HOST["host"] = ""
    return {
        "online": False,
        "error": "; ".join(errors),
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def get_status():
    while True:
        stale_status = None
        should_start_refresh = False
        now = time.time()
        with STATUS_LOCK:
            cached_status = STATUS_CACHE["status"]
            if cached_status and now < STATUS_CACHE["expires_at"]:
                return cached_status

            if cached_status:
                if not STATUS_CACHE["refreshing"]:
                    STATUS_CACHE["refreshing"] = True
                    should_start_refresh = True
                stale_status = dict(cached_status)
                stale_status["cacheState"] = "stale_refreshing"

            if not STATUS_CACHE["refreshing"]:
                STATUS_CACHE["refreshing"] = True
                break

        if stale_status is not None:
            if should_start_refresh:
                start_status_refresh_thread()
            return stale_status

        time.sleep(0.05)

    return refresh_status_cache()


def start_status_refresh_thread():
    try:
        threading.Thread(target=refresh_status_cache, name="minecraft-status-refresh", daemon=True).start()
    except Exception as error:
        append_runtime_log(f"[STATUS_REFRESH] failed to start background refresh: {error}")
        with STATUS_LOCK:
            STATUS_CACHE["refreshing"] = False


def refresh_status_cache():
    try:
        status = ping_minecraft_server()
        record_latency_sample(status)
        enrich_latency_metrics(status)
        with STATUS_LOCK:
            STATUS_CACHE["status"] = status
            STATUS_CACHE["expires_at"] = time.time() + config_int("refreshSeconds", 30, 5)
            STATUS_CACHE["refreshing"] = False
        return status
    except Exception as error:
        append_runtime_log(f"[STATUS_REFRESH] {error}")
        status = {
            "online": False,
            "error": str(error),
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with STATUS_LOCK:
            STATUS_CACHE["status"] = status
            STATUS_CACHE["expires_at"] = time.time() + 5
            STATUS_CACHE["refreshing"] = False
        return status


def record_latency_sample(status):
    now = time.time()
    with LATENCY_LOCK:
        LATENCY_HISTORY.append(
            {
                "time": now,
                "online": bool(status.get("online")),
                "latency": safe_int(status.get("latencyMs"), 0, 0),
            }
        )
        while LATENCY_HISTORY and (
            now - LATENCY_HISTORY[0]["time"] > 180 or len(LATENCY_HISTORY) > MAX_LATENCY_SAMPLES
        ):
            LATENCY_HISTORY.popleft()


def enrich_latency_metrics(status):
    with LATENCY_LOCK:
        samples = list(LATENCY_HISTORY)
    if not samples:
        status["latencyAverageMs"] = status.get("latencyMs")
        status["latencyStability"] = 0
        return

    online_samples = [sample for sample in samples if sample["online"] and sample["latency"] > 0]
    if not online_samples:
        status["latencyAverageMs"] = None
        status["latencyStability"] = 0
        return

    latencies = [sample["latency"] for sample in online_samples]
    average = sum(latencies) / len(latencies)
    variance = sum((latency - average) ** 2 for latency in latencies) / len(latencies)
    jitter = variance ** 0.5
    success_rate = len(online_samples) / len(samples)
    jitter_penalty = min(45, int(jitter / 3))
    stability = max(0, min(100, int(success_rate * 100) - jitter_penalty))

    status["latencyAverageMs"] = int(round(average))
    status["latencyStability"] = stability
    status["latencySampleCount"] = len(samples)


def get_github_status():
    while True:
        stale_status = None
        should_start_refresh = False
        now = time.time()
        with GITHUB_LOCK:
            cached_status = GITHUB_CACHE["status"]
            if cached_status and now < GITHUB_CACHE["expires_at"]:
                return cached_status

            if cached_status:
                if not GITHUB_CACHE["refreshing"]:
                    GITHUB_CACHE["refreshing"] = True
                    should_start_refresh = True
                stale_status = dict(cached_status)
                stale_status["cacheState"] = "stale_refreshing"

            if not GITHUB_CACHE["refreshing"]:
                GITHUB_CACHE["refreshing"] = True
                break

        if stale_status is not None:
            if should_start_refresh:
                start_github_refresh_thread()
            return stale_status

        time.sleep(0.05)
    return refresh_github_status()


def refresh_github_status():
    now = time.time()
    config = current_config()
    name = config.get("githubName", "SsCfg")
    index_url = config.get("sscfgIndexUrl", "").strip()

    try:
        if not index_url:
            raise ValueError("sscfgIndexUrl is empty")
        index_payload, source_url = fetch_json_with_proxy(index_url)
        packs = index_payload.get("packs")
        if not isinstance(packs, list):
            raise ValueError("index.json missing packs list")
        latest_pack = find_latest_pack(packs)
        status = {
            "online": True,
            "name": name,
            "indexUrl": index_url,
            "sourceUrl": source_url,
            "packCount": len(packs),
            "latestPack": latest_pack,
            "configMessage": f"{len(packs)} CFG",
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
    except Exception as error:
        status = {
            "online": False,
            "name": name,
            "indexUrl": index_url,
            "sourceUrl": "",
            "error": str(error),
            "configOk": False,
            "configMessage": f"index.json \u8bfb\u53d6\u5931\u8d25\uff1a{error}",
            "packCount": 0,
            "latestPack": {},
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    with GITHUB_LOCK:
        GITHUB_CACHE["status"] = status
        GITHUB_CACHE["expires_at"] = now + config_int("sscfgRefreshSeconds", 900, 10)
        GITHUB_CACHE["refreshing"] = False
    return status


def start_github_refresh_thread():
    try:
        threading.Thread(target=refresh_github_status, name="sscfg-github-refresh", daemon=True).start()
    except Exception as error:
        append_runtime_log(f"[GITHUB_REFRESH] failed to start background refresh: {error}")
        with GITHUB_LOCK:
            GITHUB_CACHE["refreshing"] = False


def clear_cache_refreshing(cache, lock):
    with lock:
        cache["refreshing"] = False


def safe_monitor_refresh(refresh_func, cache, lock, log_prefix):
    try:
        return refresh_func()
    except Exception:
        append_runtime_log(f"[{log_prefix}] unexpected refresh failure:\n{traceback.format_exc()}")
        clear_cache_refreshing(cache, lock)
        return None


def monitor_interval_seconds(key, default, minimum, log_prefix):
    try:
        return config_int(key, default, minimum)
    except Exception:
        append_runtime_log(f"[{log_prefix}] invalid monitor interval:\n{traceback.format_exc()}")
        return default


def github_monitor_loop():
    while not GITHUB_STOP.is_set():
        safe_monitor_refresh(refresh_github_status, GITHUB_CACHE, GITHUB_LOCK, "GITHUB_MONITOR")
        interval = monitor_interval_seconds("sscfgRefreshSeconds", 900, 10, "GITHUB_MONITOR")
        GITHUB_STOP.wait(interval)


def proxied_url(url):
    prefix = current_config().get("githubProxyPrefix", "").strip()
    if not prefix:
        return ""
    return f"{prefix.rstrip('/')}/{url}"


def fetch_json_with_proxy(url):
    proxy_url = proxied_url(url)
    if proxy_url:
        try:
            return fetch_json(proxy_url), proxy_url
        except Exception:
            pass
    return fetch_json(url), url


def find_latest_pack(packs):
    if not packs:
        return {}

    def sort_key(pack):
        date = str(pack.get("date", ""))
        version = str(pack.get("version", ""))
        return (date, version, str(pack.get("id", "")))

    latest = max((pack for pack in packs if isinstance(pack, dict)), key=sort_key, default={})
    return {
        "id": latest.get("id", ""),
        "name": latest.get("name", "\u672a\u547d\u540d CFG"),
        "author": latest.get("author", "\u672a\u77e5"),
        "summary": latest.get("summary", ""),
        "version": latest.get("version", ""),
        "date": latest.get("date", ""),
        "southsideVersion": latest.get("southsideVersion", ""),
        "fileName": latest.get("fileName", ""),
        "sha256": latest.get("sha256", ""),
        "downloadUrl": latest.get("downloadUrl", ""),
    }


def get_blog_status():
    while True:
        stale_status = None
        should_start_refresh = False
        now = time.time()
        with BLOG_LOCK:
            cached_status = BLOG_CACHE["status"]
            if cached_status and now < BLOG_CACHE["expires_at"]:
                return cached_status

            if cached_status:
                if not BLOG_CACHE["refreshing"]:
                    BLOG_CACHE["refreshing"] = True
                    should_start_refresh = True
                stale_status = dict(cached_status)
                stale_status["cacheState"] = "stale_refreshing"

            if not BLOG_CACHE["refreshing"]:
                BLOG_CACHE["refreshing"] = True
                break

        if stale_status is not None:
            if should_start_refresh:
                start_blog_refresh_thread()
            return stale_status

        time.sleep(0.05)
    return refresh_blog_status()


def refresh_blog_status():
    now = time.time()
    blog_url = current_config().get("blogUrl", "").strip()

    try:
        if not blog_url:
            raise ValueError("blogUrl is empty")
        request = Request(blog_url, headers={"User-Agent": "PclHome-BJD"})
        with open_url(request, timeout=8) as response:
            html_text = read_response_text(response, errors="replace")
        status = parse_blog_status(html_text, blog_url)
    except Exception as error:
        status = {
            "online": False,
            "title": "\u5e03\u5409\u5c9b\u6700\u65b0\u66f4\u65b0",
            "date": "\u672a\u77e5",
            "summary": str(error),
            "url": blog_url,
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    with BLOG_LOCK:
        BLOG_CACHE["status"] = status
        BLOG_CACHE["expires_at"] = now + config_int("blogRefreshSeconds", 900, 60)
        BLOG_CACHE["refreshing"] = False
    return status


def start_blog_refresh_thread():
    try:
        threading.Thread(target=refresh_blog_status, name="bjd-blog-refresh", daemon=True).start()
    except Exception as error:
        append_runtime_log(f"[BLOG_REFRESH] failed to start background refresh: {error}")
        with BLOG_LOCK:
            BLOG_CACHE["refreshing"] = False


def blog_monitor_loop():
    while not BLOG_STOP.is_set():
        safe_monitor_refresh(refresh_blog_status, BLOG_CACHE, BLOG_LOCK, "BLOG_MONITOR")
        interval = monitor_interval_seconds("blogRefreshSeconds", 900, 60, "BLOG_MONITOR")
        BLOG_STOP.wait(interval)


def parse_blog_status(html_text, blog_url):
    article_blocks = re.findall(r"<article\b.*?</article>", html_text, flags=re.IGNORECASE | re.DOTALL)
    candidates = article_blocks or re.findall(
        r"(<h[1-3][^>]*>.*?</h[1-3]>.*?)(?=<h[1-3][^>]*>|$)",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for block in candidates:
        plain = normalize_html_text(block)
        if "\u66f4\u65b0\u65e5\u5fd7" not in plain:
            continue
        if "\u6b22\u8fce\u4f60\u7684\u5230\u6765" in plain:
            continue

        title = extract_first_heading(block) or first_line(plain)
        date = extract_date(plain) or "\u672a\u77e5"
        summary = make_blog_summary(plain, title)
        link = extract_first_link(block, blog_url) or blog_url
        return {
            "online": True,
            "title": title,
            "date": date,
            "summary": summary,
            "url": link,
            "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    plain = normalize_html_text(html_text)
    raise ValueError("\u672a\u627e\u5230\u6807\u9898\u5305\u542b\u201c\u66f4\u65b0\u65e5\u5fd7\u201d\u7684\u6587\u7ae0")


def normalize_html_text(html_text):
    text = re.sub(r"(?is)<script\b.*?</script>|<style\b.*?</style>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*", "\n", text)
    return text.strip()


def extract_first_heading(html_text):
    match = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", html_text)
    if not match:
        return ""
    return normalize_html_text(match.group(1))


def extract_date(text):
    match = re.search(r"(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})", text)
    return match.group(1).replace("/", "-").replace(".", "-") if match else ""


def first_line(text):
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def make_blog_summary(text, title):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    filtered = [line for line in lines if line != title and "\u66f4\u65b0\u65e5\u5fd7" not in line]
    summary = " ".join(filtered[:3]).strip() or "\u6682\u65e0\u6458\u8981"
    if len(summary) > 160:
        summary = summary[:157] + "..."
    return summary


def extract_first_link(html_text, base_url):
    match = re.search(r'(?is)<a\s+[^>]*href=["\']([^"\']+)["\']', html_text)
    if not match:
        return ""
    href = html_lib.unescape(match.group(1))
    if href.startswith("http://") or href.startswith("https://"):
        return href
    parsed = urlparse(base_url)
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return f"{base_url.rstrip('/')}/{href.lstrip('/')}"


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "PclHome-BJD", "Accept": "application/vnd.github+json"})
    with open_url(request, timeout=8) as response:
        return json.loads(read_response_text(response))


def open_url(request, timeout):
    if not current_config().get("verifyHttps", False):
        return urlopen(request, timeout=timeout, context=UNVERIFIED_SSL_CONTEXT)
    try:
        return urlopen(request, timeout=timeout)
    except ssl.SSLError:
        return urlopen(request, timeout=timeout, context=UNVERIFIED_SSL_CONTEXT)


def read_response_text(response, encoding="utf-8", errors="strict"):
    max_bytes = config_int("remoteFetchMaxBytes", 1048576, 1024)
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"remote response exceeds {max_bytes} bytes")
    return payload.decode(encoding, errors=errors)


def xml_escape(value):
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

def check_rate_limit(client_ip):
    config = current_config()
    if not config.get("rateLimitEnabled", False):
        return True, 0, ""

    global RATE_LIMIT_NEXT_CLEANUP_AT
    now = time.time()
    window_seconds = config_int("rateLimitWindowSeconds", 5, 1)
    max_requests = config_int("rateLimitMaxRequests", 3, 1)

    with RATE_LIMIT_LOCK:
        if now >= RATE_LIMIT_NEXT_CLEANUP_AT:
            stale_ips = [
                ip
                for ip, queued_hits in RATE_LIMIT_HITS.items()
                if not queued_hits or now - queued_hits[-1] > window_seconds
            ]
            for ip in stale_ips:
                RATE_LIMIT_HITS.pop(ip, None)
            RATE_LIMIT_NEXT_CLEANUP_AT = now + window_seconds

        hits = RATE_LIMIT_HITS[client_ip]
        while hits and now - hits[0] > window_seconds:
            hits.popleft()

        if len(hits) >= max_requests:
            retry_after = max(1, int(window_seconds - (now - hits[0])))
            return False, retry_after, "\u8bbf\u95ee\u8fc7\u4e8e\u9891\u7e41\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5"

        hits.append(now)
        return True, 0, ""


def calculate_health(status, github_status, blog_status):
    score = 0

    if status.get("online"):
        score += 45
        latency = safe_int(status.get("latencyMs"), 999, 0)
        if latency <= 80:
            score += 20
        elif latency <= 150:
            score += 15
        elif latency <= 300:
            score += 8

        max_players = safe_int(status.get("playersMax"), 0, 0)
        online_players = safe_int(status.get("playersOnline"), 0, 0)
        if max_players > 0:
            usage = online_players / max_players
            if usage < 0.85:
                score += 10
            elif usage < 0.95:
                score += 5
    if github_status.get("online"):
        score += 15
    if blog_status.get("online"):
        score += 10

    score = max(0, min(100, score))
    if score >= 85:
        color = "#2FA866"
        label = "\u826f\u597d"
    elif score >= 60:
        color = "#F0A202"
        label = "\u6ce8\u610f"
    else:
        color = "#D83B01"
        label = "\u5f02\u5e38"
    return {"score": score, "color": color, "label": label}


def make_health_ring_xaml(health):
    score = int(health["score"])
    color = health["color"]
    if score >= 100:
        arc = '<Ellipse Width="46" Height="46" Stroke="%s" StrokeThickness="4" />' % color
    elif score <= 0:
        arc = ""
    else:
        radius = 23
        center = 26
        angle = (score / 100) * 359.9 - 90
        radians = math.radians(angle)
        end_x = center + radius * math.cos(radians)
        end_y = center + radius * math.sin(radians)
        large_arc = 1 if score > 50 else 0
        arc = (
            f'<Path Stroke="{color}" StrokeThickness="4" StrokeStartLineCap="Round" StrokeEndLineCap="Round" '
            f'Data="M 26,3 A 23,23 0 {large_arc} 1 {end_x:.2f},{end_y:.2f}" />'
        )

    return f"""<Grid Width="52" Height="52" HorizontalAlignment="Right" ToolTip="\u670d\u52a1\u5668\u5065\u5eb7\u5ea6\uff1a{score}% {xml_escape(health['label'])}">
  <Ellipse Width="46" Height="46" Stroke="#22000000" StrokeThickness="4" />
  {arc}
  <TextBlock Text="{score}%" FontSize="13" FontWeight="Bold" HorizontalAlignment="Center" VerticalAlignment="Center" />
</Grid>"""


def get_home_config():
    return merge_config(DEFAULT_CONFIG["home"], current_config().get("home", {}))


def home_text(home_config, key):
    fallback = DEFAULT_CONFIG["home"].get(key, "")
    return str(home_config.get(key, fallback))


def xaml_color(value, default="#F4F8FF"):
    color = str(value or "").strip()
    if is_xaml_color_value(color):
        return color
    return default


def make_badge_xaml(text, color, font_size=12, attrs=""):
    attr_text = f" {attrs}" if attrs else ""
    return f"""<Border{attr_text} Background="{xaml_color(color, '#D83B01')}" CornerRadius="3" Padding="8,2">
  <TextBlock Text="{xml_escape(text)}" Foreground="White" FontWeight="Bold" FontSize="{safe_int(font_size, 12, 8, 24)}" />
</Border>"""


def make_metric_xaml(column, label, value, trim=False):
    trim_attr = ' TextTrimming="CharacterEllipsis"' if trim else ""
    return f"""<StackPanel Grid.Column="{column}">
  <TextBlock Text="{xml_escape(label)}" Foreground="{{DynamicResource ColorBrush3}}" />
  <TextBlock Text="{xml_escape(value)}" FontSize="20" FontWeight="Bold"{trim_attr} />
</StackPanel>"""


def make_app_version_xaml(home_config):
    if not safe_bool(home_config.get("showAppVersion"), True):
        return ""
    return (
        f'<TextBlock Text="v{xml_escape(APP_VERSION)}" HorizontalAlignment="Right" '
        'Foreground="{DynamicResource ColorBrush3}" FontSize="11" Margin="0,-8,0,2" />'
    )


def make_status_card_xaml(status, blog_status, health, home_config):
    config = current_config()
    address = f"{config['minecraftHost']}:{config['minecraftPort']}"
    online = status.get("online")
    state_text = "\u5728\u7ebf" if online else "\u79bb\u7ebf"
    avg_latency = status.get("latencyAverageMs")
    stability = safe_int(status.get("latencyStability"), 0, 0, 100)
    latency_text = f"{avg_latency} ms / {stability}%" if avg_latency is not None else "\u65e0\u6cd5\u8fde\u63a5"
    player_text = str(status.get("playersOnline", 0)) if online else "\u672a\u77e5"
    accent = "#2FA866" if online else "#D83B01"
    blog_online = blog_status.get("online")
    blog_state = "\u5df2\u83b7\u53d6" if blog_online else "\u5f02\u5e38"
    blog_accent = "#2FA866" if blog_online else "#D83B01"
    blog_detail = f"{blog_status.get('date', '\u672a\u77e5')}  {blog_status.get('title', '\u672a\u77e5\u66f4\u65b0')}  {blog_status.get('summary', '')}"
    health_ring = make_health_ring_xaml(health)
    app_version_xaml = make_app_version_xaml(home_config)
    online_badge = make_badge_xaml(state_text, accent, 11, 'Margin="8,2,0,0" VerticalAlignment="Center"')
    blog_badge = make_badge_xaml(blog_state, blog_accent, 12)

    return f"""<local:MyCard Title="{xml_escape(config['serverName'])}" Margin="0,0,0,12">
  <StackPanel Margin="20,22,18,6">
    {app_version_xaml}
    <Grid Margin="0,2,0,2">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="62" />
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0" Margin="6,6,8,0">
        <StackPanel Orientation="Horizontal" Margin="0,0,0,4">
          <TextBlock Text="{xml_escape(address)}" FontSize="16" FontWeight="Bold" Foreground="{{DynamicResource ColorBrush1}}" />
          {online_badge}
        </StackPanel>
      </StackPanel>
      <StackPanel Grid.Column="1" Margin="4,-12,0,0" HorizontalAlignment="Right" VerticalAlignment="Center">
        {health_ring}
      </StackPanel>
    </Grid>
    <Grid Margin="0,-2,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="0.9*" />
        <ColumnDefinition Width="1.15*" />
        <ColumnDefinition Width="0.95*" />
      </Grid.ColumnDefinitions>
      {make_metric_xaml(0, home_text(home_config, 'playersLabel'), player_text)}
      {make_metric_xaml(1, home_text(home_config, 'latencyLabel'), latency_text)}
      {make_metric_xaml(2, home_text(home_config, 'versionLabel'), home_text(home_config, 'displayVersion'), True)}
    </Grid>
    <Grid Margin="0,6,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="Auto" />
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0">
        <StackPanel Orientation="Horizontal" Margin="0,0,0,2">
          <TextBlock Text="{xml_escape(home_text(home_config, 'latestUpdateTitle'))}" FontWeight="Bold" Margin="0,0,8,0" />
          {blog_badge}
        </StackPanel>
        <TextBlock Text="{xml_escape(blog_detail)}" Foreground="{{DynamicResource ColorBrush3}}" TextWrapping="Wrap" />
      </StackPanel>
      <local:MyButton Grid.Column="1" Text="{xml_escape(home_text(home_config, 'refreshButtonText'))}" Width="64" Height="30" HorizontalAlignment="Right" VerticalAlignment="Bottom" Margin="12,0,0,0" EventType="\u5237\u65b0\u4e3b\u9875" ToolTip="{xml_escape(home_text(home_config, 'refreshButtonTooltip'))}" />
    </Grid>
  </StackPanel>
</local:MyCard>"""


def make_github_card_xaml(github_status, home_config):
    github_online = github_status.get("online")
    github_state = "\u6b63\u5e38" if github_online else "\u5f02\u5e38"
    github_accent = "#2FA866" if github_online else "#D83B01"
    latest_pack = github_status.get("latestPack") or {}
    github_detail = (
        f"\u5171 {github_status.get('packCount', 0)} \u4e2a CFG\uff0c\u6700\u65b0\uff1a{latest_pack.get('name', '\u672a\u77e5')}  {latest_pack.get('version', '')}  {latest_pack.get('date', '')}  by {latest_pack.get('author', '\u672a\u77e5')}"
        if github_online
        else f"index.json: {github_status.get('configMessage') or github_status.get('error', '\u65e0\u6cd5\u8fde\u63a5')}"
    )
    github_badge = make_badge_xaml(
        github_state,
        github_accent,
        12,
        'Grid.Column="1" Margin="12,0,0,0" VerticalAlignment="Top"',
    )

    return f"""<local:MyCard Title="{xml_escape(home_text(home_config, 'sscfgCardTitle'))}" Margin="0,0,0,15">
  <StackPanel Margin="22,34,20,12">
    <Grid Margin="0,-4,0,8">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="Auto" />
      </Grid.ColumnDefinitions>
      <TextBlock Text="{xml_escape(github_status.get('name', 'SsCfg'))}" FontSize="16" FontWeight="Bold" Foreground="{{DynamicResource ColorBrush1}}" />
      {github_badge}
    </Grid>
    <TextBlock Text="{xml_escape(github_detail)}" TextWrapping="Wrap" />
  </StackPanel>
</local:MyCard>"""


def make_custom_card_xaml(card):
    if not isinstance(card, dict):
        return ""
    if not safe_bool(card.get("enabled"), True):
        return ""

    title = str(card.get("title", "")).strip() or "\u516c\u544a"
    text = str(card.get("text", "")).strip()
    if not text:
        return ""

    background = xaml_color(card.get("background"), "#F4F8FF")
    font_size = safe_int(card.get("fontSize"), 15, 10, 24)
    font_weight = "Bold" if safe_bool(card.get("bold"), True) else "Normal"
    return f"""<local:MyCard Title="{xml_escape(title)}" Margin="0,0,0,15">
  <StackPanel Margin="22,30,20,10">
    <Border Background="{background}" CornerRadius="6" Padding="14,10">
      <TextBlock Text="{xml_escape(text)}" FontSize="{font_size}" FontWeight="{font_weight}" TextWrapping="Wrap" />
    </Border>
  </StackPanel>
</local:MyCard>"""


def make_custom_cards_xaml(home_config):
    cards = home_config.get("customCards", [])
    if not isinstance(cards, list):
        return ""
    rendered_cards = [make_custom_card_xaml(card) for card in cards]
    return "\n\n".join(card for card in rendered_cards if card)


def make_config_warning_card_xaml():
    warnings = current_config_warnings()
    if not warnings:
        return ""
    warning_text = "\uff1b".join(warnings[:3])
    if len(warnings) > 3:
        warning_text += f"\uff1b\u8fd8\u6709 {len(warnings) - 3} \u6761\u914d\u7f6e\u544a\u8b66"
    return make_custom_card_xaml(
        {
            "enabled": True,
            "title": "\u914d\u7f6e\u63d0\u9192",
            "text": warning_text,
            "background": "#FFF4CE",
            "fontSize": 13,
            "bold": False,
        }
    )


def make_home_xaml(status, github_status, blog_status):
    home_config = get_home_config()
    health = calculate_health(status, github_status, blog_status)
    sections = [
        make_status_card_xaml(status, blog_status, health, home_config),
        make_config_warning_card_xaml(),
        make_github_card_xaml(github_status, home_config),
        make_custom_cards_xaml(home_config),
    ]
    return "\n\n".join(section for section in sections if section)


def is_client_disconnect_error(error_text):
    return (
        "ConnectionAbortedError" in error_text
        or "BrokenPipeError" in error_text
        or "WinError 10053" in error_text
        or "WinError 10054" in error_text
    )


def http_status_text(status_code):
    return {
        400: "Bad Request",
        200: "OK",
        404: "Not Found",
        405: "Method Not Allowed",
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status_code, "OK")


def should_log_request(path):
    config = current_config()
    if not config.get("requestLoggingEnabled", True):
        return False
    return path != "/healthz" or config.get("logHealthChecks", False)


def log_request_line(line):
    print(line)
    append_runtime_log(line)


def build_http_response(status_code, content_type, body, extra_headers=None, include_body=True):
    if isinstance(body, str):
        body = body.encode("utf-8")
    headers = [
        f"HTTP/1.1 {status_code} {http_status_text(status_code)}",
        f"Content-Type: {content_type}",
        "Cache-Control: no-store",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    if extra_headers:
        headers.extend(extra_headers)
    response = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii")
    if include_body:
        response += body
    return response


def build_json_response(status_code, payload, extra_headers=None, include_body=True):
    return build_http_response(
        status_code,
        HTTP_JSON_CONTENT_TYPE,
        json.dumps(payload, ensure_ascii=False, indent=2),
        extra_headers,
        include_body,
    )


def parse_http_request(client_socket):
    data = b""
    deadline = time.time() + config_int("requestTimeoutSeconds", 10, 1, 120)
    try:
        while b"\r\n\r\n" not in data and len(data) < MAX_HTTP_HEADER_BYTES:
            remaining_seconds = deadline - time.time()
            if remaining_seconds <= 0:
                return None
            client_socket.settimeout(remaining_seconds)
            chunk = client_socket.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        return None
    if data and b"\r\n\r\n" not in data:
        raise BadRequestError("HTTP headers are too large or incomplete")
    header_text = data.decode("iso-8859-1", errors="replace")
    lines = header_text.split("\r\n")
    if not lines or not lines[0]:
        return None
    request_line_parts = lines[0].split()
    if len(request_line_parts) != 3:
        raise BadRequestError("HTTP request line must be METHOD TARGET VERSION")
    method, path, version = request_line_parts
    if len(method) > 16:
        raise BadRequestError("HTTP method is too long")
    if not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", method):
        raise BadRequestError("HTTP method contains invalid characters")
    path = normalize_request_target(path)
    validate_http_version(version)
    headers = {}
    for line in lines[1:]:
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return {"method": method, "path": path, "headers": headers}


def handle_http_request(client_socket, client_address):
    config_token = None
    warnings_token = None
    try:
        client_socket.settimeout(config_int("requestTimeoutSeconds", 10, 1, 120))
        request = parse_http_request(client_socket)
        if not request:
            return
        client_socket.settimeout(config_int("requestTimeoutSeconds", 10, 1, 120))
        path = urlparse(request["path"]).path.lower()
        method = request["method"].upper()
        include_body = method != "HEAD"
        if path != "/healthz" and method in ("GET", "HEAD"):
            reload_config_if_needed()
        config_snapshot, warning_snapshot = get_request_config_snapshot()
        config_token = REQUEST_CONFIG.set(config_snapshot)
        warnings_token = REQUEST_CONFIG_WARNINGS.set(warning_snapshot)

        client_ip = clean_client_ip(client_address[0])
        forwarded = request["headers"].get("x-forwarded-for", "")
        real_ip = request["headers"].get("x-real-ip", "")
        if current_config().get("trustProxyHeaders"):
            if forwarded:
                client_ip = clean_client_ip(forwarded.split(",", 1)[0])
            elif real_ip:
                client_ip = clean_client_ip(real_ip)

        user_agent = clean_log_field(request["headers"].get("user-agent", "-"))
        log_path = clean_log_field(request["path"])
        line = f"[REQ] {time.strftime('%d/%b/%Y %H:%M:%S')} {client_ip} {method} {log_path} UA={user_agent}"
        if should_log_request(path):
            log_request_line(line)

        if method not in ("GET", "HEAD"):
            response = build_http_response(
                405,
                "text/plain; charset=utf-8",
                "Method not allowed",
                ["Allow: GET, HEAD"],
                include_body,
            )
        elif path == "/healthz":
            response = build_http_response(200, "text/plain; charset=utf-8", "ok", include_body=include_body)
        else:
            allowed, retry_after, message = check_rate_limit(client_ip)
            if not allowed:
                response = build_json_response(
                    429,
                    {"error": "rate_limited", "message": message, "retryAfter": retry_after},
                    [f"Retry-After: {retry_after}"],
                    include_body,
                )
            elif path in ("/", "/custom.xaml"):
                response = build_http_response(
                    200,
                    "application/xml; charset=utf-8",
                    make_home_xaml(get_status(), get_github_status(), get_blog_status()),
                    include_body=include_body,
                )
            elif path == "/api/status":
                response = build_json_response(
                    200,
                    api_status_payload(),
                    include_body=include_body,
                )
            else:
                response = build_http_response(404, "text/plain; charset=utf-8", "Not found", include_body=include_body)

        client_socket.sendall(response)
    except BadRequestError as error:
        append_runtime_log(f"[BAD_REQUEST] {clean_client_ip(client_address[0])} {error}")
        try:
            client_socket.sendall(build_http_response(400, "text/plain; charset=utf-8", "Bad request"))
        except Exception:
            pass
    except socket.timeout:
        append_runtime_log(f"[TIMEOUT] {client_address[0]} request timed out")
    except Exception:
        error_text = traceback.format_exc()
        append_runtime_log(error_text)
        if not is_client_disconnect_error(error_text):
            print(error_text)
            try:
                client_socket.sendall(
                    build_http_response(500, "text/plain; charset=utf-8", "Internal server error")
                )
            except Exception:
                pass
    finally:
        if warnings_token is not None:
            REQUEST_CONFIG_WARNINGS.reset(warnings_token)
        if config_token is not None:
            REQUEST_CONFIG.reset(config_token)
        try:
            client_socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        client_socket.close()


def handle_limited_http_request(client_socket, client_address):
    try:
        handle_http_request(client_socket, client_address)
    finally:
        REQUEST_SEMAPHORE.release()


def reject_busy_client(client_socket):
    try:
        client_socket.settimeout(config_int("requestTimeoutSeconds", 10, 1, 120))
        client_socket.sendall(
            build_http_response(503, "text/plain; charset=utf-8", "Server busy, try again later")
        )
    except Exception:
        pass
    finally:
        client_socket.close()


def serve_http_forever(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((host, port))
        server_socket.listen(20)
        append_runtime_log(f"Listening on {host}:{port}")
        while True:
            client_socket, client_address = server_socket.accept()
            if not REQUEST_SEMAPHORE.acquire(blocking=False):
                reject_busy_client(client_socket)
                continue
            try:
                threading.Thread(
                    target=handle_limited_http_request,
                    args=(client_socket, client_address),
                    daemon=True,
                ).start()
            except Exception:
                REQUEST_SEMAPHORE.release()
                client_socket.close()
                raise


def main():
    host = CONFIG["listenHost"]
    port = config_int("listenPort", DEFAULT_CONFIG["listenPort"], 1, 65535)
    monitor = threading.Thread(target=github_monitor_loop, name="sscfg-github-monitor", daemon=True)
    monitor.start()
    blog_monitor = threading.Thread(target=blog_monitor_loop, name="bjd-blog-monitor", daemon=True)
    blog_monitor.start()
    print(f"PCL2 homepage server version: {APP_VERSION}")
    print(f"PCL2 custom home XAML: http://{host}:{port}/Custom.xaml")
    print(f"Minecraft status JSON: http://{host}:{port}/api/status")
    print(f"Minecraft target: {CONFIG['minecraftHost']}:{CONFIG['minecraftPort']}")
    for warning in CONFIG_WARNINGS:
        message = f"[CONFIG] {warning}"
        print(message)
        append_runtime_log(message)
    try:
        serve_http_forever(host, port)
    finally:
        GITHUB_STOP.set()
        BLOG_STOP.set()


def config_check_result():
    if CONFIG_WARNINGS:
        lines = ["config.json has problems:"]
        lines.extend(f"- {warning}" for warning in CONFIG_WARNINGS)
        return 1, lines
    return 0, ["config.json OK"]


def check_config_cli():
    exit_code, lines = config_check_result()
    for line in lines:
        print(line)
    return exit_code


if __name__ == "__main__":
    if "--check-config" in sys.argv:
        raise SystemExit(check_config_cli())
    main()

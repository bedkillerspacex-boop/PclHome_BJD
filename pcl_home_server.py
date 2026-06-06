import json
import html as html_lib
import math
import os
import re
import socket
import ssl
import struct
import threading
import time
import traceback
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote, urlparse
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
    "joinHint": "\u628a play.bjd-mc.com \u52a0\u5165\u591a\u4eba\u6e38\u620f\u5373\u53ef\u8fdb\u5165\u5e03\u5409\u5c9b\u3002",
    "refreshSeconds": 30,
    "githubName": "SsCfg",
    "sscfgIndexUrl": "https://raw.githubusercontent.com/bedkillerspacex-boop/SsCfg/master/index.json",
    "githubProxyPrefix": "",
    "sscfgRefreshSeconds": 900,
    "blogUrl": "https://mcbjd.net/blog/",
    "blogRefreshSeconds": 900,
    "rateLimitEnabled": False,
    "rateLimitWindowSeconds": 5,
    "rateLimitMaxRequests": 3,
    "trustProxyHeaders": False,
    "verifyHttps": False,
}
APP_VERSION = "1.0.0"

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
STATUS_CACHE = {"status": None, "expires_at": 0}
LATENCY_HISTORY = deque()
GITHUB_CACHE = {"status": None, "expires_at": 0}
GITHUB_LOCK = threading.Lock()
GITHUB_STOP = threading.Event()
BLOG_CACHE = {"status": None, "expires_at": 0}
BLOG_LOCK = threading.Lock()
BLOG_STOP = threading.Event()
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_HITS = defaultdict(deque)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        return DEFAULT_CONFIG.copy()
    with open(CONFIG_PATH, "r", encoding="utf-8") as file:
        user_config = json.load(file)
    config = DEFAULT_CONFIG.copy()
    config.update(user_config)
    if not config.get("minecraftConnectHosts"):
        config["minecraftConnectHosts"] = [config["minecraftHost"]]
    return config


CONFIG = load_config()


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
    with socket.create_connection((connect_host, port), timeout=5) as sock:
        sock.settimeout(5)
        sock.sendall(make_handshake(display_host, port))
        sock.sendall(make_packet(0x00))

        _packet_length = read_varint(sock)
        packet_id = read_varint(sock)
        if packet_id != 0x00:
            raise ValueError("invalid status packet")

        json_length = read_varint(sock)
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


def ping_minecraft_server():
    display_host = CONFIG["minecraftHost"]
    port = int(CONFIG["minecraftPort"])
    errors = []

    for connect_host in CONFIG.get("minecraftConnectHosts", [display_host]):
        try:
            return ping_one_host(connect_host, display_host, port)
        except Exception as error:
            errors.append(f"{connect_host}: {error}")

    return {
        "online": False,
        "error": "; ".join(errors),
        "checkedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def get_status():
    now = time.time()
    if STATUS_CACHE["status"] and now < STATUS_CACHE["expires_at"]:
        return STATUS_CACHE["status"]

    status = ping_minecraft_server()
    record_latency_sample(status)
    enrich_latency_metrics(status)
    STATUS_CACHE["status"] = status
    STATUS_CACHE["expires_at"] = now + max(5, int(CONFIG.get("refreshSeconds", 30)))
    return status


def record_latency_sample(status):
    now = time.time()
    LATENCY_HISTORY.append(
        {
            "time": now,
            "online": bool(status.get("online")),
            "latency": int(status.get("latencyMs") or 0),
        }
    )
    while LATENCY_HISTORY and now - LATENCY_HISTORY[0]["time"] > 180:
        LATENCY_HISTORY.popleft()


def enrich_latency_metrics(status):
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
    now = time.time()
    with GITHUB_LOCK:
        if GITHUB_CACHE["status"] and now < GITHUB_CACHE["expires_at"]:
            return GITHUB_CACHE["status"]

    return refresh_github_status()


def refresh_github_status():
    now = time.time()
    name = CONFIG.get("githubName", "SsCfg")
    index_url = CONFIG.get("sscfgIndexUrl", "").strip()

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
        GITHUB_CACHE["expires_at"] = now + max(10, int(CONFIG.get("sscfgRefreshSeconds", 900)))
    return status


def github_monitor_loop():
    while not GITHUB_STOP.is_set():
        refresh_github_status()
        interval = max(10, int(CONFIG.get("sscfgRefreshSeconds", 900)))
        GITHUB_STOP.wait(interval)


def proxied_url(url):
    prefix = CONFIG.get("githubProxyPrefix", "").strip()
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
    now = time.time()
    with BLOG_LOCK:
        if BLOG_CACHE["status"] and now < BLOG_CACHE["expires_at"]:
            return BLOG_CACHE["status"]

    return refresh_blog_status()


def refresh_blog_status():
    now = time.time()
    blog_url = CONFIG.get("blogUrl", "").strip()

    try:
        if not blog_url:
            raise ValueError("blogUrl is empty")
        request = Request(blog_url, headers={"User-Agent": "PclHome-BJD"})
        with open_url(request, timeout=8) as response:
            html_text = response.read().decode("utf-8", errors="replace")
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
        BLOG_CACHE["expires_at"] = now + max(60, int(CONFIG.get("blogRefreshSeconds", 900)))
    return status


def blog_monitor_loop():
    while not BLOG_STOP.is_set():
        refresh_blog_status()
        interval = max(60, int(CONFIG.get("blogRefreshSeconds", 900)))
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


def github_contents_url(repo, file_path):
    if not repo:
        return ""
    clean_path = file_path.strip("/")
    if not clean_path:
        return f"https://api.github.com/repos/{repo}/contents"
    quoted_path = "/".join(quote(part) for part in clean_path.split("/"))
    return f"https://api.github.com/repos/{repo}/contents/{quoted_path}"


def fetch_json(url):
    request = Request(url, headers={"User-Agent": "PclHome-BJD", "Accept": "application/vnd.github+json"})
    with open_url(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_github_file_text(contents_payload):
    if not isinstance(contents_payload, dict):
        raise ValueError("config path is not a file")
    download_url = contents_payload.get("download_url")
    if not download_url:
        raise ValueError("config file has no download URL")
    request = Request(download_url, headers={"User-Agent": "PclHome-BJD"})
    with open_url(request, timeout=8) as response:
        return response.read().decode("utf-8")


def open_url(request, timeout):
    context = ssl._create_unverified_context()
    if not CONFIG.get("verifyHttps", False):
        return urlopen(request, timeout=timeout, context=context)
    try:
        return urlopen(request, timeout=timeout)
    except ssl.SSLError:
        return urlopen(request, timeout=timeout, context=context)


def validate_config_text(config_path, text):
    stripped = text.strip()
    if not stripped:
        return False, "\u914d\u7f6e\u6587\u4ef6\u4e3a\u7a7a"
    if config_path.lower().endswith(".json"):
        json.loads(stripped)
        return True, "JSON OK"
    return True, "\u6587\u4ef6\u975e\u7a7a"


def xml_escape(value):
    return (
        str(value if value is not None else "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def get_client_ip(handler):
    if CONFIG.get("trustProxyHeaders"):
        forwarded_for = handler.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        real_ip = handler.headers.get("X-Real-IP", "")
        if real_ip:
            return real_ip.strip()
    return handler.client_address[0]


def check_rate_limit(client_ip):
    if not CONFIG.get("rateLimitEnabled", False):
        return True, 0, ""

    now = time.time()
    window_seconds = max(1, int(CONFIG.get("rateLimitWindowSeconds", 5)))
    max_requests = max(1, int(CONFIG.get("rateLimitMaxRequests", 3)))

    with RATE_LIMIT_LOCK:
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
        latency = int(status.get("latencyMs") or 999)
        if latency <= 80:
            score += 20
        elif latency <= 150:
            score += 15
        elif latency <= 300:
            score += 8

        max_players = int(status.get("playersMax") or 0)
        online_players = int(status.get("playersOnline") or 0)
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


def make_home_xaml(status, github_status, blog_status):
    address = f"{CONFIG['minecraftHost']}:{CONFIG['minecraftPort']}"
    online = status.get("online")
    state_text = "\u5728\u7ebf" if online else "\u79bb\u7ebf"
    avg_latency = status.get("latencyAverageMs")
    stability = int(status.get("latencyStability") or 0)
    latency_text = f"{avg_latency} ms / {stability}%" if avg_latency is not None else "\u65e0\u6cd5\u8fde\u63a5"
    player_text = str(status.get("playersOnline", 0)) if online else "\u672a\u77e5"
    motd_text = (
        status.get("motd", "\u670d\u52a1\u5668\u5728\u7ebf")
        if online
        else f"\u670d\u52a1\u5668\u6682\u65f6\u65e0\u6cd5\u8fde\u63a5\uff1a{status.get('error', '\u672a\u77e5\u9519\u8bef')}"
    )
    version_text = "1.20.5-6"
    accent = "#2FA866" if online else "#D83B01"
    github_online = github_status.get("online")
    github_state = "\u6b63\u5e38" if github_online else "\u5f02\u5e38"
    github_accent = "#2FA866" if github_online else "#D83B01"
    latest_pack = github_status.get("latestPack") or {}
    github_detail = (
        f"\u5171 {github_status.get('packCount', 0)} \u4e2a CFG\uff0c\u6700\u65b0\uff1a{latest_pack.get('name', '\u672a\u77e5')}  {latest_pack.get('version', '')}  {latest_pack.get('date', '')}  by {latest_pack.get('author', '\u672a\u77e5')}"
        if github_online
        else f"index.json: {github_status.get('configMessage') or github_status.get('error', '\u65e0\u6cd5\u8fde\u63a5')}"
    )
    blog_online = blog_status.get("online")
    blog_state = "\u5df2\u83b7\u53d6" if blog_online else "\u5f02\u5e38"
    blog_accent = "#2FA866" if blog_online else "#D83B01"
    blog_detail = f"{blog_status.get('date', '\u672a\u77e5')}  {blog_status.get('title', '\u672a\u77e5\u66f4\u65b0')}  {blog_status.get('summary', '')}"
    health = calculate_health(status, github_status, blog_status)
    health_ring = make_health_ring_xaml(health)

    return f"""<local:MyCard Title="{xml_escape(CONFIG['serverName'])}" Margin="0,0,0,12">
  <StackPanel Margin="20,22,18,6">
    <TextBlock Text="v{xml_escape(APP_VERSION)}" HorizontalAlignment="Right" Foreground="{{DynamicResource ColorBrush3}}" FontSize="11" Margin="0,-8,0,2" />
    <Grid Margin="0,2,0,2">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="62" />
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0" Margin="6,6,8,0">
        <StackPanel Orientation="Horizontal" Margin="0,0,0,4">
          <TextBlock Text="{xml_escape(address)}" FontSize="16" FontWeight="Bold" Foreground="{{DynamicResource ColorBrush1}}" />
          <Border Background="{accent}" CornerRadius="3" Padding="7,1" Margin="8,2,0,0" VerticalAlignment="Center">
            <TextBlock Text="{xml_escape(state_text)}" Foreground="White" FontWeight="Bold" FontSize="11" />
          </Border>
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
      <StackPanel Grid.Column="0">
        <TextBlock Text="\u5728\u7ebf\u4eba\u6570" Foreground="{{DynamicResource ColorBrush3}}" />
        <TextBlock Text="{xml_escape(player_text)}" FontSize="20" FontWeight="Bold" />
      </StackPanel>
      <StackPanel Grid.Column="1">
        <TextBlock Text="\u5e03\u5409\u5c9b\u5ef6\u8fdf\u7a33\u5b9a\u6027" Foreground="{{DynamicResource ColorBrush3}}" />
        <TextBlock Text="{xml_escape(latency_text)}" FontSize="20" FontWeight="Bold" />
      </StackPanel>
      <StackPanel Grid.Column="2">
        <TextBlock Text="\u534f\u8bae\u7248\u672c" Foreground="{{DynamicResource ColorBrush3}}" />
        <TextBlock Text="{xml_escape(version_text)}" FontSize="20" FontWeight="Bold" TextTrimming="CharacterEllipsis" />
      </StackPanel>
    </Grid>
    <Grid Margin="0,6,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="Auto" />
      </Grid.ColumnDefinitions>
      <StackPanel Grid.Column="0">
        <StackPanel Orientation="Horizontal" Margin="0,0,0,2">
          <TextBlock Text="\u5e03\u5409\u5c9b\u6700\u8fd1\u66f4\u65b0" FontWeight="Bold" Margin="0,0,8,0" />
          <Border Background="{blog_accent}" CornerRadius="3" Padding="8,2">
            <TextBlock Text="{xml_escape(blog_state)}" Foreground="White" FontWeight="Bold" FontSize="12" />
          </Border>
        </StackPanel>
        <TextBlock Text="{xml_escape(blog_detail)}" Foreground="{{DynamicResource ColorBrush3}}" TextWrapping="Wrap" />
      </StackPanel>
      <local:MyButton Grid.Column="1" Text="\u5237\u65b0" Width="64" Height="30" HorizontalAlignment="Right" VerticalAlignment="Bottom" Margin="12,0,0,0" EventType="\u5237\u65b0\u4e3b\u9875" ToolTip="\u91cd\u65b0\u52a0\u8f7d\u4e3b\u9875\u5e76\u91cd\u65b0\u67e5\u8be2 Minecraft MOTD \u548c GitHub \u72b6\u6001\u3002" />
    </Grid>
  </StackPanel>
</local:MyCard>

<local:MyCard Title="SsCfg" Margin="0,0,0,15">
  <StackPanel Margin="22,34,20,12">
    <Grid Margin="0,-4,0,8">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="*" />
        <ColumnDefinition Width="Auto" />
      </Grid.ColumnDefinitions>
      <TextBlock Text="{xml_escape(github_status.get('name', 'SsCfg'))}" FontSize="16" FontWeight="Bold" Foreground="{{DynamicResource ColorBrush1}}" />
      <Border Grid.Column="1" Background="{github_accent}" CornerRadius="3" Padding="8,2" Margin="12,0,0,0" VerticalAlignment="Top">
        <TextBlock Text="{xml_escape(github_state)}" Foreground="White" FontWeight="Bold" FontSize="12" />
      </Border>
    </Grid>
    <TextBlock Text="{xml_escape(github_detail)}" TextWrapping="Wrap" />
  </StackPanel>
</local:MyCard>

<local:MyCard Title="\u5e7f\u544a" Margin="0,0,0,15">
  <StackPanel Margin="22,30,20,10">
    <Border Background="#F4F8FF" CornerRadius="6" Padding="14,10">
      <TextBlock Text="Kam\u5ba2\u6237\u7aef\uff01\u5e03\u5409\u5c9b\u6700\u5f3a\u516c\u76ca\u5ba2\u6237\u7aef \u8fdb\u7fa41106439778" FontSize="15" FontWeight="Bold" TextWrapping="Wrap" />
    </Border>
  </StackPanel>
</local:MyCard>"""


class Handler(BaseHTTPRequestHandler):
    def log_request_line(self):
        client_ip = get_client_ip(self)
        user_agent = self.headers.get("User-Agent", "-")
        print(f"[REQ] {self.log_date_time_string()} {client_ip} {self.command} {self.path} UA={user_agent}")

    def send_body(self, status_code, content_type, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            return

    def send_rate_limited(self, retry_after, message):
        body = json.dumps(
            {"error": "rate_limited", "message": message, "retryAfter": retry_after},
            ensure_ascii=False,
            indent=2,
        )
        encoded = body.encode("utf-8")
        try:
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Retry-After", str(retry_after))
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            self.wfile.flush()
        except (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self):
        try:
            self.log_request_line()
            path = urlparse(self.path).path.lower()
            if path in ("/", "/custom.xaml"):
                self.send_body(200, "application/xml; charset=utf-8", make_home_xaml(get_status(), get_github_status(), get_blog_status()))
                return

            # Rate limiting is disabled by default because PCL2 can issue clustered reload/debug requests.
            allowed, retry_after, message = check_rate_limit(get_client_ip(self))
            if not allowed:
                self.send_rate_limited(retry_after, message)
                return

            if path == "/api/status":
                self.send_body(
                    200,
                    "application/json; charset=utf-8",
                    json.dumps(
                        {"minecraft": get_status(), "github": get_github_status(), "blog": get_blog_status()},
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                return
            self.send_body(404, "text/plain; charset=utf-8", "Not found")
        except Exception:
            if is_client_disconnect_error(traceback.format_exc()):
                return
            error_text = traceback.format_exc()
            print(error_text)
            try:
                self.send_body(500, "text/plain; charset=utf-8", error_text)
            except Exception:
                if not is_client_disconnect_error(traceback.format_exc()):
                    raise

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


def is_client_disconnect_error(error_text):
    return (
        "ConnectionAbortedError" in error_text
        or "BrokenPipeError" in error_text
        or "WinError 10053" in error_text
        or "WinError 10054" in error_text
    )


def main():
    host = CONFIG["listenHost"]
    port = int(CONFIG["listenPort"])
    server = HTTPServer((host, port), Handler)
    monitor = threading.Thread(target=github_monitor_loop, name="sscfg-github-monitor", daemon=True)
    monitor.start()
    blog_monitor = threading.Thread(target=blog_monitor_loop, name="bjd-blog-monitor", daemon=True)
    blog_monitor.start()
    print(f"PCL2 homepage server version: {APP_VERSION}")
    print(f"PCL2 custom home XAML: http://{host}:{port}/Custom.xaml")
    print(f"Minecraft status JSON: http://{host}:{port}/api/status")
    print(f"Minecraft target: {CONFIG['minecraftHost']}:{CONFIG['minecraftPort']}")
    try:
        server.serve_forever()
    finally:
        GITHUB_STOP.set()
        BLOG_STOP.set()
        server.server_close()


if __name__ == "__main__":
    main()

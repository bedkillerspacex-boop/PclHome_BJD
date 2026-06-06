# PCL2 BuJiDao Custom Home

This is a Python-only PCL2 custom homepage server.

It does not show browser page data. It performs a Minecraft Java status ping, reads the server MOTD/player count/version/latency, then returns PCL2 XAML from `/Custom.xaml`.

The homepage also checks the configured GitHub repo in `config.json`:

```json
"sscfgIndexUrl": "https://raw.githubusercontent.com/bedkillerspacex-boop/SsCfg/master/index.json",
"githubProxyPrefix": "https://gh-proxy.com/",
"sscfgRefreshSeconds": 900
```

`githubProxyPrefix` is optional. When set, the server tries the proxy first and falls back to the original GitHub raw URL if the proxy fails. `sscfgRefreshSeconds` is 900 seconds, so SsCfg is checked every 15 minutes. The server reads `index.json`, counts `packs`, and displays the latest CFG.

The homepage also fetches the latest BuJiDao update from:

```json
"blogUrl": "https://mcbjd.net/blog/",
"blogRefreshSeconds": 900
```

It skips the welcome post and displays the first post whose title/text contains "更新日志".

Remote GitHub/blog responses are capped to avoid large unexpected downloads:

```json
"remoteFetchMaxBytes": 1048576
```

The homepage includes a small PCL2 refresh button:

```xml
<local:MyButton Text="Refresh" EventType="刷新主页" />
```

Clicking it reloads `/Custom.xaml`, so the Python server can query the Minecraft MOTD again.

## Start

```powershell
cd E:\DESKTOP\project\PclHome_BJD
.\start.ps1
```

If PowerShell blocks the script:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

Or run Python directly:

```powershell
python .\pcl_home_server.py
```

Or use the self-update launcher:

```powershell
.\update-and-run.cmd
```

It downloads the latest zip from GitHub through `gh-proxy`, extracts it over the current folder, then starts the server.

## PCL2 URL

Set PCL2 custom homepage URL to:

```text
http://your-server-ip:3000/Custom.xaml
```

Status debug URL:

```text
http://your-server-ip:3000/api/status
```

## Config

Edit `config.json`.

`minecraftHost` is the address displayed in PCL2 and sent in the Minecraft handshake.

`minecraftConnectHosts` are fallback hosts used for the actual TCP connection. This is useful when DNS for `play.bjd-mc.com` fails on the cloud server.
Each entry must be a string. Empty or non-string entries are skipped and shown
as configuration warnings.

`minecraftPingTimeoutSeconds` controls the per-host Minecraft status timeout.
If all fallback hosts are slow to fail, lowering this reduces worst-case refresh
time.

Open port `3000` in Windows Firewall and the cloud security group.

## Homepage Maintenance

Most homepage text is in the `home` block of `config.json`. The server reloads
`config.json` automatically when `/Custom.xaml` or `/api/status` is requested,
so changing homepage text does not require restarting Python.
Process startup settings such as `listenHost`, `listenPort`, and
`maxRequestThreads` still require a restart.

Dummy-proof edit flow:

1. Edit the `home` block in `config.json`.
2. Save the file.
3. Refresh the PCL homepage.
4. If the edit is valid, the new homepage appears immediately.
5. If JSON is broken, the page keeps the last valid homepage and shows a
   configuration warning card.

Change these fields first; do not edit Python unless you are changing behavior:

```json
"home": {
  "displayVersion": "1.20.5-6",
  "playersLabel": "在线人数",
  "latencyLabel": "布吉岛延迟稳定性",
  "versionLabel": "协议版本",
  "latestUpdateTitle": "布吉岛最近更新",
  "refreshButtonText": "刷新",
  "sscfgCardTitle": "SsCfg",
  "showAppVersion": true,
  "customCards": [
    {
      "enabled": true,
      "title": "广告",
      "text": "Kam客户端！布吉岛最强公益客户端 进群1106439778",
      "background": "#F4F8FF",
      "fontSize": 15,
      "bold": true
    }
  ]
}
```

Set a card's `enabled` to `false` to hide it. Add another object to
`customCards` to show another announcement card. Colors must use `#RRGGBB`.

After editing `config.json`, check it after saving:

```powershell
python .\pcl_home_server.py --check-config
```

It prints `config.json OK` when the file can be loaded. If JSON is broken, fix
the reported line and column first. It also reports homepage maintenance
warnings such as invalid card colors, empty enabled cards, or `customCards`
being the wrong JSON type. Remote URLs such as `sscfgIndexUrl`, `blogUrl`, and
`githubProxyPrefix` are also checked so typos are visible before restart or
manual testing.

Runtime reload state is also visible in:

```text
http://your-server-ip:3000/api/status
```

In that response, `config.restartRequired` becomes `true` when a startup-only
setting was changed in `config.json` but the running process is still using the
old value. Check `config.restartRequiredDetails` for the exact fields and
restart Python after changing `listenHost`, `listenPort`, or
`maxRequestThreads`.

The same response also includes `cache.minecraft`, `cache.github`, and
`cache.blog`. Their `state`, `refreshing`, and `expiresInSeconds` fields show
whether the homepage is using fresh data, stale data, or a background refresh.

## Rate limit

Each client IP can be limited:

```json
"rateLimitEnabled": false,
"rateLimitWindowSeconds": 5,
"rateLimitMaxRequests": 3
```

When enabled, homepage and API requests over the limit return HTTP `429` with a
`Retry-After` header. `/healthz` is not rate-limited so external monitors can
continue to check process health.

The HTTP server also has basic slow-client and thread-exhaustion protection:

```json
"requestTimeoutSeconds": 10,
"maxRequestThreads": 64
```

Request logging can be reduced if the server is noisy:

```json
"requestLoggingEnabled": true,
"logHealthChecks": false,
"runtimeLogMaxBytes": 1048576
```

`/healthz` is not written to `server.runtime.log` by default. When
`server.runtime.log` reaches `runtimeLogMaxBytes`, it is rotated to
`server.runtime.log.1`. Set `runtimeLogMaxBytes` to `0` to disable rotation.

If the service runs behind Nginx, CDN, or another reverse proxy, set this only after the proxy correctly forwards the real client IP:

```json
"trustProxyHeaders": true
```

If your Windows server reports `CERTIFICATE_VERIFY_FAILED` when reading GitHub or the blog, keep:

```json
"verifyHttps": false
```

Set it to `true` only after Python can verify HTTPS certificates correctly.

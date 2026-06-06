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

Open port `3000` in Windows Firewall and the cloud security group.

## Rate limit

Each client IP is limited by default:

```json
"rateLimitEnabled": false,
"rateLimitWindowSeconds": 5,
"rateLimitMaxRequests": 3
```

Requests over the limit return HTTP `429` with a `Retry-After` header.

If the service runs behind Nginx, CDN, or another reverse proxy, set this only after the proxy correctly forwards the real client IP:

```json
"trustProxyHeaders": true
```

If your Windows server reports `CERTIFICATE_VERIFY_FAILED` when reading GitHub or the blog, keep:

```json
"verifyHttps": false
```

Set it to `true` only after Python can verify HTTPS certificates correctly.

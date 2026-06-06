$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) {
    $Python = Get-Command py -ErrorAction SilentlyContinue
}

if (-not $Python) {
    Write-Host "未找到 Python。请先安装 Python 3.9+，并勾选 Add Python to PATH。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

Write-Host "正在启动 PCL2 布吉岛主页服务..." -ForegroundColor Cyan
Write-Host "PCL2 自定义主页地址：http://你的云服务器IP:3000/Custom.xaml" -ForegroundColor Yellow
Write-Host "状态检查地址：http://你的云服务器IP:3000/api/status" -ForegroundColor Yellow
Write-Host "按 Ctrl+C 停止服务。"
& $Python.Source .\pcl_home_server.py

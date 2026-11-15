@echo off
rem 安徽省法律服务招标信息收集和推送skill · 启动脚本（Windows）
rem 自动寻找 Python 3.9+ 并运行 radar.py；找不到时打印安装方法，退出码 127。
rem 可用环境变量 RADAR_PYTHON 指定 python.exe 路径。
setlocal EnableDelayedExpansion
set "SCRIPT=%~dp0radar.py"
set "CHECK=import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"

if defined RADAR_PYTHON (
  "%RADAR_PYTHON%" -c "%CHECK%" >nul 2>&1
  if !errorlevel! equ 0 (
    "%RADAR_PYTHON%" -X utf8 "%SCRIPT%" %*
    exit /b !errorlevel!
  )
)

for %%C in (py python python3) do (
  %%C -c "%CHECK%" >nul 2>&1
  if !errorlevel! equ 0 (
    %%C -X utf8 "%SCRIPT%" %*
    exit /b !errorlevel!
  )
)

set "WB=%LOCALAPPDATA%\Programs\WorkBuddy\resources\vendor\python\python.exe"
if exist "%WB%" (
  "%WB%" -c "%CHECK%" >nul 2>&1
  if !errorlevel! equ 0 (
    "%WB%" -X utf8 "%SCRIPT%" %*
    exit /b !errorlevel!
  )
)

chcp 65001 >nul
echo [PYTHON_NOT_FOUND] 未找到 Python 3.9 或更高版本，无法运行本 skill 的脚本。 1>&2
echo. 1>&2
echo 安装方法（任选其一，装好后重新打开终端或 Agent）： 1>&2
echo   1. 在 PowerShell 或命令提示符运行：winget install Python.Python.3.12 1>&2
echo   2. 从 https://www.python.org/downloads/ 下载安装包，安装时勾选「Add python.exe to PATH」 1>&2
echo   3. 在 Microsoft Store 搜索 Python 3.12 安装 1>&2
echo   已有 Python 但不在 PATH 中：设置环境变量 RADAR_PYTHON 为 python.exe 的完整路径 1>&2
echo. 1>&2
echo 不想安装：可以让 Agent 使用「简易模式」（见 references\no-python.md）， 1>&2
echo 只用 Agent 自带的联网搜索，覆盖面和可靠性较低，且不能自动推送。 1>&2
exit /b 127

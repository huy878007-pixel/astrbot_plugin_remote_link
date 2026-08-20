@echo off
rem 云信互联 本地代理启动脚本（双击运行）
rem 优先运行打包好的 YunxinAgent.exe（配置放 exe 同目录），否则回退到 Python 源码模式
cd /d "%~dp0"

set "EXE="
if exist "%~dp0..\dist\YunxinAgent.exe" set "EXE=%~dp0..\dist\YunxinAgent.exe"
if exist "%~dp0YunxinAgent.exe" set "EXE=%~dp0YunxinAgent.exe"

if defined EXE (
  pushd "%~dp0"
  for %%F in ("%EXE%") do pushd "%%~dpF"
  echo ============================================
  echo   云信互联 本地代理
  echo   按 Ctrl+C 退出；断线会自动重连
  echo   配置文件: agent_config.json（需与 exe 同目录）
  echo ============================================
  "%EXE%"
  popd
  popd
  goto :end
)

echo ============================================
echo   云信互联 本地代理（Python 模式）
echo   按 Ctrl+C 退出；断线会自动重连
echo ============================================
python local_agent.py -c agent_config.json
if errorlevel 1 (
  echo.
  echo 启动失败，请检查：
  echo   1. 是否已安装依赖: pip install aiohttp
  echo   2. agent_config.json 是否存在且填写正确
  echo.
  pause
)

:end

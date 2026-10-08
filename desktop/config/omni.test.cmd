@echo off
rem OmniAPI-Test command line (the desktop TEST build only; package.ps1 -TestIdentity ships it as omni.cmd).
rem Same as the released omni.cmd, but everything defaults to the test build: port 7939, data home
rem %USERPROFILE%\.omniapi-test, works under it, offline with fake providers, its own logon entry name
rem and a stand-in Claude config. Values already set in the environment win (installer tests set them).
setlocal
if not defined OMNIAPI_DEFAULT_PORT set "OMNIAPI_DEFAULT_PORT=7939"
if not defined OMNIAPI_HOME set "OMNIAPI_HOME=%USERPROFILE%\.omniapi-test"
if not defined STORAGE__BASE_PATH set "STORAGE__BASE_PATH=%OMNIAPI_HOME%\works"
if not defined OMNIAPI_DEV set "OMNIAPI_DEV=1"
if not defined OMNIAPI_OFFLINE set "OMNIAPI_OFFLINE=1"
if not defined OMNIAPI_CLAUDE_CONFIG set "OMNIAPI_CLAUDE_CONFIG=%OMNIAPI_HOME%\claude.json"
set "OMNIAPI_DESKTOP_RUN_VALUE=OmniAPI-Test"
"%~dp0python\python.exe" -I -m omniapi_mcp.cli %*
exit /b %ERRORLEVEL%

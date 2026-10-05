@echo off
rem OmniAPI command line for the desktop install: omni status, omni works, omni chat, ...
rem Runs the Python that ships in this folder (-I: ignores PYTHON* variables and the current folder).
rem Not added to PATH; call it by its full path. The service it talks to is the one the desktop app runs.
"%~dp0python\python.exe" -I -m omniapi_mcp.cli %*
exit /b %ERRORLEVEL%

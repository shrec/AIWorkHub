@echo off
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\runtime\aiworkhub\app_server_mux.py" (
    set "PYTHONPATH=%ROOT%\runtime;%PYTHONPATH%"
) else if exist "%ROOT%\src\aiworkhub\app_server_mux.py" (
    set "PYTHONPATH=%ROOT%\src;%PYTHONPATH%"
) else (
    echo AIWorkHub mux runtime is missing
    exit /b 1
)
python -m aiworkhub.app_server_mux %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%

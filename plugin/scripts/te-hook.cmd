@echo off
setlocal
set "DIR=%~dp0"
set "PYTHONUTF8=1"

:: Explicit TE_PYTHON
if defined TE_PYTHON (
    if exist "%TE_PYTHON%" (
        "%TE_PYTHON%" "%DIR%te_bootstrap.py" hook %*
        exit /b %ERRORLEVEL%
    )
)

:: Find python.exe on PATH, skipping WindowsApps store alias
for /f "tokens=*" %%i in ('where python.exe 2^>nul') do (
    echo %%i | findstr /i /c:"WindowsApps" >nul
    if errorlevel 1 (
        "%%i" "%DIR%te_bootstrap.py" hook %*
        exit /b %ERRORLEVEL%
    )
)

:: Try py.exe (-3) launcher
where py.exe >nul 2>&1
if %ERRORLEVEL% equ 0 (
    py -3 "%DIR%te_bootstrap.py" hook %*
    exit /b %ERRORLEVEL%
)

:: Try python3.exe on PATH
for /f "tokens=*" %%i in ('where python3.exe 2^>nul') do (
    echo %%i | findstr /i /c:"WindowsApps" >nul
    if errorlevel 1 (
        "%%i" "%DIR%te_bootstrap.py" hook %*
        exit /b %ERRORLEVEL%
    )
)

:: Fail open: drain stdin and output empty JSON
for /f "tokens=*" %%a in ('more') do rem
echo {}
exit /b 0

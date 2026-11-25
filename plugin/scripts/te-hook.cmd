@echo off
setlocal EnableDelayedExpansion
set "DIR=%~dp0"
set "PYTHONUTF8=1"

:: 1. TE_PYTHON is the person's own choice: it runs as named when it is there, as a file or as
::    a command on PATH. One that is not there is passed over for the probes below, as the sh
::    launchers do, so a stale setting cannot keep the hook from answering. The name is read with
::    delayed expansion and run in quotes: a path with spaces or parentheses
::    ("C:\Program Files (x86)\...") stays one word and cannot end an `if` block early.
if not defined TE_PYTHON goto :probe
set "TE_PY=%TE_PYTHON:"=%"
if exist "!TE_PY!" (
    set "RUN_PY="!TE_PY!""
    goto :run_hook
)
where /q "!TE_PY!" >nul 2>&1
if !ERRORLEVEL! equ 0 (
    set "RUN_PY="!TE_PY!""
    goto :run_hook
)

:probe

:: Probe script: exits 0 if >= 3.10, exits 1 if older
set "PROBE=import sys; sys.exit(sys.version_info < (3, 10))"
set "OLD_PY="

:: 2. Probe py.exe (-3 launcher)
where py.exe >nul 2>&1
if !ERRORLEVEL! equ 0 (
    py -3 -E -S -c "%PROBE%" >nul 2>&1
    if !ERRORLEVEL! equ 0 (
        set "RUN_PY=py -3"
        goto :run_hook
    ) else if !ERRORLEVEL! equ 1 (
        if not defined OLD_PY set "OLD_PY=py -3"
    )
)

:: 3. Probe python.exe on PATH, skipping WindowsApps store alias
for /f "tokens=*" %%i in ('where python.exe 2^>nul') do (
    echo %%~fi | findstr /i /c:"WindowsApps" >nul
    if errorlevel 1 (
        "%%~fi" -E -S -c "%PROBE%" >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            set "RUN_PY="%%~fi""
            goto :run_hook
        ) else if !ERRORLEVEL! equ 1 (
            if not defined OLD_PY set "OLD_PY="%%~fi""
        )
    )
)

:: 4. Probe python3.exe on PATH, skipping WindowsApps store alias
for /f "tokens=*" %%i in ('where python3.exe 2^>nul') do (
    echo %%~fi | findstr /i /c:"WindowsApps" >nul
    if errorlevel 1 (
        "%%~fi" -E -S -c "%PROBE%" >nul 2>&1
        if !ERRORLEVEL! equ 0 (
            set "RUN_PY="%%~fi""
            goto :run_hook
        ) else if !ERRORLEVEL! equ 1 (
            if not defined OLD_PY set "OLD_PY="%%~fi""
        )
    )
)

:: 5. If only older Python was found, hand off to it so bootstrap fails open
if defined OLD_PY (
    set "RUN_PY=%OLD_PY%"
    goto :run_hook
)

:: 6. Fail open: drain stdin and output empty JSON
for /f "tokens=*" %%a in ('more 2^>nul') do rem
echo {}
exit /b 0

:run_hook
%RUN_PY% "%DIR%te_bootstrap.py" hook %*
exit /b !ERRORLEVEL!

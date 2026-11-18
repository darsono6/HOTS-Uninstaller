@echo off
setlocal
cd /d "%SystemRoot%\System32" >nul 2>&1
if not defined TEMP set "TEMP=%SystemRoot%\Temp"

if defined APPDATA (
    set "USER_DIR=%APPDATA%\HOTS Uninstaller"
) else (
    set "USER_DIR=C:\HOTS Uninstaller"
)

set "LOG=%USER_DIR%\error.log"
set "RESULT_FILE=%TEMP%\hots_uninstaller_result_%RANDOM%_%RANDOM%.json"

set "CHOICES_FILE=%~1"
set "PS_CHOICES=%CHOICES_FILE:'=''%"

fltmc >nul 2>&1
if %errorlevel% neq 0 (
    echo [hots_uninstall] Not running as administrator - elevating via UAC... >> "%LOG%"
    powershell -NoProfile -Command "$p = Start-Process -FilePath '%~f0' -ArgumentList '\"%PS_CHOICES%\"' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"
    exit /b
)


REM A file with this name must never exist before the wizard runs (stale or
REM     planted in %TEMP% by someone else). "del" removes a planted link itself,
REM     not the file it points to.
if exist "%RESULT_FILE%" del /f /q "%RESULT_FILE%" >nul 2>&1

echo [hots_uninstall] ---- %DATE% %TIME% ---- >> "%LOG%"
echo [hots_uninstall] Choices file: "%CHOICES_FILE%" >> "%LOG%"
if exist "%CHOICES_FILE%" (
    echo [hots_uninstall] Choices file found. >> "%LOG%"
) else (
    echo [hots_uninstall] WARNING: choices file not found - the wizard choice will not be applied. >> "%LOG%"
)

echo [hots_uninstall] Applying the choice made in the uninstall wizard... >> "%LOG%"
start "" /wait "%~dp0HOTS Uninstaller.exe" --uninstall-apply "%CHOICES_FILE%" "%RESULT_FILE%"
set "CLEANUP_RC=%errorlevel%"
echo [hots_uninstall] Cleanup step finished, exit code %CLEANUP_RC%. >> "%LOG%"

REM The result is trusted only when the program exited with 0 (a non-zero code
REM means it refused the result path or could not write the result).
set "DELETE_SETTINGS=0"
set "DELETE_BACKUPS=0"
if not "%CLEANUP_RC%"=="0" (
    echo [hots_uninstall] WARNING: cleanup step failed - keeping saved data to be safe. >> "%LOG%"
) else (
    if exist "%RESULT_FILE%" (
        type "%RESULT_FILE%" >> "%LOG%"
        echo. >> "%LOG%"
        findstr /c:"\"delete_settings\": true" "%RESULT_FILE%" >nul 2>&1
        if not errorlevel 1 set "DELETE_SETTINGS=1"
        findstr /c:"\"delete_backups\": true" "%RESULT_FILE%" >nul 2>&1
        if not errorlevel 1 set "DELETE_BACKUPS=1"
    ) else (
        echo [hots_uninstall] WARNING: no result from the cleanup step - keeping saved data to be safe. >> "%LOG%"
    )
)
if exist "%RESULT_FILE%" del /f /q "%RESULT_FILE%" >nul 2>&1

if not exist "%USER_DIR%" (
    echo [hots_uninstall] Folder "%USER_DIR%" does not exist - skipping. >> "%LOG%"
    goto :done
)

if "%DELETE_SETTINGS%"=="1" if "%DELETE_BACKUPS%"=="1" (
    echo [hots_uninstall] Removing data folder: "%USER_DIR%" ... >> "%LOG%"
    rmdir "%USER_DIR%" >nul 2>&1
    attrib -R -H -S "%USER_DIR%\*.*" /S /D /L >nul 2>&1
    rmdir /s /q "%USER_DIR%" >nul 2>&1
    if exist "%USER_DIR%" (
        echo [hots_uninstall] WARNING: could not fully remove "%USER_DIR%" >> "%LOG%"
    ) else (
        echo [hots_uninstall] Removed "%USER_DIR%" >> "%LOG%"
    )
    goto :done
)

if "%DELETE_BACKUPS%"=="1" (
    echo [hots_uninstall] Removing registry backups ... >> "%LOG%"
    if exist "%USER_DIR%\RegistryBackups" (
        attrib -R -H -S "%USER_DIR%\RegistryBackups\*.*" /S /D /L >nul 2>&1
        rmdir /s /q "%USER_DIR%\RegistryBackups" >nul 2>&1
    )
    if exist "%USER_DIR%\RegistryBackups" (
        echo [hots_uninstall] WARNING: could not fully remove the registry backups >> "%LOG%"
    ) else (
        echo [hots_uninstall] Removed the registry backups. >> "%LOG%"
    )
) else (
    echo [hots_uninstall] Keeping registry backups. >> "%LOG%"
)

if "%DELETE_SETTINGS%"=="1" (
    echo [hots_uninstall] Removing settings and history ... >> "%LOG%"
    for %%F in ("%USER_DIR%\*") do (
        if /i not "%%~nxF"=="error.log" (
            attrib -R -H -S "%%F" >nul 2>&1
            del /f /q "%%F" >nul 2>&1
        )
    )
    for /d %%D in ("%USER_DIR%\*") do (
        if /i not "%%~nxD"=="RegistryBackups" (
            attrib -R -H -S "%%D\*.*" /S /D /L >nul 2>&1
            rmdir /s /q "%%D" >nul 2>&1
        )
    )
    echo [hots_uninstall] Removed settings and history. >> "%LOG%"
) else (
    echo [hots_uninstall] Keeping settings and history. >> "%LOG%"
)

:done
echo [hots_uninstall] Done. >> "%LOG%"

if "%DELETE_SETTINGS%"=="1" if exist "%USER_DIR%" (
    del /f /q "%LOG%" >nul 2>&1
    rmdir "%USER_DIR%" >nul 2>&1
)

endlocal
exit /b 0

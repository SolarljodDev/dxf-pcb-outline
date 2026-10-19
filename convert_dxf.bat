@echo off
setlocal

if "%~1"=="" (
  echo Drag an EasyEDA PCB PDF file onto this BAT file.
  echo.
  echo Or run:
  echo   convert_dxf.bat "C:\path\board.pdf"
  pause
  exit /b 2
)

if /I not "%~x1"==".pdf" (
  echo Input must be a PDF file. DXF is used only for output to Aspire.
  pause
  exit /b 2
)

set "TOOL_DIR=%~dp0"
set "VENV_DIR=%TOOL_DIR%.venv"
set "PYTHON_EXE=%VENV_DIR%\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo Creating the local Python environment...
  py -3 -m venv "%VENV_DIR%" || python -m venv "%VENV_DIR%" || goto :error
)

"%PYTHON_EXE%" -c "import ezdxf, shapely, pypdf" >nul 2>nul
if errorlevel 1 (
  echo Installing converter libraries...
  "%PYTHON_EXE%" -m pip install --disable-pip-version-check -r "%TOOL_DIR%requirements.txt" || goto :error
)

"%PYTHON_EXE%" "%TOOL_DIR%dxf_to_aspire.py" %*
if errorlevel 1 goto :error

echo.
echo Conversion completed.
pause
exit /b 0

:error
echo.
echo Conversion failed. See the message above.
pause
exit /b 1

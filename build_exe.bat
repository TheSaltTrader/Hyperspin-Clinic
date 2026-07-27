@echo off
REM HyperSpin Clinic - portable folder build (PyInstaller onedir).
REM Result: dist\HyperSpinClinic\  (copy the whole folder; run
REM         HyperSpinClinic.exe inside it)
REM
REM Folder mode is used instead of a single exe on purpose: a onefile
REM build must unpack ~200 MB to a temp dir on EVERY launch (20+ s cold
REM start); the folder layout starts in a couple of seconds and keeps
REM tools + database beside the exe.
REM
REM Windows support: the exe runs on Windows 8.1 / 10 / 11 and newer.
REM NOTE Windows 7: modern Python (3.9+) and several dependencies no
REM longer support Win7. For a Win7 build, use Python 3.8 with pinned
REM legacy versions of the requirements - see README "Windows support".
pip install pyinstaller
pyinstaller --noconfirm --clean --onedir --windowed ^
  --name HyperSpinClinic ^
  --collect-all chromadb ^
  --collect-all onnxruntime ^
  --collect-all tokenizers ^
  --collect-all keyring ^
  --collect-all jsonschema ^
  --collect-all rfc3987_syntax ^
  --collect-all lark ^
  --collect-submodules win32ctypes ^
  --hidden-import keyring.backends.Windows ^
  --hidden-import PIL._tkinter_finder ^
  main.py
echo.
echo Bundling the embedding model (offline first-run indexing)...
if exist "%USERPROFILE%\.cache\chroma\onnx_models\all-MiniLM-L6-v2\onnx" (
  xcopy /E /I /Y /Q "%USERPROFILE%\.cache\chroma\onnx_models\all-MiniLM-L6-v2\onnx" "dist\HyperSpinClinic\onnx_models\all-MiniLM-L6-v2\onnx\"
) else (
  echo WARNING: local Chroma model cache not found - first indexing will download it.
)
echo.
echo Bundling the Theme Suite next to the exe (auto-located at startup)...
if exist "theme_suite" (
  xcopy /E /I /Y /Q "theme_suite" "dist\HyperSpinClinic\theme_suite\"
) else if exist "..\Mame Project\Release" (
  xcopy /E /I /Y /Q "..\Mame Project\Release" "dist\HyperSpinClinic\theme_suite\"
) else (
  echo WARNING: no Theme Suite found to bundle - the Themes tab will be disabled.
)
echo.
echo Build done: dist\HyperSpinClinic\HyperSpinClinic.exe
pause

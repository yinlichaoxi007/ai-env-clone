@echo off
chcp 65001 >nul 2>&1
REM 一键运行全部单元测试（含无头 GUI 测试），结果写入 test_result.txt 并自动打开
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo 未检测到 Python，请先安装 Python 3.10+ 并勾选“Add to PATH”。
    pause
    exit /b 1
)

echo 正在运行测试，请稍候...
set RESULT_FILE=test_result.txt
if exist "%RESULT_FILE%" del /f /q "%RESULT_FILE%" 2>nul
if not exist "%RESULT_FILE%" (
    python -m unittest discover -s tests -p "test_*.py" > "%RESULT_FILE%" 2>&1
) else (
    REM 旧结果文件被占用时，改用带时间戳的文件，避免冲突
    set RESULT_FILE=test_result_%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%.txt
    python -m unittest discover -s tests -p "test_*.py" > "%RESULT_FILE%" 2>&1
)
set RC=%errorlevel%

echo.
echo ========================================
type "%RESULT_FILE%"
echo ========================================
if %RC%==0 (
    echo [OK] 全部测试通过。
) else (
    echo [FAIL] 存在失败用例，详情见上方输出 / %RESULT_FILE%。
)
echo.
echo 完整结果已保存至 %RESULT_FILE%
pause
endlocal

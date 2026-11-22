@echo off
chcp 65001 >nul
REM 一键演示（Windows 原生）：在临时目录上跑全部虚构样例，仅依赖标准库。
REM 用法（项目根目录）： samples\run_demo.bat
setlocal enabledelayedexpansion
set "ROOT=%~dp0.."
pushd "%ROOT%"
set "TMPDB=%TEMP%\recovery_demo_%RANDOM%.sqlite"
set "RUNNER=%ROOT%\solution\runner.py"

echo == 1) 初始化账号/受托人/证明/配方 ==
python -I "%RUNNER%" ingest samples\01_bootstrap.json --db "%TMPDB%" >nul || goto :fail
echo OK

echo == 2) 冻结窗口内取消（预留证明永久锁定，回执可回放） ==
python -I "%RUNNER%" ingest samples\02_reserve_challenge_cancel.json --db "%TMPDB%" >nul || goto :fail
echo OK

echo == 3) 完整 reserve -^> activate 身份世代推进（独立数据库） ==
set "HAPPY=%TEMP%\recovery_happy_%RANDOM%.sqlite"
python -I "%RUNNER%" ingest samples\01_bootstrap.json --db "%HAPPY%" >nul || goto :fail
python -I "%RUNNER%" ingest samples\03_reserve_activate.json --db "%HAPPY%" >nul || goto :fail
echo OK

echo == 4) 同截止点旧认证器异议优先 ==
set "CUTOFF=%TEMP%\recovery_cutoff_%RANDOM%.sqlite"
python -I "%RUNNER%" ingest samples\01_bootstrap.json --db "%CUTOFF%" >nul || goto :fail
python -I "%RUNNER%" ingest samples\04_cutoff_challenge_priority.json --db "%CUTOFF%" >nul || goto :fail
echo OK

echo == 5) simulate_loss 离线演练最小准备清单 ==
python -I "%RUNNER%" simulate_loss samples\05_simulate_loss.json || goto :fail

echo == 6) simulate_loss 无解诊断（预期退出码 1） ==
python -I "%RUNNER%" simulate_loss samples\06_simulate_no_plan.json
echo exit code = !errorlevel! （预期 1）

del "%TMPDB%" "%HAPPY%" "%CUTOFF%" 2>nul
popd
exit /b 0

:fail
echo 演示失败。
popd
exit /b 1

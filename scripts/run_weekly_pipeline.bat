@echo off
REM run_weekly_pipeline.bat
REM ------------------------
REM Windows Task Scheduler entry point for the weekly auction data pipeline.
REM Point Task Scheduler's "Action" at this file (Program/script:
REM   E:\01_vibe_coding\08_auction\scripts\run_weekly_pipeline.bat
REM ), no arguments needed for the default full run.
REM
REM To resume from a manual scrape earlier in the week, run:
REM   run_weekly_pipeline.bat --skip-scrape

cd /d "E:\01_vibe_coding\08_auction"
"C:\Python314\python.exe" scripts\run_weekly_pipeline.py %*

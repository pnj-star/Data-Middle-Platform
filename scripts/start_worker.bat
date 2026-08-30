@echo off
cd /d "D:\my_project\file pipeline channel"
"D:\my_env\fastapi_env\Scripts\python.exe" -m celery -A tasks.celery_worker worker --loglevel=INFO -P solo --logfile "D:\my_project\file pipeline channel\celery_worker.log"

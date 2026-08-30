@echo off
REM Start the MinerU API service (GPU document parsing backend) on 127.0.0.1:8010.
REM The project's converter routes pdf/docx/pptx to this service (MINERU_BASE_URL).
REM Requires D:\my_env\mineru_env (created with uv, Python 3.12, mineru[all] + CUDA torch).
set "MINERU_API=D:\my_env\mineru_env\Scripts\mineru-api.exe"
if not exist "%MINERU_API%" set "MINERU_API=mineru-api"
"%MINERU_API%" --host 127.0.0.1 --port 8010

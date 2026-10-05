FROM python:3.13.3-slim@sha256:56a11364ffe0fee3bd60af6d6d5209eba8a99c2c16dc4c7c5861dc06261503cc
WORKDIR /opt/railshot
COPY deployment/scripts/requirements-recovery.txt ./requirements-recovery.txt
RUN pip install --no-cache-dir -r requirements-recovery.txt
COPY deployment/scripts/recovery_service.py ./recovery_service.py

FROM python:3.12-slim

RUN python -m pip install --no-cache-dir uv==0.13.7 \
    && uv pip install --system --no-cache \
       dbt-core==1.11.17 dbt-duckdb==1.11.1 duckdb==1.5.6

COPY docker/analytics/runner.py /opt/runner.py

ENV PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["python", "/opt/runner.py"]

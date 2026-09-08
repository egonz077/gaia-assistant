FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 TZ=America/New_York
WORKDIR /srv

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY gaia ./gaia
COPY migrations ./migrations

# Exactly one worker. The per-user turn lock is in-process, so a second
# worker would silently reintroduce concurrent turns for the same user.
CMD ["uvicorn", "gaia.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

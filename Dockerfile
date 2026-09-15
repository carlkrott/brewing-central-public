FROM python:3.12-slim@sha256:cab2dbf575e971934a81e4622f5aba17aa7929719bd7e31033a3a83b97fd0464

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/
COPY requirements.lock /app/
COPY wheels/ /wheels/
RUN pip install --no-cache-dir --no-index --find-links=/wheels --require-hashes -r requirements.lock

RUN groupadd --system --gid 10001 ispindel \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin ispindel

COPY app /app/app
RUN chmod -R a=rX /app/app

EXPOSE 8098

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8098/health/live',timeout=2); b=json.load(r); s=r.status; r.close(); raise SystemExit(0 if s==200 and b=={'status':'ok','service':'ispindel-dashboard'} else 1)"]

USER 10001:10001

CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8098"]

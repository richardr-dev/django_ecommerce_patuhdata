FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Dependencies first so code edits do not reinstall them.
COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY . .
# Hashed, compressed static files for production. The throwaway key exists only for this step.
RUN DEBUG=0 SECRET_KEY=collectstatic-build-only ALLOWED_HOSTS=localhost \
        python manage.py collectstatic --noinput \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /app/media \
    && chown -R app:app /app/media /app/staticfiles

USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8000/healthz/', timeout=4).status != 200)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", \
     "--timeout", "60", "--access-logfile", "-"]

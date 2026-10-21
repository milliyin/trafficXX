FROM python:3.12-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN test -s /etc/ssl/certs/ca-certificates.crt
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 app
COPY trafficxx ./trafficxx
COPY database ./database
USER app
EXPOSE 8000
CMD ["uvicorn", "trafficxx.booking:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS test
USER root
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

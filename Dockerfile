FROM python:3.12-slim
WORKDIR /app
COPY pitwall ./pitwall
COPY web ./web
COPY data/austin-2025-demo.json data/austin-2025-source.json data/austin-2025-rivals.json ./data/
COPY project.json ./
RUN useradd --create-home pitwall && mkdir -p /app/.local && chown -R pitwall:pitwall /app
USER pitwall
EXPOSE 8001
CMD ["python", "-m", "pitwall.server", "--host", "0.0.0.0"]

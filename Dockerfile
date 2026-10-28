FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY mtg_engine ./mtg_engine
COPY config ./config
RUN pip install --no-cache-dir '.[vision,postgres]'
ENV MTG_ENGINE_CONFIG=/app/config/default.yaml
EXPOSE 8000
CMD ["mtg-engine", "serve", "--host", "0.0.0.0", "--port", "8000"]

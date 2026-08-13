FROM python:3.13-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project

COPY app ./app
RUN uv sync --locked --no-dev

ENV KX_HOST=0.0.0.0
ENV KX_PORT=8181
EXPOSE 8181

CMD ["uv", "run", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8181", "--log-level", "info", "--no-access-log"]

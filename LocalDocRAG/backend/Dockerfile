FROM ghcr.io/astral-sh/uv:0.7.8 AS uv
FROM python:3.12.10-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer poppler-utils fonts-noto-cjk libgomp1 libglib2.0-0 libgl1 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 DATA_DIR=/data
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY scripts ./scripts
# Download CPU OCR weights at image build time, never during document processing.
RUN mkdir -p /data && python -c "from rapidocr import RapidOCR;RapidOCR(params={'EngineConfig.onnxruntime.use_cuda':False,'EngineConfig.onnxruntime.intra_op_num_threads':2})"
RUN useradd -m -u 10001 app && chown -R app:app /data
USER app
EXPOSE 8000
CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","1","--no-access-log"]

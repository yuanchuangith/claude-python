FROM python@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df

WORKDIR /app
COPY server.py /app/server.py
USER 65532:65532
EXPOSE 8000
CMD ["python", "/app/server.py", "http"]

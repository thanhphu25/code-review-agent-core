FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml README.md __init__.py __main__.py cli.py demo.py graph.py dashboard*.py ./
COPY static/ ./static/
RUN python -m pip install --no-cache-dir '.[web]' && mkdir /data && chown 65532:65532 /data

USER 65532:65532
ENTRYPOINT ["review-agent"]
CMD ["demo"]

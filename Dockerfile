FROM python:3.12-slim

WORKDIR /build
COPY pyproject.toml README.md __init__.py __main__.py cli.py demo.py graph.py ./
RUN python -m pip install --no-cache-dir .

USER 65532:65532
ENTRYPOINT ["review-benchmark"]
CMD ["demo"]

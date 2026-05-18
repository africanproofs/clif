# clif — keyless FTSO reward claimer. No keys in this image (fwd holds them).
# Multi-stage, non-root, lockfile-honored.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir poetry==1.8.5
WORKDIR /src
COPY pyproject.toml poetry.lock README.md ./
COPY clif ./clif
# Pin runtime deps from the committed lock, then build the clif wheel.
RUN poetry export --only main --without-hashes -f requirements.txt -o requirements.txt \
 && poetry build -f wheel

FROM python:3.12-slim AS runtime
RUN useradd --uid 1000 --create-home clif
COPY --from=builder /src/requirements.txt /tmp/requirements.txt
COPY --from=builder /src/dist/*.whl /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir --no-deps /tmp/*.whl \
 && rm -rf /tmp/requirements.txt /tmp/*.whl
USER clif
WORKDIR /home/clif
# Status file lives in the clif user's home (writable, non-root). No secrets.
ENV CLIF_STATE_DIR=/home/clif/.clif-state
ENTRYPOINT ["clif"]
CMD ["auto"]

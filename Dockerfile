# clif — keyless FTSO reward claimer and FSP signer. No keys in this image.
# Multi-stage, non-root, lockfile-honored.

FROM python:3.12-slim AS builder
RUN pip install --no-cache-dir --timeout 120 --retries 5 poetry==1.8.5
WORKDIR /src
COPY pyproject.toml poetry.lock README.md ./
COPY clif ./clif
# Pin runtime deps from the committed lock, then build the clif wheel.
RUN poetry export --only main --without-hashes -f requirements.txt -o requirements.txt \
 && poetry build -f wheel

FROM python:3.12-slim AS runtime
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN useradd --uid 1000 --create-home clif
COPY --from=builder /src/requirements.txt /tmp/requirements.txt
COPY --from=builder /src/dist/*.whl /tmp/
# --timeout/--retries: pip defaults (15s, 0 retries) are too tight for a
# build-from-source provider on a variable link — a single slow wheel from the
# PyPI CDN otherwise fails the whole image build (ReadTimeout / "from versions: none").
# The whole install is additionally RETRIED up to 3x so a transient unreachable/timeout
# on one package (incl. the fwd-client git clone) doesn't fail the image build. Build
# images SEQUENTIALLY — parallel fwd+clif builds can saturate a constrained link.
# --without-hashes on export: fwd-client is a VCS dep (git+https://...) which pip
# cannot hash, making --require-hashes incompatible. git is present in this stage.
RUN ( pip install --no-cache-dir --timeout 300 --retries 5 -r /tmp/requirements.txt \
      && pip install --no-cache-dir --timeout 300 --retries 5 --no-deps /tmp/*.whl ) \
 || ( echo "pip install retry 1/2" && sleep 5  && pip install --no-cache-dir --timeout 300 --retries 5 -r /tmp/requirements.txt && pip install --no-cache-dir --timeout 300 --retries 5 --no-deps /tmp/*.whl ) \
 || ( echo "pip install retry 2/2" && sleep 15 && pip install --no-cache-dir --timeout 300 --retries 5 -r /tmp/requirements.txt && pip install --no-cache-dir --timeout 300 --retries 5 --no-deps /tmp/*.whl ) \
 && rm -rf /tmp/requirements.txt /tmp/*.whl
USER clif
WORKDIR /home/clif
# Status file lives in the clif user's home (writable, non-root). No secrets.
ENV CLIF_STATE_DIR=/home/clif/.clif-state
ENTRYPOINT ["clif"]
CMD ["epoch", "run"]

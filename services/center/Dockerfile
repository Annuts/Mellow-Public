ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS embedding-model
ARG WITH_EMBED_MODEL=1
ARG MELLOW_MODEL_MIRROR=
COPY src/mellow_center/embeddings.py /fetch_embeddings.py
RUN mkdir -p /models/bge-small-zh-v1.5 \
    && if [ "$WITH_EMBED_MODEL" = "1" ]; then \
         MELLOW_MODEL_MIRROR="$MELLOW_MODEL_MIRROR" python /fetch_embeddings.py /models/bge-small-zh-v1.5; \
       fi

FROM ${PYTHON_IMAGE} AS runtime

RUN for attempt in 1 2 3 4 5; do \
      apt-get update \
      && apt-get install -y --no-install-recommends -o Acquire::Retries=5 espeak-ng tzdata nodejs \
      && break; \
      [ "$attempt" = 5 ] && exit 1; sleep $((attempt * 10)); \
    done \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:aa133fe4e627ddab07916ec3afca1d51d5a9ac5f8bd71a4219d5e8d8c30fda40 https://registry.npmjs.org/@fly-ai/flyai-cli/-/flyai-cli-1.0.16.tgz /tmp/flyai.tgz
RUN tar xOzf /tmp/flyai.tgz package/dist/flyai-bundle.cjs > /usr/local/bin/flyai \
    && chmod 755 /usr/local/bin/flyai && flyai --help > /dev/null && rm /tmp/flyai.tgz
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY --from=embedding-model /models /opt/mellow/models

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    MELLOW_BUILTIN_MODEL_DIR=/opt/mellow/models/bge-small-zh-v1.5 \
    HF_HUB_OFFLINE=1 TOKENIZERS_PARALLELISM=false
USER 1000:1000
EXPOSE 8765
ENTRYPOINT ["python", "-m", "mellow_center", "--data-dir", "/var/lib/mellow"]

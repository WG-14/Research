ARG UV_PYTHON_IMAGE
FROM ${UV_PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    RESEARCH_OPS_SOURCE_ROOT=/opt/Research \
    PATH=/opt/Research/.venv/bin:${PATH}

RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
       ca-certificates jq openssl postgresql-client tar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/Research
COPY . /opt/Research

RUN uv sync --frozen --all-packages --no-dev --no-editable \
    && uv cache clean \
    && python -m compileall -q \
       src \
       apps/internal_web/src \
       services/research_operations/src \
    && chmod 0555 services/research_operations/scripts/*.sh \
                  services/research_operations/scripts/*.py \
    && groupadd --gid 10001 research \
    && useradd --uid 10001 --gid research --no-create-home \
       --shell /usr/sbin/nologin research \
    && ln -s /opt/Research/services/research_operations /opt/research-ops \
    && chown -R research:research /opt/Research

USER 10001:10001
WORKDIR /opt/Research

ENTRYPOINT ["/opt/research-ops/scripts/runtime-entrypoint.py"]
CMD ["research-ops", "--help"]

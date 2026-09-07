FROM debian:bookworm-slim AS stockfish

ARG STOCKFISH_VERSION=19
ARG TARGETARCH

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates curl tar && \
    rm -rf /var/lib/apt/lists/*

RUN target_arch="${TARGETARCH:-$(uname -m)}" && \
    case "${target_arch}" in \
        amd64|x86_64) \
            asset="stockfish-linux-x86-64-universal.tar.gz" \
            sha256="9defc0d4e55d49c65a6d042f3e571a39fcea499ade6dbe741b53b8c65e03611f" \
            ;; \
        arm64|aarch64) \
            asset="stockfish-linux-arm64-universal.tar.gz" \
            sha256="fe26cfd1d9db4c8af3d21e24d9ff34cacb31c1f940085a7583da11796f2bac01" \
            ;; \
        *) echo "Unsupported target architecture: ${target_arch}" >&2; exit 1 ;; \
    esac && \
    curl --fail --silent --show-error --location \
        -o /tmp/stockfish.tar.gz \
        "https://github.com/official-stockfish/Stockfish/releases/download/sf_${STOCKFISH_VERSION}/${asset}" && \
    echo "${sha256}  /tmp/stockfish.tar.gz" | sha256sum -c - && \
    mkdir -p /opt/stockfish && \
    tar -xzf /tmp/stockfish.tar.gz -C /opt/stockfish && \
    find /opt/stockfish -type f -name 'stockfish*' -perm -111 -exec cp {} /usr/local/bin/stockfish \; && \
    chmod +x /usr/local/bin/stockfish

FROM python:3.12-slim-bookworm

COPY --from=stockfish /usr/local/bin/stockfish /usr/local/bin/stockfish

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/utils/eco && \
    curl --fail --silent --show-error --location -o /app/utils/eco/ecoA.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoA.json && \
    curl --fail --silent --show-error --location -o /app/utils/eco/ecoB.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoB.json && \
    curl --fail --silent --show-error --location -o /app/utils/eco/ecoC.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoC.json && \
    curl --fail --silent --show-error --location -o /app/utils/eco/ecoD.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoD.json && \
    curl --fail --silent --show-error --location -o /app/utils/eco/ecoE.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoE.json

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
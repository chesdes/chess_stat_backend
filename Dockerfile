FROM python:3.12-slim-bookworm

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends stockfish curl && \
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
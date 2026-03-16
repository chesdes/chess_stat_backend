FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y stockfish curl

RUN mkdir -p /app/utils/eco

RUN curl -L -o /app/utils/eco/ecoA.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoA.json && \
    curl -L -o /app/utils/eco/ecoB.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoB.json && \
    curl -L -o /app/utils/eco/ecoC.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoC.json && \
    curl -L -o /app/utils/eco/ecoD.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoD.json && \
    curl -L -o /app/utils/eco/ecoE.json https://raw.githubusercontent.com/hayatbiralem/eco.json/master/ecoE.json

COPY .env .env
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
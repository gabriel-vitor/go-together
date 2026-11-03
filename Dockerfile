# Imagem única para todos os componentes do VAIJUNTO (servidor de empresa,
# clientes, simulador e testes). O comando padrão sobe o servidor de uma
# empresa; os demais componentes sobrepõem o comando (ver docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY comum/ comum/
COPY servidor/ servidor/
COPY consorcio/ consorcio/
COPY cliente/ cliente/
COPY simulador/ simulador/
COPY testes/ testes/

# Logs sem buffer, para acompanhar o servidor em tempo real no docker logs.
ENV PYTHONUNBUFFERED=1

EXPOSE 5000 8000
CMD ["python", "-m", "servidor.servidor"]

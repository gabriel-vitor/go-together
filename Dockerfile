# Imagem única para todos os componentes do VAIJUNTO (servidor, clientes e
# teste): o código usa apenas a biblioteca padrão do Python, então não há
# dependências a instalar. O comando padrão sobe o servidor; os clientes e o
# teste são executados sobrepondo o comando (ver docker-compose.yml).
FROM python:3.12-slim

WORKDIR /app
COPY comum/ comum/
COPY servidor/ servidor/
COPY cliente/ cliente/
COPY testes/ testes/

# Logs sem buffer, para acompanhar o servidor em tempo real no docker logs.
ENV PYTHONUNBUFFERED=1

EXPOSE 5000
CMD ["python", "-m", "servidor.servidor"]

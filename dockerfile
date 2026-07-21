FROM python:3.11-slim

# логи Python не буферизуются -> видны в docker logs сразу
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Хост закрыт от pypi.org -> ставим из внутреннего зеркала.
# На машине с интернетом (Mac) переопределить:
#   --build-arg PIP_INDEX_URL=https://pypi.org/simple --build-arg PIP_TRUSTED_HOST=pypi.org
ARG PIP_INDEX_URL=https://repo.corp.tander.ru/repository/pypi/simple
ARG PIP_TRUSTED_HOST=repo.corp.tander.ru

COPY requirements.txt .
RUN pip install --no-cache-dir \
    --index-url "$PIP_INDEX_URL" \
    --trusted-host "$PIP_TRUSTED_HOST" \
    -r requirements.txt

COPY . .

# порт HTTP-сервера кнопок/модалок (BUTTON_PORT внутри контейнера)
EXPOSE 8080

CMD ["python", "bot.py"]

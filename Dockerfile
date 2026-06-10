FROM python:3.12-alpine

RUN apk add --no-cache libffi openssl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y pip setuptools \
    && rm -rf /root/.cache

COPY app/ ./app/

RUN mkdir -p /app/data/certs

EXPOSE 25 465 587 2587 5000

CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5000", "--timeout", "120", "app.main:app"]

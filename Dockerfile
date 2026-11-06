FROM aiogram/telegram-bot-api@sha256:edc1d268cd86efc8d50f118dc4342e6756ff303a342d61edc0871ff25545f774 AS botapi
FROM python:3.12-alpine3.21@sha256:d4f9227f21409479c7fe92288f2e40b1a56ae591c8ad3b5446dfeaef90f67857

COPY --from=botapi /usr/local/bin/telegram-bot-api /usr/local/bin/
COPY --from=botapi /usr/lib/libstdc++.so.6.0.33 /usr/lib/libstdc++.so.6
COPY --from=botapi /usr/lib/libgcc_s.so.1 /usr/lib/

ENV PYTHONUNBUFFERED=1 \
    PORT=8080 \
    MCM_LOCAL_API_PORT=8081

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . ./

CMD ["python", "mulai_lokal.py"]

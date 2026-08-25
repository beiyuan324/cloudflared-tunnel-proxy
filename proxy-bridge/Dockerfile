FROM redis:8-alpine

ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

RUN apk add --no-cache python3

ENV HTTP_PROXY=
ENV HTTPS_PROXY=
COPY cloudflared_connect_proxy.py /usr/local/bin/cloudflared_connect_proxy.py

ENTRYPOINT ["python3", "/usr/local/bin/cloudflared_connect_proxy.py"]

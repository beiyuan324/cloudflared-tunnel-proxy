FROM cloudflare/cloudflared:latest AS cloudflared
FROM redis:8-alpine

ARG HTTP_PROXY
ARG HTTPS_PROXY
ENV HTTP_PROXY=${HTTP_PROXY}
ENV HTTPS_PROXY=${HTTPS_PROXY}

RUN apk add --no-cache python3

COPY --from=cloudflared /usr/local/bin/cloudflared /usr/local/bin/cloudflared
COPY cloudflared_supervisor.py /usr/local/bin/cloudflared_supervisor.py

RUN chmod 755 /usr/local/bin/cloudflared /usr/local/bin/cloudflared_supervisor.py
ENV HTTP_PROXY=
ENV HTTPS_PROXY=
USER 65532:65532

ENTRYPOINT ["python3", "/usr/local/bin/cloudflared_supervisor.py"]

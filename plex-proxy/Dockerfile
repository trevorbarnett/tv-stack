FROM alpine:latest
RUN apk add --no-cache socat iproute2
COPY start.sh /start.sh
RUN chmod +x /start.sh
ENTRYPOINT ["/start.sh"]

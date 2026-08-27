# The SPA. No build step: the app is vanilla ES modules, so there is nothing
# to bundle and no node toolchain in the image.
FROM nginx:1.27-alpine

COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY web /usr/share/nginx/html

# nginx:alpine ships an unprivileged-friendly setup; port 8080 avoids needing
# CAP_NET_BIND_SERVICE for a low port.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost:8080/ || exit 1

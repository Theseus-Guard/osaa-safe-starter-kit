FROM node:24-alpine AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx/nginx-unprivileged:1.31.8-alpine3.24
COPY gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /build/dist/ /usr/share/nginx/html/

FROM node:24.22.2-alpine3.24@sha256:8a32c8cf649a49915a66e81e7a609223c708d0adabf288ee3f4077d765542f85 AS frontend-build
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM nginx/nginx-unprivileged:1.31.8-alpine3.24@sha256:03df07e5931f8374d440bd62243a5ce9b24719715ef6fc4c9c626a5f1dbef417
COPY gateway/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /build/dist/ /usr/share/nginx/html/

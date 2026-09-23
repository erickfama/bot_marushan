#!/usr/bin/env sh
set -eu

echo "== Sistema =="
uname -a
test -r /etc/os-release && sed -n '1,12p' /etc/os-release

echo "== Recursos =="
getconf _NPROCESSORS_ONLN
free -h
df -h /

echo "== Docker =="
docker version --format 'Engine {{.Server.Version}}' 2>/dev/null || true
docker compose version 2>/dev/null || true

echo "== Contenedores y redes (solo lectura) =="
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || true
docker network ls 2>/dev/null || true

echo "== Puertos en escucha =="
ss -lntup 2>/dev/null || netstat -lntup 2>/dev/null || true

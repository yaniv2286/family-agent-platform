#!/bin/bash
set -e

cd "$(dirname "$0")"

apt-get update
apt-get install -y ca-certificates curl gnupg lsb-release ufw

curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sh /tmp/get-docker.sh

apt-get update
apt-get install -y docker-compose-plugin

ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

mkdir -p ./data/koko
chmod 755 ./data/koko

docker compose up -d

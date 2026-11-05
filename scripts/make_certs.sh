#!/bin/bash
# make_certs.sh [ip] - self-signed CA + server certificate for the VA (:8000) and non-VA (:8001) HTTPS services the phones open.
# CN / SAN = the UE-facing address (ogstun, default 10.45.0.1). One CA (YOLO-Local-CA) is written to va_service/certs and copied to
# nonva_service/certs, each with its own server.key / server.crt. Phones: install ca.crt once (Settings > Security > CA certificate)
# or accept the browser warning. Existing certificates are kept (remove the directory to regenerate).
set -eu
D=$(cd "$(dirname "$0")/.." && pwd); IP=${1:-10.45.0.1}
CA=$D/va_service/certs; mkdir -p $CA
if [ ! -s $CA/ca.key ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -keyout $CA/ca.key -out $CA/ca.crt -days 3650 -subj "/CN=YOLO-Local-CA" 2>/dev/null; echo "CA: $CA/ca.crt"
fi
for svc in va_service nonva_service; do
  C=$D/$svc/certs; mkdir -p $C; cp -p $CA/ca.crt $CA/ca.key $C/ 2>/dev/null || true
  [ -s $C/server.key ] && [ -s $C/server.crt ] && { echo "$svc/certs: exists"; continue; }
  openssl req -newkey rsa:2048 -nodes -keyout $C/server.key -out $C/server.csr -subj "/CN=$IP" 2>/dev/null
  printf 'authorityKeyIdentifier=keyid,issuer\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=IP:%s\n' $IP > $C/server.ext
  openssl x509 -req -in $C/server.csr -CA $C/ca.crt -CAkey $C/ca.key -CAcreateserial -out $C/server.crt -days 3650 -extfile $C/server.ext 2>/dev/null
  chmod 600 $C/ca.key $C/server.key; echo "$svc/certs: server.crt CN=$IP ($(openssl x509 -in $C/server.crt -noout -enddate))"
done

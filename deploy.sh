#!/bin/bash
# deploy.sh — Full production deploy on a fresh Ubuntu 22.04 VPS
# Run as root: bash deploy.sh your-domain.com

set -e
DOMAIN=${1:-"localhost"}

echo "================================================"
echo "  Deploying Trading Platform"
echo "  Domain: $DOMAIN"
echo "================================================"

# System deps
apt-get update -q
apt-get install -y -q docker.io docker-compose nginx certbot python3-certbot-nginx curl

# Start Docker
systemctl enable docker
systemctl start docker

# Set domain in nginx config
sed "s/YOUR_DOMAIN_HERE/$DOMAIN/g" nginx.conf > /etc/nginx/sites-available/trading
ln -sf /etc/nginx/sites-available/trading /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Copy web dashboard
cp web/index.html /var/www/html/index.html 2>/dev/null || true
mkdir -p /app/web
cp web/index.html /app/web/index.html 2>/dev/null || true

# SSL cert (skip if localhost)
if [ "$DOMAIN" != "localhost" ]; then
    certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m admin@$DOMAIN || true
fi

nginx -t && systemctl restart nginx

# Build and start Docker stack
docker-compose pull
docker-compose up -d

echo ""
echo "================================================"
echo "  Deployed successfully!"
echo ""
echo "  Web dashboard: https://$DOMAIN"
echo "  API:           https://$DOMAIN/api"
echo "  Logs:          docker-compose logs -f bot"
echo "  Status:        docker-compose ps"
echo "================================================"

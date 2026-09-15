#!/bin/bash
# ModelLink - Nginx + SSL Setup
# Run this after deploy.sh

set -e

echo "=== Nginx + SSL Setup ==="

# Get the server's public IP
PUBLIC_IP=$(curl -s ifconfig.me)
echo "Your server's public IP: $PUBLIC_IP"

# Create nginx config
sudo cat > /etc/nginx/sites-available/modellink << 'NGINXEOF'
server {
    listen 80;
    server_name _;

    # Max upload size
    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_cache_bypass $http_upgrade;
        proxy_read_timeout 300s;
        proxy_connect_timeout 75s;

        # SSE streaming support
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
    }
}
NGINXEOF

# Enable the site
sudo ln -sf /etc/nginx/sites-available/modellink /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test and reload
sudo nginx -t
sudo systemctl reload nginx

echo ""
echo "=== Nginx configured! ==="
echo ""
echo "Your ModelLink is now accessible at:"
echo "  http://$PUBLIC_IP"
echo ""
echo "To add SSL (https), run:"
echo "  sudo certbot --nginx -d $PUBLIC_IP --non-interactive --agree-tos --email your@email.com"
echo ""
echo "(Note: SSL with IP addresses has limitations. For full HTTPS, use a domain name.)"

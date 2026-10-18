"""Nginx + Certbot IP certificate lifecycle. No Docker socket or DSM access."""
import hashlib
import ipaddress
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import time
import urllib.request

CERT = Path('/etc/letsencrypt/live/svn-ip/fullchain.pem')
KEY = CERT.with_name('privkey.pem')
STOP = False
NGINX = None
ACME = None


def settings():
    address = str(ipaddress.IPv4Address(os.environ['PUBLIC_IP']))
    email = os.environ['ACME_EMAIL']
    if not email or any(c.isspace() for c in email) or '@' not in email:
        raise ValueError('Set a valid ACME_EMAIL')
    return address, email


def digest():
    return hashlib.sha256(CERT.read_bytes() + KEY.read_bytes()).hexdigest() if CERT.exists() and KEY.exists() else None


def render():
    address, _ = settings()
    Path('/var/www/acme').mkdir(parents=True, exist_ok=True)
    Path('/var/www/health').mkdir(parents=True, exist_ok=True)
    Path('/var/www/health/healthz').write_text('nginx ready\n')
    config = '''
map $http_destination $svn_destination {
    default $http_destination;
    ~^https://(.*)$ http://$1;
}
server {
    listen 80 default_server;
    server_name _;
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/acme;
        default_type text/plain;
        limit_except GET { deny all; }
        try_files $uri =404;
    }
    location = /healthz { root /var/www/health; }
    location / { return 404; }
}
'''
    if digest():
        config += '''
server {
    listen 443 ssl default_server;
    server_name ADDRESS;
    ssl_certificate /etc/letsencrypt/live/svn-ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/svn-ip/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 0;
    client_body_timeout 21600s;
    send_timeout 21600s;
    location = /healthz { root /var/www/health; }
    location /svn/ {
        proxy_pass http://master-http:80;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header Connection "";
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Destination $svn_destination;
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_read_timeout 21600s;
        proxy_send_timeout 21600s;
        proxy_redirect http://$http_host/ https://$http_host/;
    }
    location / { return 404; }
}
'''.replace('ADDRESS', address)
    Path('/etc/nginx/conf.d/svn.conf').write_text(config)
    subprocess.run(['nginx', '-t'], check=True)


def stop(signum, frame):
    global STOP
    STOP = True
    if ACME is not None and ACME.poll() is None:
        ACME.terminate()
    if NGINX is not None and NGINX.poll() is None:
        NGINX.send_signal(signal.SIGQUIT)


def issue_or_renew():
    global ACME
    address, email = settings()
    # certonly with --keep-until-expiring handles both first issuance and renewal.
    # The persistent named lineage prevents duplicate certificates on restarts.
    args = ['certbot', 'certonly', '--non-interactive', '--agree-tos',
            '--email', email, '--cert-name', 'svn-ip', '--keep-until-expiring',
            '--preferred-profile', 'shortlived', '--preferred-challenges', 'http',
            '--webroot', '--webroot-path', '/var/www/acme', '--ip-address', address,
            '--server', os.getenv('ACME_SERVER', 'https://acme-v02.api.letsencrypt.org/directory')]
    ACME = subprocess.Popen(args)
    try:
        return ACME.wait(timeout=600) == 0
    except subprocess.TimeoutExpired:
        ACME.kill()
        ACME.wait()
        print('Certbot timed out; will retry', flush=True)
        return False
    finally:
        ACME = None


def health():
    address, _ = settings()
    subprocess.run(['openssl', 'x509', '-in', str(CERT), '-noout',
                    '-checkend', '86400', '-checkip', address], check=True)
    # Verify both trust and the ACTUALLY served certificate, without insecure flags.
    # The TLS socket connects locally but validates the configured public IP.
    import socket
    with socket.create_connection(('127.0.0.1', 443), timeout=5) as sock:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=address):
            pass
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    opener.open('http://127.0.0.1/healthz', timeout=5).read()


def main():
    global NGINX
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'
    if command == 'health':
        health()
        return
    render()
    if command == 'render':
        return
    if command != 'serve':
        raise ValueError('Unknown command')
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    NGINX = subprocess.Popen(['nginx', '-g', 'daemon off;'])
    loaded = digest()
    next_check = 0
    try:
        while not STOP:
            if NGINX.poll() is not None:
                raise RuntimeError('nginx exited unexpectedly')
            if time.monotonic() >= next_check:
                success = issue_or_renew()
                if STOP:
                    break
                current = digest()
                if current != loaded:
                    render()
                    subprocess.run(['nginx', '-s', 'reload'], check=True)
                    loaded = current
                    print('nginx loaded updated IP certificate', flush=True)
                interval = 'RENEW_INTERVAL_SECONDS' if success else 'ACME_RETRY_SECONDS'
                next_check = time.monotonic() + max(60, int(os.getenv(interval, '43200' if success else '900')))
            time.sleep(1)
    finally:
        if NGINX.poll() is None:
            NGINX.send_signal(signal.SIGQUIT)
            try:
                NGINX.wait(timeout=30)
            except subprocess.TimeoutExpired:
                NGINX.kill()
                NGINX.wait()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        sys.exit(1)

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    apache2 libapache2-mod-svn subversion python3 ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 svnmirror \
    && useradd -u 10001 -g svnmirror -M -s /usr/sbin/nologin svnmirror \
    && usermod -a -G svnmirror www-data \
    && a2enmod dav dav_svn authz_svn proxy proxy_http ssl cgid headers rewrite \
    && a2dissite 000-default \
    && printf 'Listen 80\nListen 81\n' > /etc/apache2/ports.conf \
    && printf 'ServerName localhost\n' > /etc/apache2/conf-available/servername.conf \
    && a2enconf servername
COPY app.py /opt/svnmirror/app.py
COPY write-guard /opt/svnmirror/write-guard
COPY auto.py worker.py telemetry.py dashboard.py discovery.cgi status.cgi log-errors /opt/svnmirror/
COPY web/ /opt/svnmirror/web/
# Synology/Git working copies may supply owner-only modes to COPY. Normalize
# image code and assets, independently of the host ACLs. No runtime data here.
RUN find /opt/svnmirror -type d -exec chmod 755 {} \; \
    && find /opt/svnmirror -type f -exec chmod 644 {} \; \
    && chmod 755 /opt/svnmirror/write-guard /opt/svnmirror/discovery.cgi /opt/svnmirror/status.cgi /opt/svnmirror/log-errors
EXPOSE 80 81
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "/opt/svnmirror/app.py"]
CMD ["serve"]

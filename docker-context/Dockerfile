#############################
# Markus Hilsenbeck
# svnserver with svn:// protocol

FROM ubuntu:22.04

LABEL Maintainer="Markus Hilsenbeck" \
      E-Mail="markus.dev@hilsi.de" \
      GitHub="https://github.com/MarkusH1975/"


# Add Tini as init process running as pid 1
ENV TINI_VERSION v0.19.0
ADD https://github.com/krallin/tini/releases/download/${TINI_VERSION}/tini /tini
RUN chmod +x /tini
ENTRYPOINT ["/tini", "-w", "-v", "--"]
      

# volume: create directories and links
RUN mkdir -p /volume/svnrepo
RUN mkdir -p /volume/usvn
RUN mkdir -p /var/www/html


# Installation
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install --no-install-recommends -y apt-utils && \
    apt-get install --no-install-recommends -y \
    vim nano sudo wget curl unzip lsof psmisc git \
    locales \
    ca-certificates \
    subversion \
    apache2 \
    libapache2-mod-svn \
    php php-cli php-xml php-mbstring php-sqlite3 libapache2-mod-php \
    sqlite3 \
    && apt-get clean && apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*


# generate some locales
RUN locale-gen \
    de_DE.UTF-8 \
    en_GB.UTF-8 \
    en_US.UTF-8


# volume: set permissions
RUN chmod -Rfv 777 /volume/svnrepo
RUN chmod -Rfv 777 /volume/usvn
RUN chown -R www-data:www-data /volume/svnrepo /volume/usvn

# Copy USVN source code into container
COPY usvn/src /var/www/html/usvn
RUN chown -R www-data:www-data /var/www/html/usvn

# Modify USVN to auto-configure repositories when created
# Add configure-repo call after repository creation in RepositoryController
RUN if [ -f /var/www/html/usvn/app/controllers/RepositoryController.php ]; then \
    sed -i '/USVN_Project::createProject/a \            \n            \/\/ Auto-configure repository for svnserve\n            $repoPath = $project->getPath();\n            exec("\/usr\/local\/bin\/configure-repo '"'"'$repoPath'"'"' > \/dev\/null 2>\&1 \&");' \
    /var/www/html/usvn/app/controllers/RepositoryController.php; \
    fi

# Set permissions for USVN directories
# public: needs write access for installation
# config: needs permanent write access
RUN chmod -R 777 /var/www/html/usvn/public && \
    chmod -R 777 /var/www/html/usvn/config

# Enable Apache modules
RUN a2enmod rewrite
RUN a2enmod dav
RUN a2enmod dav_svn
RUN a2enmod authz_svn

# Copy Apache configuration for USVN
COPY docker-context/usvn.conf /etc/apache2/sites-available/000-default.conf

# volume: copy everything to the template folder for automatic generation
RUN cp -fvra /volume /volume.template

#Ports exposed by the container
# Apache web server for USVN: http://localhost:80/usvn/
EXPOSE 80
# svn protocol: svn://
EXPOSE 3690

# Environment variables: select services to be started
ENV ENABLE_SVNSERVER=true \
    ENABLE_APACHE=true 

# To force docker build to invalidate cache from now on, to be sure entrypoint.sh is updated
# docker build --build-arg CACHE_DATE=$(date +%Y-%m-%d:%H:%M:%S) -t svnserver.apache .
ARG CACHE_DATE
RUN echo "\n* Docker image build time: $CACHE_DATE"

# add startup script
COPY docker-context/entrypoint.sh /

# add svn password setter script (for manual password changes)
COPY docker-context/set-svn-password.php /usr/local/bin/set-svn-password
RUN chmod +x /usr/local/bin/set-svn-password

# add repository configuration script
COPY docker-context/configure-repo.sh /usr/local/bin/configure-repo
RUN chmod +x /usr/local/bin/configure-repo

# CMD will be executed by tini 
CMD ["/bin/bash", "/entrypoint.sh"]

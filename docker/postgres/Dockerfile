FROM postgres:17-bookworm
COPY init-cluster.sh /docker-entrypoint-initdb.d/01-init-cluster.sh
RUN chmod +x /docker-entrypoint-initdb.d/01-init-cluster.sh

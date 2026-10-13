FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY openscn ./openscn
RUN pip install --no-cache-dir . \
    && useradd --system --uid 10001 --create-home openscn \
    && mkdir /data \
    && chown openscn:openscn /data
USER openscn
VOLUME ["/data"]
EXPOSE 7777
ENTRYPOINT ["openscn"]
CMD ["serve", "--data", "/data", "--host", "0.0.0.0", "--port", "7777"]

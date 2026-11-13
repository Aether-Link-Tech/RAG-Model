FROM python:3.12-slim

# Create dedicated unprivileged sandbox user and group (UID/GID 10001)
RUN groupadd -g 10001 sandboxgroup && \
    useradd -u 10001 -g sandboxgroup -s /sbin/nologin -M sandboxuser

WORKDIR /app

# Install isolated parsing dependencies
RUN pip install --no-cache-dir \
    PyPDF2==3.0.1 \
    python-docx==1.1.2 \
    pymupdf==1.24.10

COPY runner.py /app/runner.py

RUN chown -R root:root /app && \
    chmod 755 /app && \
    chmod 644 /app/runner.py

# Switch to unprivileged non-root user
USER 10001:10001

ENTRYPOINT ["python", "-u", "/app/runner.py"]


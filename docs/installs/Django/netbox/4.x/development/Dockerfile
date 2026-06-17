FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    postgresql-client \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# setuptools is required for editable installs of packages using setup.py /
# setuptools build backend; python:3.12-slim doesn't include it by default.
RUN pip install --no-cache-dir setuptools

# Pre-install NetBox's pip requirements as a cached layer.
# Build context is the repo root so requirements.txt is accessible.
COPY requirements.txt /tmp/netbox-requirements.txt
RUN pip install --no-cache-dir -r /tmp/netbox-requirements.txt

# manage.py lives here; Django adds this to sys.path automatically.
WORKDIR /opt/netbox/netbox
ENV PYTHONPATH=/opt/netbox/netbox

EXPOSE 8080

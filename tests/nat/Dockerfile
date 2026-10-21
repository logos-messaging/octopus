# Build context is the repo root. LIB must point at a Linux liblogosdelivery.so:
#   docker compose -f tests/nat/docker-compose.yml build --build-arg LIB=path/to/liblogosdelivery.so
FROM python:3.12-slim
ARG LIB
# liblogosdelivery links against OpenMP, which the slim image does not ship.
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY vendor/logos-delivery-python-bindings ./vendor/logos-delivery-python-bindings
COPY ${LIB} ./vendor/logos-delivery-python-bindings/lib/liblogosdelivery.so
# The bindings come from the vendored copy above, so skip the [logos] extra: its git URL needs git.
RUN pip install --no-cache-dir -e . cffi cbor2 result
COPY tests/nat/nat_check.py ./nat_check.py
CMD ["python", "nat_check.py"]

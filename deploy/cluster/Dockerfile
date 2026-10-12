ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
COPY wheels/ /tmp/wheels/
RUN python -m pip install --no-index --no-deps --no-cache-dir /tmp/wheels/*.whl \
    && rm -r /tmp/wheels
COPY --chmod=0444 initialize.py /opt/dsec-initialize.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "dsec.host.cluster"]

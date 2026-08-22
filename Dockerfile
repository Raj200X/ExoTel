FROM python:3.12-slim

WORKDIR /app

# Prevent python from buffering stdout/stderr
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Install dependencies
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

# Copy application source code
COPY config/ ./config/
COPY src/ ./src/
COPY server.py ./

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "saarthi.main:app", "--host", "0.0.0.0", "--port", "8000"]

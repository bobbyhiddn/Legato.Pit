# Legato.Pit - Dashboard & Transcript Dropbox
FROM python:3.11-slim

# Install dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY src /app/src/

# Expose the port
EXPOSE 8000

# Set working directory to src
WORKDIR /app/src

# Run with Gunicorn
CMD ["gunicorn", "--workers=2", "--bind=0.0.0.0:8000", "--log-level=info", "--access-logfile=-", "--error-logfile=-", "main:app"]

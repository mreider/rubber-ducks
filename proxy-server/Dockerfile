FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy the app code
COPY . .

# Run the load generator
CMD ["python", "proxy_server.py"]

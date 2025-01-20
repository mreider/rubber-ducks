FROM python:3.9-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy the app code
COPY . .

# Expose the port
EXPOSE 5000

CMD ["python", "order_ducks.py"]

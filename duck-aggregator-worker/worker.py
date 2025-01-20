import json
import logging
import os
import sys
import pika
import redis

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind


# For Redis instrumentation
from opentelemetry.instrumentation.redis import RedisInstrumentor

# Initialize Redis instrumentation
RedisInstrumentor().instrument()

# For extracting trace context
dt_endpoint = os.getenv("DT_ENDPOINT", "http://localhost:4317")
dt_api_token = os.getenv("DT_API_TOKEN", "")
if not dt_endpoint or not dt_api_token:
    raise ValueError("Both DT_ENDPOINT and DT_API_TOKEN environment variables must be set.")

metadata = {}
for file_path in [
    "dt_metadata_e617c525669e072eebe3d0f08212e8f2.json",
    "/var/lib/dynatrace/enrichment/dt_metadata.json",
    "/var/lib/dynatrace/enrichment/dt_host_metadata.json"
]:
    try:
        with open(file_path) as f:
            metadata.update(json.load(f))
    except FileNotFoundError:
        logging.warning(f"Metadata file not found: {file_path}")
    except Exception as e:
        logging.warning(f"Could not read metadata file {file_path}: {e}")

# Add custom metadata
metadata.update({"service.name": "aggregator-worker", "service.version": "1.0.0"})
resource = Resource.create(metadata)

# Setup tracer provider
trace_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{dt_endpoint}/v1/traces",
    headers={"Authorization": f"Api-Token {dt_api_token}"}
)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)

# Setup logger
logger_provider = LoggerProvider(resource=resource)
set_logger_provider(logger_provider)
logger_provider.add_log_record_processor(
    BatchLogRecordProcessor(
        OTLPLogExporter(endpoint=f"{dt_endpoint}/v1/logs", headers={"Authorization": f"Api-Token {dt_api_token}"})
    )
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

rabbit_host = "duck-queue-service"
aggregator_queue = "aggregator_queue"

# Redis connection (for example, setting this up in case you want to use it for storing partial results)
redis_client = redis.StrictRedis(host="localhost", port=6379, db=0)

def handle_aggregator(ch, method, properties, body):
    tracer = trace.get_tracer(__name__)

    # Extract trace context from the incoming message
    received_headers = properties.headers or {}
    ctx = extract(received_headers)
    
    # Create span for consuming from RabbitMQ
    with tracer.start_as_current_span("aggregator_consume", context=ctx, kind=SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.source", aggregator_queue)
        span.set_attribute("messaging.operation", "consume")
        try:
            msg = json.loads(body)
            order_id = msg.get("order_id")
            span.set_attribute("order_id", order_id)

            order_trace_id = msg.get("trace_id")
            if order_trace_id:
                span.link(trace.Link(trace_id=order_trace_id))

            logger.info(f"[aggregator-worker] Got partial result: {msg}")

            # Separate Redis span for interacting with Redis
            with tracer.start_as_current_span("redis_set", kind=SpanKind.CLIENT) as redis_span:
                redis_span.set_attribute("db.system", "redis")
                redis_span.set_attribute("db.operation", "set")
                redis_span.set_attribute("redis.key", order_id)

                # Simulate Redis operation (SET)
                redis_client.set(order_id, json.dumps(msg))  # Example Redis operation

                redis_span.set_attribute("redis.result", "stored")  # Storing result in Redis
                logger.info(f"Stored partial result for order_id {order_id} in Redis")

            # Acknowledge message after processing
            ch.basic_ack(delivery_tag=method.delivery_tag)
        
        except Exception as e:
            logger.error("Aggregator worker failed to process message", exc_info=True)
            ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    logger.info("[aggregator-worker] Connecting to RabbitMQ...")
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbit_host))
    channel = connection.channel()

    channel.queue_declare(queue=aggregator_queue, durable=True)

    logger.info("[aggregator-worker] Starting to consume aggregator_queue...")
    channel.basic_consume(queue=aggregator_queue, on_message_callback=handle_aggregator, auto_ack=False)
    channel.start_consuming()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("[aggregator-worker] Stopping by user request.")
    except Exception as e:
        logger.error("[aggregator-worker] Critical error", exc_info=True)
        sys.exit(1)

import json
import logging
import os
import sys
import redis
import pika

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
from opentelemetry.trace import SpanKind, Link

# For Redis instrumentation
from opentelemetry.instrumentation.redis import RedisInstrumentor
RedisInstrumentor().instrument()

# For extracting trace context and setting up Dynatrace exporter
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
        OTLPLogExporter(
            endpoint=f"{dt_endpoint}/v1/logs",
            headers={"Authorization": f"Api-Token {dt_api_token}"}
        )
    )
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Redis connection for storing partial results
redis_client = redis.StrictRedis(host='duck-db-service', port=6379, db=0)

def on_message(ch, method, properties, body):
    tracer = trace.get_tracer(__name__)
    # Extract headers from pika properties if available
    received_headers = properties.headers if properties and properties.headers else {}
    # Extract trace context from incoming message headers
    ctx = extract(received_headers)
    
    with tracer.start_as_current_span("aggregator_consume", context=ctx, kind=SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination.name", "aggregator_queue")
        span.set_attribute("messaging.operation.name", "consume")
        try:
            # Assume the body contains a JSON string with a nested "body" field
            data = json.loads(body.decode("utf-8"))
            msg = json.loads(data.get("body", "{}"))
            order_id = msg.get("order_id")
            span.set_attribute("order_id", order_id)
    
            order_trace_id = msg.get("trace_id")
            if order_trace_id:
                span.link(Link(trace_id=order_trace_id))
    
            logger.info(f"[aggregator-worker] Got partial result: {msg}")
    
            # Create a separate span for the Redis operation
            with tracer.start_as_current_span("redis_set", kind=SpanKind.CLIENT) as redis_span:
                redis_span.set_attribute("db.system", "redis")
                redis_span.set_attribute("db.operation", "set")
                redis_span.set_attribute("redis.key", order_id)
                redis_client.set(order_id, json.dumps(msg))
                redis_span.set_attribute("redis.result", "stored")
                logger.info(f"Stored partial result for order_id {order_id} in Redis")
        
        except Exception as e:
            logger.error("Aggregator worker failed to process message", exc_info=True)
    
    # Acknowledge message after processing
    ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    logger.info("[aggregator-worker] Starting pika consumer...")
    connection = pika.BlockingConnection(pika.ConnectionParameters(host="duck-queue-service"))
    channel = connection.channel()
    channel.queue_declare(queue="aggregator_queue", durable=True)
    channel.basic_qos(prefetch_count=1)
    channel.basic_consume(queue="aggregator_queue", on_message_callback=on_message)
    try:
        channel.start_consuming()
    except KeyboardInterrupt:
        channel.stop_consuming()
        logger.info("[aggregator-worker] Consumer stopped by user request.")
    except Exception as e:
        logger.error("[aggregator-worker] Critical error", exc_info=True)
        sys.exit(1)
    finally:
        connection.close()

if __name__ == "__main__":
    main()

import json
import logging
import os
import sys
import time

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


# For extracting + injecting trace context
from opentelemetry.propagate import extract, inject

# For Redis instrumentation
from opentelemetry.instrumentation.redis import RedisInstrumentor

# Initialize Redis instrumentation
RedisInstrumentor().instrument()

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
metadata.update({"service.name": "analytics-worker", "service.version": "1.0.0"})

resource = Resource.create(metadata)
trace_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{dt_endpoint}/v1/traces",
    headers={"Authorization": f"Api-Token {dt_api_token}"}
)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)

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
fanout_exchange = "duck_orders_fanout"
analytics_queue = "analytics_queue"
aggregator_queue = "aggregator_queue"


def some_redis_operation():
    tracer = trace.get_tracer(__name__)
    
    # Start a Redis span for a database operation
    with tracer.start_as_current_span("redis_get", kind=trace.SpanKind.CLIENT) as span:
        # Set Redis specific attributes for the span
        span.set_attribute("db.system", "redis")
        span.set_attribute("db.operation", "get")  # Operation type (e.g., get, set, etc.)
        span.set_attribute("db.name", "example_database")  # Optionally specify database name
        
        # Example Redis GET operation
        redis_client = redis.StrictRedis(host='duck-db-service', port=6379, db=0)
        result = redis_client.get('some_key')
        span.set_attribute("redis.key", 'some_key')
        span.set_attribute("redis.result", result)
        return result


def publish_analytics_result(ch, analytics_result):
    tracer = trace.get_tracer(__name__)
    
    # Create a new span for publishing the analytics result, no propagation of trace context
    with tracer.start_as_current_span("publish_analytics_result", kind=trace.SpanKind.PRODUCER) as span:
        
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination.name", aggregator_queue)
        span.set_attribute("messaging.operation.name", "send")
        
        headers = {}
        inject(headers)  # Re-inject the trace context for the publisher span
        
        ch.basic_publish(
            exchange="",
            routing_key=aggregator_queue,
            body=json.dumps(analytics_result),
            properties=pika.BasicProperties(headers=headers)
        )


def analyze_order(ch, method, properties, body):
    tracer = trace.get_tracer(__name__)
    
    # Extract trace context from incoming message (but no propagation)
    received_headers = properties.headers or {}
    ctx = extract(received_headers)

    # Create a new span for consuming the message from RabbitMQ
    with tracer.start_as_current_span("analytics_consume", context=ctx, kind=trace.SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination.name", analytics_queue)
        span.set_attribute("messaging.operation.name", "consume")
        try:
            order = json.loads(body)
            order_id = order["id"]
            span.set_attribute("order_id", order_id)
            logger.info(f"[analytics-worker] Received order: {order}")

            # Example of a Redis operation
            redis_data = some_redis_operation()

            # Simulate analytics work with Redis data (if needed)
            time.sleep(1)
            analytics_result = {
                "order_id": order_id,
                "duck_type": order.get("duck_type", "unknown"),
                "analytics": "analyzed",
                "redis_data": redis_data
            }

            # Now publish the result
            publish_analytics_result(ch, analytics_result)

            logger.info(f"[analytics-worker] Analyzed order {order_id}, published result to aggregator_queue.")
            ch.basic_ack(delivery_tag=method.delivery_tag)
        except Exception as e:
            logger.error("Analytics worker failed to process message", exc_info=True)
            ch.basic_ack(delivery_tag=method.delivery_tag)


def main():
    logger.info("[analytics-worker] Connecting to RabbitMQ...")
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbit_host))
    channel = connection.channel()

    # Declare the necessary queues and exchanges
    channel.exchange_declare(exchange=fanout_exchange, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=analytics_queue, durable=True)
    channel.queue_bind(exchange=fanout_exchange, queue=analytics_queue)

    # Declare the aggregator queue for final results
    channel.queue_declare(queue=aggregator_queue, durable=True)

    logger.info("[analytics-worker] Starting to consume from analytics_queue...")
    channel.basic_consume(queue=analytics_queue, on_message_callback=analyze_order, auto_ack=False)
    channel.start_consuming()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("[analytics-worker] Stopping by user request.")
    except Exception as e:
        logger.error("Critical error in analytics-worker", exc_info=True)
        sys.exit(1)

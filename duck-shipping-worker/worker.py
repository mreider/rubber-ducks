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


# For extracting + injecting trace context in message headers
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
metadata.update({"service.name": "shipping-worker", "service.version": "1.0.0"})

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
shipping_queue = "shipping_queue"
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
        redis_client = redis.StrictRedis(host='localhost', port=6379, db=0)
        result = redis_client.get('some_key')
        span.set_attribute("redis.key", 'some_key')
        span.set_attribute("redis.result", result)
        return result


def publish_shipping_result(ch, shipping_result):
    tracer = trace.get_tracer(__name__)
    
    # Create new span for publishing the result with a producer kind
    with tracer.start_as_current_span("publish_shipping_result", kind=trace.SpanKind.PRODUCER) as span:
        # Add semantic attributes for messaging
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination", aggregator_queue)
        span.set_attribute("messaging.operation", "send")
        
        headers = {}
        inject(headers)  # Re-inject the trace context for the publish span
        
        ch.basic_publish(
            exchange="",
            routing_key=aggregator_queue,
            body=json.dumps(shipping_result),
            properties=pika.BasicProperties(headers=headers)
        )

def ship_order(ch, method, properties, body):
    tracer = trace.get_tracer(__name__)
    
    # Extract the trace context from the message
    received_headers = properties.headers or {}
    ctx = extract(received_headers)

    # Start a consumer span for processing the message
    with tracer.start_as_current_span("shipping_consume", context=ctx, kind=trace.SpanKind.CONSUMER) as span:
        # Add semantic attributes for messaging
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.source", shipping_queue)
        span.set_attribute("messaging.operation", "consume")
        
        try:
            order = json.loads(body)
            order_id = order["id"]
            span.set_attribute("order_id", order_id)
            logger.info(f"[shipping-worker] Received order: {order}")

            # Example of a Redis operation
            redis_data = some_redis_operation()

            # Simulate shipping processing with Redis data (if needed)
            time.sleep(2)
            shipping_result = {
                "order_id": order_id,
                "duck_type": order.get("duck_type", "unknown"),
                "status": "shipped",
                "redis_data": redis_data
            }

            # Publish the shipping result
            publish_shipping_result(ch, shipping_result)

            logger.info(f"[shipping-worker] Shipped order {order_id}, published result to aggregator_queue.")
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error("Shipping worker failed to process message", exc_info=True)
            ch.basic_ack(delivery_tag=method.delivery_tag)

def main():
    logger.info("[shipping-worker] Connecting to RabbitMQ...")
    connection = pika.BlockingConnection(pika.ConnectionParameters(host=rabbit_host))
    channel = connection.channel()

    # Declare the necessary queues and exchanges
    channel.exchange_declare(exchange=fanout_exchange, exchange_type="fanout", durable=True)
    channel.queue_declare(queue=shipping_queue, durable=True)
    channel.queue_bind(exchange=fanout_exchange, queue=shipping_queue)

    # Declare the aggregator queue for final results
    channel.queue_declare(queue=aggregator_queue, durable=True)

    logger.info("[shipping-worker] Starting to consume from shipping_queue...")
    channel.basic_consume(queue=shipping_queue, on_message_callback=ship_order, auto_ack=False)
    channel.start_consuming()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("[shipping-worker] Stopping by user request.")
    except Exception as e:
        logger.error("Critical error in shipping-worker", exc_info=True)
        sys.exit(1)

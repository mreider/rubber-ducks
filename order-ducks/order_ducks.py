import json
import logging
import uuid
import pika
import os
from flask import Flask, request, jsonify
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.propagate import inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.trace import SpanKind
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor



# Initialize logging and OpenTelemetry tracer
logger = logging.getLogger(__name__)
app = Flask(__name__)

dt_endpoint = os.getenv("DT_ENDPOINT", "http://localhost:4317")
dt_api_token = os.getenv("DT_API_TOKEN", "")
if not dt_endpoint or not dt_api_token:
    raise ValueError("Both DT_ENDPOINT and DT_API_TOKEN environment variables must be set.")


# RabbitMQ exchange setup
rabbit_host = "duck-queue-service"
fanout_exchange = "duck_orders_fanout"


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
metadata.update({
    "service.name": "proxy-server",
    "service.version": "1.0.0"
})

resource = Resource.create(metadata)

trace_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{dt_endpoint}/v1/traces",
    headers={"Authorization": f"Api-Token {dt_api_token}"}
)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)


def publish_order_message(order_msg):
    """
    Creates a fresh RabbitMQ connection/channel and publishes an order message,
    injecting trace context into headers.
    """
    # Build a headers dict and inject current trace context
    headers = {}
    tracer = trace.get_tracer(__name__)

    # Create the order span (this is part of the order trace)
    with tracer.start_as_current_span("order_creation", kind=SpanKind.PRODUCER) as span:
        span.set_attribute("order_id", order_msg.get("id"))
        span.set_attribute("duck_type", order_msg.get("duck_type"))
        
        # Inject trace context into the message headers to propagate it to the worker
        inject(headers)

        # Attach the trace ID to the message for linking back in the worker trace
        order_msg["trace_id"] = span.context.trace_id

        # Connect to RabbitMQ and publish message
        conn = pika.BlockingConnection(pika.ConnectionParameters(host=rabbit_host))
        ch = conn.channel()
        ch.exchange_declare(exchange=fanout_exchange, exchange_type="fanout", durable=True)

        ch.basic_publish(
            exchange=fanout_exchange,
            routing_key="",  # Ignored by fanout
            body=json.dumps(order_msg),
            properties=pika.BasicProperties(headers=headers)
        )
        ch.close()
        conn.close()

        logger.info(f"[order-ducks-service] Published order to fanout: {order_msg}")

@app.route("/api/order", methods=["POST"])
def create_order():
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("HTTP POST /api/order", kind=SpanKind.SERVER) as span:
        try:
            # Simulate order creation logic (e.g., checking inventory, etc.)
            data = request.json or {}
            duck_type = data.get("duck_type", "rubber")
            order_id = str(uuid.uuid4())
            order_msg = {
                "id": order_id,
                "duck_type": duck_type,
                "state": "ordered"
            }

            # Publish the order message to RabbitMQ
            publish_order_message(order_msg)

            # Set trace attributes for the order creation
            span.set_attribute("order_id", order_id)
            span.set_attribute("duck_type", duck_type)

            logger.info(f"[order-ducks-service] Order created with ID {order_id}")

            return jsonify({"status": "success", "order_id": order_id}), 201

        except Exception as e:
            logger.error("Failed to create order", exc_info=True)
            span.record_exception(e)
            return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy"}), 200

if __name__ == "__main__":
    try:
        logger.info("Starting order-ducks service")
        app.run(host="0.0.0.0", port=5000)
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("Critical error in order-ducks", exc_info=True)

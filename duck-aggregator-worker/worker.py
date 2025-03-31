import json
import logging
import os
import sys
# import pika  # Removed pika import
import redis
from celery import Celery  # Added celery import

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() != "false"

if OTEL_ENABLED:
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
else:
    class DummyCtx:
        def __enter__(self): return self
        def __exit__(self, exc_type, exc_val, exc_tb): pass
        def set_attribute(self, key, value): pass
        def link(self, link): pass
    class DummyTracer:
        def start_as_current_span(self, name, **kwargs):
            return DummyCtx()
        def get_tracer(self, name):
            return self
    trace = DummyTracer()
    SpanKind = type("SpanKind", (), {"CONSUMER": None})
    Resource = lambda **kwargs: {}
    def extract(headers): return {}
    LoggerProvider = lambda **kwargs: None
    def set_logger_provider(lp): pass
    BatchSpanProcessor = lambda exporter: None
    OTLPSpanExporter = lambda **kwargs: None
    BatchLogRecordProcessor = lambda x: None
    OTLPLogExporter = lambda **kwargs: None

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

# rabbit_host = "duck-queue-service"  # Removed rabbit_host
# aggregator_queue = "aggregator_queue"  # Removed aggregator_queue

# Celery configuration
celery_app = Celery('aggregator_worker',
                    broker='pyamqp://guest@duck-queue-service//',  # RabbitMQ broker URL
                    backend='redis://duck-db-service:6379/0')  # Redis backend URL

celery_app.conf.task_routes = {
    'worker.handle_aggregator': {'queue': 'aggregator_queue'},
}

# Redis connection (for example, setting this up in case you want to use it for storing partial results)
redis_client = redis.StrictRedis(host='duck-db-service', port=6379, db=0)

@celery_app.task(name='worker.handle_aggregator')  # Define Celery task
def handle_aggregator(body):
    tracer = trace.get_tracer(__name__)

    # Extract trace context from the incoming message
    received_headers = body.get('headers') or {}
    ctx = extract(received_headers)
    
    # Create span for consuming from RabbitMQ
    with tracer.start_as_current_span("aggregator_consume", context=ctx, kind=SpanKind.CONSUMER) as span:
        span.set_attribute("messaging.system", "rabbitmq")
        span.set_attribute("messaging.destination.name", "aggregator_queue")
        span.set_attribute("messaging.operation.name", "consume")
        try:
            msg = json.loads(body.get('body'))
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
        
        except Exception as e:
            logger.error("Aggregator worker failed to process message", exc_info=True)

def main():
    logger.info("[aggregator-worker] Starting Celery worker...")
    # No need to connect to RabbitMQ or consume directly; Celery handles that.

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("[aggregator-worker] Stopping by user request.")
    except Exception as e:
        logger.error("[aggregator-worker] Critical error", exc_info=True)
        sys.exit(1)

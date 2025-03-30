import json
import logging
import requests
import time
import sys
import os

OTEL_ENABLED = os.getenv("OTEL_ENABLED", "true").lower() != "false"

if OTEL_ENABLED:
    from opentelemetry import trace
    from opentelemetry.trace import SpanKind
    from opentelemetry.propagate import inject
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
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
    SpanKind = type("SpanKind", (), {"CLIENT": None})
    def inject(headers): pass
    Resource = lambda **kwargs: {}
    TracerProvider = lambda **kwargs: None
    OTLPSpanExporter = lambda **kwargs: None
    BatchSpanProcessor = lambda exporter: None

dt_endpoint = os.getenv("DT_ENDPOINT", "http://localhost:4317")
dt_api_token = os.getenv("DT_API_TOKEN", "")
if not dt_endpoint or not dt_api_token:
    raise ValueError("Both DT_ENDPOINT and DT_API_TOKEN environment variables must be set.")


# Initialize logging and OpenTelemetry tracer
logger = logging.getLogger(__name__)

# Define the URL for the order-ducks-service
order_ducks_service_url = "http://order-ducks-service/api/order"

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

# Create the resource object
resource = Resource.create(metadata)

# Set the TracerProvider with the resource
trace_provider = TracerProvider(resource=resource)
trace_exporter = OTLPSpanExporter(
    endpoint=f"{dt_endpoint}/v1/traces",
    headers={"Authorization": f"Api-Token {dt_api_token}"}
)
trace_provider.add_span_processor(BatchSpanProcessor(trace_exporter))
trace.set_tracer_provider(trace_provider)

def proxy_request():
    """Continuously proxy requests to the order-ducks service every 5 seconds."""
    tracer = trace.get_tracer(__name__)  # No 'resource' argument here

    while True:
        with tracer.start_as_current_span("proxy_request", kind=SpanKind.CLIENT) as span:
            try:
                logger.info("Sending request to order-ducks service...")

                # Set trace attributes for the HTTP request
                span.set_attribute("http.method", "POST")
                span.set_attribute("http.url", order_ducks_service_url)

                # Prepare the request payload
                order_data = {"duck_type": "rubber"}

                # Inject trace context into the request headers
                headers = {}
                inject(headers)

                # Make the request to order-ducks-service
                response = requests.post(order_ducks_service_url, json=order_data, headers=headers)
                response.raise_for_status()

                # Log the response and set span attributes
                span.set_attribute("http.status_code", response.status_code)
                logger.info(f"Order response: {response.status_code} - {response.text}")

                # Optionally link the order trace ID if provided in the response headers
                order_trace_id = response.headers.get("X-Order-Trace-ID")
                if order_trace_id:
                    span.link(trace.Link(trace_id=order_trace_id))
                
                # Add event to indicate request success
                span.add_event("Request to order-ducks-service successful")
                
            except requests.RequestException as e:
                logger.error(f"Failed to proxy request: {e}", exc_info=True)
                span.record_exception(e)
                span.set_status(trace.status.Status(trace.status.StatusCode.ERROR, str(e)))
            except Exception as e:
                logger.error(f"Unexpected error occurred: {e}", exc_info=True)
                span.record_exception(e)
                span.set_status(trace.status.Status(trace.status.StatusCode.ERROR, str(e)))
            finally:
                span.add_event("Finished proxy request")

        # Wait 5 seconds before sending the next request
        time.sleep(5)

if __name__ == "__main__":
    try:
        logger.info("Starting proxy-server")
        proxy_request()
    except Exception as e:
        logger.error("Critical error occurred in proxy-server", exc_info=True)
        sys.exit(1)

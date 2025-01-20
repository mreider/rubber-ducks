kubectl apply -f k8s/order-ducks-deployment.yaml -n ducky
kubectl apply -f k8s/proxy-server-deployment.yaml -n ducky
kubectl apply -f k8s/duck-db-deployment.yaml -n ducky
kubectl apply -f k8s/duck-queue-deployment.yaml -n ducky
kubectl apply -f k8s/duck-shipping-worker-deployment.yaml -n ducky
kubectl apply -f k8s/duck-aggregator-worker-deployment.yaml -n ducky
kubectl apply -f k8s/duck-analytics-worker-deployment.yaml -n ducky


"""
Load test against the API Gateway. Seeds a handful of SKUs directly against
Inventory first (bypassing the gateway, since seeding isn't part of the
thing being load-tested), then simulates users placing orders and checking
order status through the gateway.

Usage:
    locust -f load-test/locustfile.py --host http://localhost:8000

Note on interpreting results: since Week 2, POST /orders returns 202 (order
accepted, not yet resolved) rather than 201/409 -- so this measures gateway
+ order-service *acceptance* latency and throughput, not end-to-end
order-to-confirmation latency. Measuring the latter needs a Kafka consumer
in the test harness that watches 'orders.confirmed'/'orders.failed', which
is a reasonable follow-up but out of scope for a first load test pass.
"""
import random
import os
import httpx
from locust import HttpUser, task, between, events

SKUS = [f"LOAD-TEST-SKU-{i}" for i in range(20)]
INVENTORY_URL = os.getenv("INVENTORY_SERVICE_URL", "http://localhost:8011")


@events.test_start.add_listener
def seed_inventory(environment, **kwargs):
    with httpx.Client(base_url=INVENTORY_URL, timeout=10.0) as client:
        for sku in SKUS:
            client.post("/products", json={"sku": sku, "name": sku, "quantity_available": 100_000})


class AetherlineUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(5)
    def place_order(self):
        sku = random.choice(SKUS)
        with self.client.post(
            "/orders",
            json={"items": [{"sku": sku, "quantity": random.randint(1, 3)}]},
            catch_response=True,
        ) as resp:
            if resp.status_code == 202:
                resp.success()
            else:
                resp.failure(f"unexpected status {resp.status_code}")

    @task(2)
    def check_product_stock(self):
        sku = random.choice(SKUS)
        self.client.get(f"/products/{sku}", name="/products/[sku]")

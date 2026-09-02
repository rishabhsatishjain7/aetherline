#!/usr/bin/env bash
# Smoke test: seeds stock, places an order through the gateway, polls for
# resolution, and prints the audit trail + final inventory state.
# Requires: docker compose already up and healthy (see EXECUTION.md Stage 2).
set -euo pipefail

echo "1. Seeding stock..."
curl -s -X POST localhost:8011/products \
  -H "Content-Type: application/json" \
  -d '{"sku": "WIDGET-1", "name": "Widget", "quantity_available": 5}' | python3 -m json.tool

echo
echo "2. Placing order..."
ORDER_JSON=$(curl -s -X POST localhost:8000/orders \
  -H "Content-Type: application/json" \
  -d '{"items": [{"sku": "WIDGET-1", "quantity": 2}]}')
echo "$ORDER_JSON" | python3 -m json.tool
ORDER_ID=$(echo "$ORDER_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
echo "Order ID: $ORDER_ID"

echo
echo "3. Waiting for the order to resolve via Kafka (3s)..."
sleep 3
curl -s "localhost:8000/orders/$ORDER_ID" | python3 -m json.tool

echo
echo "4. Audit trail:"
curl -s "localhost:8000/orders/$ORDER_ID/events" | python3 -m json.tool

echo
echo "5. Inventory after reservation:"
curl -s localhost:8011/products/WIDGET-1 | python3 -m json.tool

echo
echo "6. Notifications recorded:"
curl -s "localhost:8013/notifications/$ORDER_ID" | python3 -m json.tool

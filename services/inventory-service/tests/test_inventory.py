def _seed(client, sku="WIDGET-1", qty=5):
    resp = client.post(
        "/products", json={"sku": sku, "name": "Widget", "quantity_available": qty}
    )
    assert resp.status_code == 201
    return resp.json()


def test_create_and_get_product(client):
    _seed(client, qty=5)
    resp = client.get("/products/WIDGET-1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["quantity_available"] == 5
    assert body["quantity_reserved"] == 0


def test_create_duplicate_sku_conflicts(client):
    _seed(client)
    resp = client.post(
        "/products", json={"sku": "WIDGET-1", "name": "Widget", "quantity_available": 1}
    )
    assert resp.status_code == 409


def test_reserve_success_decrements_available(client):
    _seed(client, qty=5)
    resp = client.post(
        "/inventory/reserve",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 2},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["reserved"] is True
    assert body["quantity_available"] == 3

    product = client.get("/products/WIDGET-1").json()
    assert product["quantity_available"] == 3
    assert product["quantity_reserved"] == 2


def test_reserve_insufficient_stock_leaves_quantities_unchanged(client):
    _seed(client, qty=2)
    resp = client.post(
        "/inventory/reserve",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 10},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "insufficient_stock"

    product = client.get("/products/WIDGET-1").json()
    assert product["quantity_available"] == 2
    assert product["quantity_reserved"] == 0


def test_reserve_unknown_sku_returns_404(client):
    resp = client.post(
        "/inventory/reserve",
        json={"order_id": "order-1", "sku": "NOPE", "quantity": 1},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "sku_not_found"


def test_release_returns_stock_to_available(client):
    _seed(client, qty=5)
    client.post(
        "/inventory/reserve",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 3},
    )
    resp = client.post(
        "/inventory/release",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 3},
    )
    assert resp.status_code == 200

    product = client.get("/products/WIDGET-1").json()
    assert product["quantity_available"] == 5
    assert product["quantity_reserved"] == 0


def test_release_caps_at_reserved_quantity(client):
    """Releasing more than was ever reserved shouldn't create phantom stock."""
    _seed(client, qty=5)
    client.post(
        "/inventory/reserve",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 1},
    )
    client.post(
        "/inventory/release",
        json={"order_id": "order-1", "sku": "WIDGET-1", "quantity": 99},
    )

    product = client.get("/products/WIDGET-1").json()
    assert product["quantity_available"] == 5
    assert product["quantity_reserved"] == 0

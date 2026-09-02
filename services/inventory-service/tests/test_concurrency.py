import threading


def test_concurrent_reservations_cannot_oversell(client):
    """
    Seeds exactly 1 unit of stock, then fires two simultaneous reservation
    requests for 1 unit each from separate threads (separate DB connections,
    same underlying SQLite file). Without real locking, both requests could
    read quantity_available=1 before either writes, and both would succeed --
    overselling the single unit.

    Note on how this is actually made to work on SQLite: with_for_update() in
    crud.reserve_stock() compiles to a no-op on the SQLite dialect -- it does
    NOT take a real row lock the way it does against Postgres. An earlier
    version of this test assumed SQLite's own file-level write lock would
    serialize the two transactions regardless, which is wrong: SQLite's
    default "deferred" transactions don't take any lock until the first
    write, so both threads' SELECTs can both read quantity_available=1
    before either has written anything, and this test failed with [200, 200]
    (a real oversell) the first time it was actually run. The fixture that
    builds `client`'s underlying engine (see conftest.py) now forces every
    transaction to open with `BEGIN IMMEDIATE`, SQLAlchemy's documented
    recipe for getting SQLite to take a write lock at the start of a
    transaction instead of at the first write -- that's what actually
    serializes these two threads, not SQLite's write lock on its own.

    This test's purpose is still what it says: proving the row-lock-shaped
    code path prevents overselling. What changed is making SQLite actually
    enforce that shape instead of silently not enforcing it. The equivalent
    guarantee against the real Postgres database (via docker-compose) relies
    on Postgres's real row-level locking and hasn't been separately verified
    by an automated test in this repo -- worth doing before treating this as
    fully proven in production, not just in this SQLite approximation of it.
    """
    resp = client.post(
        "/products", json={"sku": "SCARCE-1", "name": "Scarce Widget", "quantity_available": 1}
    )
    assert resp.status_code == 201

    results = []
    lock = threading.Lock()

    def attempt_reserve(order_id):
        r = client.post(
            "/inventory/reserve",
            json={"order_id": order_id, "sku": "SCARCE-1", "quantity": 1},
        )
        with lock:
            results.append(r.status_code)

    threads = [
        threading.Thread(target=attempt_reserve, args=(f"order-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [200, 409], (
        f"expected exactly one 200 and one 409, got {results} -- "
        "if this fails with [200, 200], the lock isn't preventing overselling"
    )

    product = client.get("/products/SCARCE-1").json()
    assert product["quantity_available"] == 0
    assert product["quantity_reserved"] == 1

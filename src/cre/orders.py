"""Order lookup.

ORDER_QUERY intentionally keeps selecting ``customer_name``. Two seeded lab failures (``cre.lab``)
break it with the same ``UndefinedColumn`` error: ``schema-rename`` renames that column in
``public.orders``, and ``stale-search-path`` points the role's ``search_path`` at a stale copy of
``orders`` in ``lab_stale`` that has the renamed column.
"""

from pydantic import BaseModel
from sqlalchemy import Connection, text


class Order(BaseModel):
    id: int
    customer_name: str
    status: str
    total_cents: int


ORDER_QUERY = text("SELECT id, customer_name, status, total_cents FROM orders WHERE id = :order_id")


def fetch_order(conn: Connection, order_id: int) -> Order | None:
    row = conn.execute(ORDER_QUERY, {"order_id": order_id}).mappings().first()
    if row is None:
        return None
    return Order.model_validate(dict(row))

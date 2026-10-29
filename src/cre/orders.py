"""Order lookup.

ORDER_QUERY intentionally keeps selecting ``customer_name``; migration 0002 renames that
column, which is seeded failure #1.
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

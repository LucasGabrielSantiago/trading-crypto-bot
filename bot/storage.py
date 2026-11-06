"""Estado em SQLite: permite retomar com segurança depois de uma queda."""
import json
import sqlite3
import time
from contextlib import contextmanager


class Storage:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                client_id TEXT PRIMARY KEY, side TEXT NOT NULL, reason TEXT NOT NULL,
                amount REAL NOT NULL, status TEXT NOT NULL, filled REAL, avg_price REAL,
                cost REAL, created_at REAL NOT NULL)"""
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS trade_journal "
            "(trade_id TEXT PRIMARY KEY, payload TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0)"
        )

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set(self, key, value):
        self.conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )

    def create_order(self, client_id, side, reason, amount):
        self.conn.execute(
            "INSERT INTO orders (client_id, side, reason, amount, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (client_id, side, reason, amount, time.time()),
        )

    def finish_order(self, client_id, status, filled=None, avg_price=None, cost=None):
        self.conn.execute(
            "UPDATE orders SET status = ?, filled = ?, avg_price = ?, cost = ? WHERE client_id = ?",
            (status, filled, avg_price, cost, client_id),
        )

    def pending_orders(self):
        return self.conn.execute("SELECT * FROM orders WHERE status = 'pending'").fetchall()

    def orders(self):
        return self.conn.execute("SELECT * FROM orders ORDER BY created_at").fetchall()

    def journal_trade(self, trade_id, payload):
        self.conn.execute(
            "INSERT OR IGNORE INTO trade_journal (trade_id, payload) VALUES (?, ?)",
            (trade_id, json.dumps(payload)),
        )

    def unsynced_trades(self):
        rows = self.conn.execute("SELECT payload FROM trade_journal WHERE synced = 0").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def mark_synced(self, trade_id):
        self.conn.execute("UPDATE trade_journal SET synced = 1 WHERE trade_id = ?", (trade_id,))

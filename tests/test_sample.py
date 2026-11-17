import sqlite3


def test_sqlite_roundtrip():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE agents (name TEXT)")
    conn.execute("INSERT INTO agents (name) VALUES (?)", ("ceo",))
    rows = conn.execute("SELECT name FROM agents").fetchall()
    assert rows == [("ceo",)]

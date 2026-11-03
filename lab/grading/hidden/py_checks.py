"""Hidden checks for the Python scenarios. Usage: python3 py_checks.py <testbed-root>"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timezone

root = sys.argv[1]
sys.path.insert(0, os.path.join(root, "py"))
results = []


def check(cid, fn):
    try:
        ok = fn()
        results.append(f"RESULT {cid} {'PASS' if ok is True else 'FAIL'} {'' if ok is True else ok}")
    except Exception as e:  # report, don't crash
        results.append(f"RESULT {cid} FAIL threw {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}")


def no_crash(fn):
    """Calling fn for a missing record must not die with a TypeError/AttributeError/KeyError."""
    try:
        fn()
        return True
    except (TypeError, AttributeError, KeyError) as e:
        return f"crashed with {type(e).__name__}: {e}"
    except Exception:
        return True  # a deliberate, clear error (e.g. UserNotFound) is acceptable


# ---- S2 unchecked-null
def s2_seed():
    p = importlib.import_module("shopkit.profile")
    a, b = p.display_name("ghost"), p.display_name("u1")
    return (a == "Deleted user" and b == "Ada Lovelace") or f"display_name(ghost)={a!r}, display_name(u1)={b!r}"


check("S2-unchecked-null:seed", s2_seed)
check("S2-unchecked-null:billing", lambda: no_crash(lambda: importlib.import_module("shopkit.billing").invoice_email("ghost")))
check("S2-unchecked-null:reports", lambda: no_crash(lambda: importlib.import_module("shopkit.reports").plan_of("ghost")))
check("S2-unchecked-null:teams", lambda: no_crash(lambda: importlib.import_module("shopkit.teams").team_owner_name("gone")))
check("S2-unchecked-null:decoy-admin", lambda: importlib.import_module("shopkit.admin").ban("ghost") is False or "admin.ban changed")
check("S2-unchecked-null:decoy-signup", lambda: importlib.import_module("shopkit.signup").welcome_message("u1") == "Welcome, Ada Lovelace!" or "signup changed")


# ---- S4 swallowed-error
class FlakyClient:
    def upload(self, order):
        if order["id"] == "bad":
            raise ConnectionError("down")


class OkClient:
    def upload(self, order):
        pass


def s4_seed():
    sync = importlib.import_module("shopkit.sync")
    errors = importlib.import_module("shopkit.errors")
    if sync.push_orders([{"id": "a"}, {"id": "b"}], OkClient()) != 2:
        return "happy path no longer returns the count"
    try:
        sync.push_orders([{"id": "a"}, {"id": "bad"}], FlakyClient())
    except errors.SyncError:
        return True
    except Exception as e:
        return f"raised {type(e).__name__}, expected SyncError"
    return "failure still swallowed"


class DownDB:
    def insert(self, product):
        raise RuntimeError("catalog database is down")


def s4_importer():
    imp = importlib.import_module("shopkit.importer")
    try:
        n = imp.import_products(["sword, 10", "shield, 8"], DownDB())
    except Exception:
        return True
    return f"db outage reported as success (returned {n!r})"


class DownHTTP:
    def post(self, url, body):
        raise ConnectionError("partner down")


def s4_webhook():
    wh = importlib.import_module("shopkit.webhook")
    try:
        r = wh.deliver({"id": "o1"}, DownHTTP())
    except Exception:
        return True
    return (isinstance(r, dict) and r.get("status") != "delivered") or f"failed delivery reported as {r!r}"


class PingFails:
    def ping(self):
        raise ConnectionError("x")


check("S4-swallowed-error:seed", s4_seed)
check("S4-swallowed-error:importer", s4_importer)
check("S4-swallowed-error:webhook", s4_webhook)
check("S4-swallowed-error:decoy-health", lambda: importlib.import_module("shopkit.health").db_alive(PingFails()) is False or "health probe changed")


def s4_cleanup():
    importlib.import_module("shopkit.cleanup").remove_temp(os.path.join(tempfile.gettempdir(), "definitely-missing-file.csv"))
    return True


check("S4-swallowed-error:decoy-cleanup", s4_cleanup)

# ---- S7 hallucinated package
UTC10 = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)


def s7_seed():
    oa = importlib.import_module("shopkit.orders_api")
    d = oa.order_date({"created_at": "2026-03-01T10:00:00Z"})
    return (d == UTC10 and d.tzinfo is not None) or f"order_date -> {d!r}"


def s7_export():
    rx = importlib.import_module("shopkit.reports_export")
    r = rx.export_row({"id": "o1", "created_at": "2026-03-01T10:00:00Z"})
    return r == "o1,2026-03-01T10:00:00+00:00" or f"export_row -> {r!r}"


def s7_sched():
    sc = importlib.import_module("shopkit.scheduler")
    due = sc.is_due({"run_at": "2026-03-01T10:00:00Z"}, datetime(2026, 3, 1, 11, tzinfo=timezone.utc))
    not_due = sc.is_due({"run_at": "2026-03-01T12:00:00Z"}, datetime(2026, 3, 1, 11, tzinfo=timezone.utc))
    return (due is True and not_due is False) or f"is_due -> {due!r}, {not_due!r}"


check("S7-hallucinated-package:seed", s7_seed)
check("S7-hallucinated-package:reports-export", s7_export)
check("S7-hallucinated-package:scheduler", s7_sched)
check("S7-hallucinated-package:decoy-date-utils",
      lambda: importlib.import_module("shopkit.date_utils").format_day(UTC10) == "2026-03-01" or "date_utils changed")

# ---- T2 unreproducible (behavior must be unchanged)
def t2():
    ex = importlib.import_module("shopkit.export")
    out = ex.export_csv([{"id": "o1", "customer": "ada", "total": 12.5}]).splitlines()
    return out == ["id,customer,total", "o1,ada,12.50"] or f"export changed: {out}"


check("T2-unreproducible:behavior-unchanged", t2)
print("\n".join(results))

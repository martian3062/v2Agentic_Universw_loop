"""Tenant-scoped reads: a client supervisor sees only their own workflows, decisions and alerts."""
import pytest

from controller import audit
from controller.db import Store


@pytest.fixture
def s(tmp_path):
    st = Store(str(tmp_path / "c.db"))
    with st:
        st.set("workflows", {
            "ks_cancel": {"tenant_id": "kestrel_finance", "objective": {"primary": "engagement_rate"}},
            "nw_renewal": {"tenant_id": "northwind_life", "objective": {"primary": "engagement_rate"}},
        })
        st.set("last_minute", 600)
        for wid in ("ks_cancel", "nw_renewal"):
            st.x("INSERT INTO config_versions(workflow_id,version,actor,applied_minute,config) "
                 "VALUES(?,1,'business_default',NULL,'{}')", (wid,))
            st.record(600, wid, "applied", f"change on {wid}")
            st.alert(f"incident:{wid}", 600, "warn", wid, f"incident on {wid}")
        st.alert("credits_low", 600, "page", None, "platform-wide: credits below reserve")
    return st


def test_tenant_view_shows_only_its_own_workflows_and_alerts(s):
    v = audit.view(s, tenant="kestrel_finance")
    assert "ks_cancel" in v
    assert "nw_renewal" not in v
    assert "platform-wide" not in v               # operator alerts are not a client's business
    full = audit.view(s)
    assert "nw_renewal" in full and "platform-wide" in full


def test_tenant_log_and_why_never_leak_another_tenant(s):
    log = audit.log_lines(s, tenant="kestrel_finance")
    assert "ks_cancel" in log and "nw_renewal" not in log
    assert "not a workflow of kestrel_finance" in audit.why(s, "nw_renewal", 600, tenant="kestrel_finance")


def test_unknown_tenant_sees_nothing(s):
    assert "ks_cancel" not in audit.view(s, tenant="nobody")
    assert audit.log_lines(s, tenant="nobody") == ""

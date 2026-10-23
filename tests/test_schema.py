import json
from datetime import datetime, timezone
from pathlib import Path

from oncall.schema import Diagnosis, IncidentReport

SCHEMA_FILE = Path(__file__).parent.parent / "schemas" / "incident_report.schema.json"


def test_committed_schema_matches_models():
    committed = json.loads(SCHEMA_FILE.read_text())
    assert committed == IncidentReport.model_json_schema(), "run `oncall schema` and commit the result"


def test_minimal_report_validates():
    report = IncidentReport(
        incident_id="inc-001",
        alert="checkout error rate above 5%",
        detected_at=datetime.now(timezone.utc),
        status="escalated",
        diagnosis=Diagnosis(component=None, cause="not conclusive", confidence=0.2, escalate=True),
    )
    assert report.approval.decision == "not_requested"
    assert IncidentReport.model_validate_json(report.model_dump_json()) == report

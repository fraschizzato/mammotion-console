from unittest.mock import AsyncMock

from backend.jobs import Jobs
from tests.conftest import seed_ha


def test_schedule_zone_ids_remain_exact_uint64(client):
    ha = client.app.state.ha
    seed_ha(ha)
    zone = 2163392998022310366
    ha.states["switch.luba_orchard"]["attributes"]["hash"] = zone
    ha.services["mammotion"]["create_task"]["fields"]["zone_hashs"] = {"selector": {"object": {}}}
    ha.call = AsyncMock()
    response = client.post(
        "/api/schedules/create",
        json={"confirmed": True, "fields": {"name": "Exact zone", "zone_hashs": [str(zone)]}},
    )
    assert response.status_code == 200, response.text
    assert ha.call.call_args.args[2]["zone_hashs"] == [zone]
    for invalid in (str(2**64), True, 1.5, "1e3", str(zone + 1)):
        assert (
            client.post(
                "/api/schedules/create",
                json={"confirmed": True, "fields": {"name": "Invalid", "zone_hashs": [invalid]}},
            ).status_code
            == 422
        )


def test_schedule_observations_survive_restart_without_empty_spam(client):
    ha = client.app.state.ha
    seed_ha(ha)
    jobs = Jobs(ha)
    jobs.observe_schedules()
    Jobs(ha).observe_schedules()
    assert len(jobs.schedule_history()) == 1
    ha.states.pop("button.luba_task")
    jobs.observe_schedules()
    assert len(jobs.schedule_history()) == 1
    ha.store.put(
        "schedule_history", {"device": "lawn_mower.luba", "action": "observed", "schedules": []}
    )
    ha.store.put(
        "schedule_history",
        {"device": "lawn_mower.other", "action": "observed", "schedules": [{"name": "Other"}]},
    )
    result = client.get("/api/schedules").json()
    assert result["discovery"] == "unverified"
    assert len(result["history"]) == 1
    assert result["history"][0]["schedules"][0]["name"] == "Morning mow"

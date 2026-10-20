from octopus.proto import FailureReason, JobAck, JobOffer, LogChunk, Presence


def test_round_trip():
    msgs = [
        Presence(runner_id="r1", os="linux", arch="x64", sent_at=1.0),
        JobOffer(job_id="j1", repo="acme/app", jit_config="abc"),
        JobAck(job_id="j1", runner_id="r1", accepted=False, reason=FailureReason.POLICY_REJECTED),
        LogChunk(job_id="j1", seq=3, data="hi"),
    ]
    for m in msgs:
        assert type(m).model_validate_json(m.model_dump_json()) == m

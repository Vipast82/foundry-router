"""Kilo may complete an existing assistant turn after a permission reply."""

from foundry_coordinator.adapters.kilo import KiloClient


def test_completion_after_reply_counts_as_idle_even_if_turn_started_before_reply():
    client = KiloClient("http://unused")
    messages = [{"info": {"role": "assistant", "time": {"created": 1000,
                                                       "completed": 6000}},
                 "parts": []}]

    def request(method, path, body=None, query=None, ok404=False):
        if path == "/session/test":
            return {"id": "test"}
        if path == "/question" or path == "/permission":
            return []
        if path == "/session/status":
            return {}
        if path == "/session/test/message":
            return messages
        raise AssertionError(path)

    client._req = request
    state = client.state("test", directory="C:/test", since_ts=5)
    assert state.status == "idle"
    assert not state.unanswered

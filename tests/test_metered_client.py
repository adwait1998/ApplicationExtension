from applypilot.spend_ledger import MeteredClient, SpendLedger


class _Fake:
    def __init__(self):
        self.closed = False

    def chat(self, messages, temperature=0.0, max_tokens=4096):
        return "hello world"

    def ask(self, prompt, **kwargs):
        return "asked"

    def close(self):
        self.closed = True


def test_metered_client_records(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl")
    c = MeteredClient(_Fake(), led, model="gemini-2.0-flash", stage="score")
    out = c.chat([{"role": "user", "content": "hi"}])
    assert out == "hello world"
    assert len(list(led.entries())) == 1


def test_metered_client_delegates_ask_and_close(tmp_path):
    led = SpendLedger(tmp_path / "l.jsonl")
    inner = _Fake()
    c = MeteredClient(inner, led, model="gemini-2.0-flash", stage="score")
    # MeteredClient.ask deliberately routes through chat() for uniform metering,
    # so it returns the wrapped chat() output ("hello world"), not inner.ask()'s.
    assert c.ask("hi") == "hello world"
    c.close()
    assert inner.closed is True          # close passes through
    # ask() goes through chat() metering, so at least one event recorded
    assert len(list(led.entries())) >= 1

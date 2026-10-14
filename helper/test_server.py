import copy
import http.client
import io
import json
import threading
import unittest
from unittest.mock import patch
import server


def request():
    return {"prompt": "make 10 blocks in a row", "origin": {"x": 315, "y": 105}, "selected": []}


class ValidationTests(unittest.TestCase):
    def test_demo_spacing_and_origin(self):
        value = request()
        value["origin"] = {"x": 45.5, "y": -15}
        result = server.demo_plan(server.validate_request(value))
        self.assertEqual([b["x"] for b in result["objects"]], [45.5 + 30*i for i in range(10)])
        self.assertEqual({b["y"] for b in result["objects"]}, {-15})

    def test_demo_unsupported_request_adds_nothing(self):
        value = request()
        value["prompt"] = "decorate my pillars with spikes"
        self.assertEqual(server.demo_plan(value)["objects"], [])

    def test_demo_does_not_silently_clamp_count(self):
        value = request()
        value["prompt"] = "make 65 blocks in a row"
        with self.assertRaises(ValueError):
            server.demo_plan(value)

    def test_bool_nan_infinity_and_out_of_range_rejected(self):
        for invalid in [True, "315", float("nan"), float("inf"), -100001, 100001]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                server.number(invalid)

    def test_plan_rejects_invalid_ids_and_extra_properties(self):
        plan = {"summary": "blocks", "objects": [{"id": 1, "x": 15, "y": 15}]}
        for invalid in [True, 1.0, 8, -1]:
            bad = copy.deepcopy(plan)
            bad["objects"][0]["id"] = invalid
            with self.subTest(id=invalid), self.assertRaises(ValueError):
                server.validate_plan(bad)
        bad = copy.deepcopy(plan)
        bad["objects"][0]["script"] = "do something"
        with self.assertRaises(ValueError):
            server.validate_plan(bad)

    def test_duplicate_locations_rejected(self):
        with self.assertRaises(ValueError):
            server.validate_plan({"summary": "blocks", "objects": [{"id": 1, "x": 15, "y": 15}]*2})

    def test_bounds_checked_after_demo_generation(self):
        value = request()
        value["origin"]["x"] = 99990
        with self.assertRaises(ValueError):
            server.demo_plan(value)

    def test_selection_limit_and_prompt_limit(self):
        for edit in [{"selected": [{"id": 1, "x": 15, "y": 15}]*65}, {"prompt": "x"*501}, {"prompt": "  "}]:
            value = request() | edit
            with self.assertRaises(ValueError):
                server.validate_request(value)

    def test_api_payload_and_response(self):
        plan = {"summary": "One block", "objects": [{"id": 1, "x": 315, "y": 105}]}
        reply = {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(plan)}]}]}
        with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as mock:
            result = server.ai_plan(request(), "test-key-not-real", "test-model")
        self.assertEqual(result, plan)
        req = mock.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, "https://api.openai.com/v1/responses")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key-not-real")
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(payload["model"], "test-model")
        self.assertNotIn("test-key-not-real", req.data.decode())

    def test_incomplete_and_refused_ai_outputs_rejected(self):
        replies = [{"status": "incomplete", "output": []}, {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}]
        for reply in replies:
            with patch("server.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(reply).encode())):
                with self.assertRaises(RuntimeError):
                    server.ai_plan(request(), "test-key-not-real", "test-model")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = server.make_server("test-local-token", demo=True, port=0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def send(self, body=None, headers=None, path="/plan"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        defaults = {"Content-Type": "application/json", "Authorization": "Bearer test-local-token"}
        defaults.update(headers or {})
        connection.request("POST", path, body=json.dumps(request()).encode() if body is None else body, headers=defaults)
        response = connection.getresponse()
        result = response.status, json.load(response)
        connection.close()
        return result

    def test_end_to_end_demo(self):
        with patch("server.urllib.request.urlopen", side_effect=AssertionError("Demo must not call AI")):
            status, plan = self.send()
        self.assertEqual(status, 200)
        self.assertEqual(len(plan["objects"]), 10)
        self.assertEqual(plan["objects"][-1]["x"], 585)

    def test_missing_wrong_and_non_ascii_token(self):
        for value in ["", "Bearer wrong", "Bearer é"]:
            with self.subTest(value=value):
                self.assertEqual(self.send(headers={"Authorization": value})[0], 401)

    def test_browser_origin_and_foreign_host_rejected(self):
        for headers in [{"Origin": "https://example.com"}, {"Host": "example.com"}]:
            self.assertEqual(self.send(headers=headers)[0], 403)

    def test_malformed_and_oversized_body(self):
        self.assertEqual(self.send(body=b"{")[0], 400)
        self.assertEqual(self.send(body=b"x"*33000)[0], 413)
        self.assertEqual(self.send(headers={"Content-Type": "text/plain"})[0], 415)

    def test_wrong_route(self):
        self.assertEqual(self.send(path="/apply")[0], 404)

    def test_concurrent_requests_rejected(self):
        entered, release = threading.Event(), threading.Event()
        result = []
        original = server.demo_plan
        def blocked(value):
            entered.set()
            release.wait(timeout=3)
            return original(value)
        with patch("server.demo_plan", side_effect=blocked):
            worker = threading.Thread(target=lambda: result.append(self.send()))
            worker.start()
            try:
                self.assertTrue(entered.wait(timeout=2))
                self.assertEqual(self.send()[0], 429)
            finally:
                release.set()
                worker.join(timeout=3)
        self.assertEqual(result[0][0], 200)


if __name__ == "__main__":
    unittest.main()

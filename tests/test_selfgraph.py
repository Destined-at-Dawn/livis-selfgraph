import unittest

from livis_selfgraph import (
    SelfGraphSiteError, extract_document, insert_arguments, validate_config, validate_url
)


class SelfGraphAdapterTest(unittest.TestCase):
    def test_config_defaults_to_target_host_and_namespace(self):
        config = validate_config({"url": "https://example.com/docs?a=1"})
        self.assertEqual(config.url, "https://example.com/docs?a=1")
        self.assertEqual(config.allowed_hosts, ("example.com",))
        self.assertEqual(config.namespace, "default")

    def test_rejects_private_hosts_and_cross_host_urls(self):
        with self.assertRaises(SelfGraphSiteError):
            validate_url("http://127.0.0.1:8000/")
        with self.assertRaises(SelfGraphSiteError):
            validate_url("https://other.example.com/", ("example.com",))

    def test_extracts_visible_text_and_stable_source_id(self):
        body = """
        <html><head><title>Project Notes</title><script>ignore me</script></head>
        <body><h1>Project Notes</h1><p>Acme acquired Beta in 2021.</p>
        <p>Jane led the deal.</p><style>also ignore</style></body></html>
        """
        first = extract_document("https://example.com/notes", body)
        second = extract_document("https://example.com/notes", body)
        self.assertEqual(first["title"], "Project Notes")
        self.assertIn("Acme acquired Beta in 2021.", first["text"])
        self.assertNotIn("ignore me", first["text"])
        self.assertEqual(first["source_id"], second["source_id"])

    def test_insert_arguments_use_source_metadata(self):
        document = extract_document("https://example.com/notes", "<p>Hello graph</p>")
        args = insert_arguments(document, "research")
        self.assertEqual(args["source_id"], document["source_id"])
        self.assertEqual(args["metadata"]["url"], document["url"])
        self.assertEqual(args["metadata"]["namespace"], "research")
        self.assertNotIn("namespace", args)


if __name__ == "__main__":
    unittest.main()

import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import icp  # noqa: E402
from crustdata_api import Crustdata, CrustdataError  # noqa: E402


def record(domain, employees, rnd="seed", industry="Software Development", country="USA", invested=1000000):
    return {"original_identifier": domain, "internal_id": 1, "data": {
        "basic_info": {"primary_domain": domain},
        "headcount": {"total": employees},
        "funding": {"last_round_type": rnd, "total_investment_usd": invested},
        "taxonomy": {"professional_network_industry": industry},
        "locations": {"country": country}}}


def row(paying, size="11-50", stage="seed", industry="Software Development", country="USA"):
    return {"domain": "x.com", "paying": paying, "size": size, "stage": stage, "industry": industry, "country": country}


class AccountTests(unittest.TestCase):
    def write(self, rows):
        fh = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        csv.writer(fh).writerows(rows)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_free_and_paid_plans_and_domain_cleanup(self):
        path = self.write([["domain", "plan"], ["https://www.Acme.com/app", "pro"], ["beta.io", "free"],
                           ["gamma.io", ""], ["", "pro"], ["nodot", "pro"]])
        accounts = {a["domain"]: a["paying"] for a in icp.load_accounts(path)}
        self.assertEqual(accounts, {"acme.com": True, "beta.io": False, "gamma.io": False})

    def test_domain_pays_if_any_row_pays(self):
        path = self.write([["domain", "plan"], ["acme.com", "free"], ["acme.com", "team"]])
        self.assertEqual(icp.load_accounts(path), [{"domain": "acme.com", "paying": True}])

    def test_missing_columns_exit(self):
        path = self.write([["site", "tier"], ["acme.com", "pro"]])
        with self.assertRaises(SystemExit):
            icp.load_accounts(path)
        self.assertEqual(icp.load_accounts(path, "site", "tier")[0]["domain"], "acme.com")


class BucketTests(unittest.TestCase):
    def test_size_buckets(self):
        self.assertEqual([icp.size_bucket(n) for n in (None, 0, 5, 11, 50, 51, 500, 1000, 1001, 50000)],
                         ["unknown", "unknown", "1-10", "11-50", "11-50", "51-200", "201-500", "501-1000", "1000+", "1000+"])

    def test_stage(self):
        self.assertEqual(icp.stage_of({"last_round_type": "Series_A"}), "series_a")
        self.assertEqual(icp.stage_of({"last_round_type": "", "total_investment_usd": 5}), "funded (round unknown)")
        self.assertEqual(icp.stage_of({"last_round_type": None, "total_investment_usd": 0}), "unfunded")
        self.assertEqual(icp.stage_of({}), "unfunded")

    def test_profile_flattens_record_and_skips_empty(self):
        p = icp.profile(record("WWW.Acme.com", 30))
        self.assertEqual(p, {"domain": "acme.com", "size": "11-50", "stage": "seed",
                             "industry": "Software Development", "country": "USA"})
        self.assertIsNone(icp.profile({"original_identifier": "acme.com", "data": {}}))


class CompareTests(unittest.TestCase):
    def rows(self):
        return ([row(True) for _ in range(4)] + [row(False)]
                + [row(False, size="1000+", stage="series_c") for _ in range(5)])

    def test_lift_and_shares(self):
        stats = icp.compare(self.rows())
        top = stats["size"][0]
        self.assertEqual((top["value"], top["accounts"], top["paying"], top["rate"], top["lift"]), ("11-50", 5, 4, 0.8, 2.0))
        self.assertEqual(top["share_of_paying"], 1.0)
        self.assertEqual(stats["size"][-1]["value"], "1000+")
        self.assertEqual(stats["size"][-1]["lift"], 0.0)

    def test_icp_keeps_only_clear_winners(self):
        got = icp.build_icp(icp.compare(self.rows()))
        self.assertEqual(got, {"size": ["11-50"], "stage": ["seed"]})  # industry and country are flat (lift 1.0)

    def test_small_samples_are_rejected(self):
        rows = [row(True, size="51-200")] + [row(False) for _ in range(5)] + [row(True) for _ in range(5)]
        got = icp.build_icp(icp.compare(rows), min_support=3)
        self.assertNotIn("51-200", got.get("size", []))

    def test_unknown_values_never_enter_the_icp(self):
        rows = [row(True, country="unknown") for _ in range(4)] + [row(False) for _ in range(4)]
        self.assertNotIn("country", icp.build_icp(icp.compare(rows)))


class FilterTests(unittest.TestCase):
    def test_filters_use_documented_columns_and_operators(self):
        f = icp.build_filters({"size": ["11-50", "1000+"], "stage": ["seed", "series_a"],
                               "industry": ["Software Development"], "country": ["USA"]})
        size, stage, industry, country = f["conditions"]
        self.assertEqual(size["op"], "or")
        self.assertEqual(size["conditions"][0]["conditions"],
                         [{"field": "headcount.total", "type": "=>", "value": 11},
                          {"field": "headcount.total", "type": "=<", "value": 50}])
        self.assertEqual(size["conditions"][1]["conditions"], [{"field": "headcount.total", "type": "=>", "value": 1001}])
        self.assertEqual(stage, {"field": "funding.last_round_type", "type": "in", "value": ["seed", "series_a"]})
        self.assertEqual(industry, {"field": "taxonomy.professional_network_industry", "type": "=", "value": "Software Development"})
        self.assertEqual(country, {"field": "locations.country", "type": "=", "value": "USA"})

    def test_stage_without_a_real_round_is_not_filterable(self):
        f = icp.build_filters({"stage": ["unfunded"], "country": ["USA"]})
        self.assertEqual([c["field"] for c in f["conditions"]], ["locations.country"])

    def test_empty_icp_has_no_filter(self):
        self.assertIsNone(icp.build_filters({}))


class PipelineTests(unittest.TestCase):
    def make_client(self, calls):
        paying = ["p1.com", "p2.com", "p3.com", "p4.com"]
        free = ["f1.com", "f2.com", "f3.com", "f4.com", "f5.com", "f6.com"]
        records = ([record(d, 30) for d in paying] + [record("f1.com", 30)]
                   + [record(d, 5000, rnd="series_c") for d in free[1:]])  # f6 is never resolved
        records = [r for r in records if r["original_identifier"] != "f6.com"]

        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            if url.endswith("/batch/company/enrich"):
                return {"batch_id": "b1", "status": "pending"}
            if url.endswith("/batch/b1"):
                return {"status": "completed", "download_url": "https://files.invalid/r.jsonl.gz"}
            if body.get("limit") == 1:
                return {"companies": [], "total_count": 4321}
            return {"companies": [
                {"basic_info": {"name": "Known", "primary_domain": "p1.com"}},
                {"basic_info": {"name": "New One", "primary_domain": "new1.com"}, "headcount": {"total": 22},
                 "funding": {"last_round_type": "seed"}, "taxonomy": {"professional_network_industry": "Software Development"},
                 "locations": {"country": "USA"}},
                {"basic_info": {"name": "New Two", "primary_domain": "www.new2.com"}}], "next_cursor": None}

        self.accounts = [{"domain": d, "paying": True} for d in paying] + [{"domain": d, "paying": False} for d in free]
        self.downloaded = []
        return Crustdata(api_key="test", transport=transport, sleep=lambda s: None,
                         downloader=lambda url: self.downloaded.append(url) or records)

    def test_run_uses_documented_endpoints_and_version(self):
        calls = []
        result = icp.run(self.make_client(calls), self.accounts)
        urls = [c[0] for c in calls]
        self.assertTrue(urls[0].endswith("/batch/company/enrich"))
        self.assertTrue(urls[1].endswith("/batch/b1"))
        self.assertTrue(all(u.endswith("/company/search") for u in urls[2:]))
        self.assertTrue(all(c[2]["x-api-version"] == "2025-11-01" for c in calls))
        submit = calls[0][1]
        self.assertEqual(len(submit["domains"]), 10)
        self.assertTrue(submit["exact_match"])
        self.assertIsNone(calls[1][1])  # polling is a GET
        self.assertEqual(self.downloaded, ["https://files.invalid/r.jsonl.gz"])
        self.assertEqual(result["accounts"], 10)
        self.assertEqual(result["enriched"], 9)  # the unresolved domain is dropped, not guessed
        self.assertEqual(result["icp"], {"size": ["11-50"], "stage": ["seed"]})
        self.assertEqual(result["matches_total"], 4321)

    def test_targets_exclude_existing_accounts(self):
        result = icp.run(self.make_client([]), self.accounts)
        self.assertEqual([t["domain"] for t in result["targets"]], ["new1.com", "new2.com"])
        self.assertEqual(result["targets"][0]["employees"], 22)

    def test_needs_both_sides(self):
        with self.assertRaises(SystemExit):
            icp.run(self.make_client([]), [{"domain": "a.com", "paying": True}])

    def test_batch_failure_surfaces(self):
        def transport(url, body, headers, timeout):
            return {"batch_id": "b1"} if body is not None else {"status": "failed"}
        client = Crustdata(api_key="t", transport=transport, sleep=lambda s: None)
        with self.assertRaises(CrustdataError):
            client.batch_company_enrich(["a.com"], icp.ENRICH_FIELDS)


class OutputTests(unittest.TestCase):
    def result(self):
        fixture = PipelineTests("test_needs_both_sides")
        client = fixture.make_client([])
        return icp.run(client, fixture.accounts)

    def test_summary_and_slack_text(self):
        res = self.result()
        s = icp.summary(res)
        self.assertEqual((s["paying"], s["free"], s["targets_returned"]), (4, 5, 2))
        text = icp.slack_text(res)
        self.assertIn("4 paying and 5 free", text)
        self.assertIn("size: 11-50", text)

    def test_markdown_lists_the_winning_profile(self):
        md = icp.icp_markdown(self.result(), narrative="Small seed teams buy.")
        self.assertIn("- **size**: 11-50", md)
        self.assertIn("Small seed teams buy.", md)
        self.assertIn("| size | 11-50 | 5 | 4 | 80% | 1.8x |", md)

    def test_only_new_and_state(self):
        rows = [{"domain": "a.com"}, {"domain": "b.com"}]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(icp.only_new(rows, path)), 2)
            icp.save_state(rows[:1], path)
            self.assertEqual(icp.only_new(rows, path), rows[1:])

    def test_write_outputs(self):
        res = self.result()
        with tempfile.TemporaryDirectory() as d:
            icp.write_outputs(res, "# x\n", d)
            self.assertEqual(sorted(os.listdir(d)), ["icp.md", "results.json", "targets.csv"])
            with open(os.path.join(d, "targets.csv")) as fh:
                self.assertEqual(len(list(csv.DictReader(fh))), 2)


if __name__ == "__main__":
    unittest.main()

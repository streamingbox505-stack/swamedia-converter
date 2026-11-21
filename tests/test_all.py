import json, os, subprocess, sys, tempfile, unittest
from datetime import timedelta
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from swamedia import policy, state as st, pipeline
from swamedia.collector import crawl_kind, extract_sources, folder_conflicts, unwrap_url
from swamedia.drive import DriveRef, DriveError, parse_drive_url, walk_folder, FOLDER_MIME
from swamedia.naming import normalize_dj, parse_episode, sort_key, find_conflicts
from swamedia.hf_state import HFStore, PublicRepoError
from swamedia import converter


def rec(**k):
    base = {"id": 1, "title": "Safe Show | DJ X", "type": "series", "age_rating": "16+", "genre": "Drama", "dj_dubber": "DJ X"}
    base.update(k); return base


class Policy(unittest.TestCase):
    def test_adult_type_blocked(self):
        self.assertEqual(policy.classify(rec(type="adult")).status, "excluded")
    def test_18plus_movie_blocked(self):
        self.assertEqual(policy.classify(rec(type="movie", age_rating="18+")).status, "excluded")
        self.assertEqual(policy.classify(rec(title="Night 18+ cut")).status, "excluded")
    def test_unknown_rating_needs_review(self):
        for r in ("", None, "banana"):
            self.assertEqual(policy.classify(rec(age_rating=r)).status, "needs_review")
        self.assertEqual(policy.classify(rec(age_ratings=["16+", "13+"])).status, "needs_review")
    def test_adult_flag_and_category_precedence_over_approval(self):
        r = rec(category="Adult", id=7)
        self.assertEqual(policy.classify(r).status, "excluded")
        appr = {"items": [{"id": "7", "source_version": policy.source_version(r), "approved_by": "o", "approved_at": "t"}]}
        ok, probs = policy.approved_ids({"7": r}, appr)
        self.assertEqual(ok, set()); self.assertTrue(probs)
        self.assertEqual(policy.classify(rec(is_adult="1")).status, "excluded")
    def test_nav_link_is_not_false_positive(self):
        r = rec(watch_url="https://swamedia.co.tz/watch.php?id=5", html='<a href="/adult">Wakubwa</a>')
        self.assertEqual(policy.classify(r).status, "source_regular")
        self.assertEqual(policy.classify(rec(title="Adulting Diaries")).status, "source_regular")
    def test_approval_invalid_when_metadata_changes(self):
        r = rec(id=3); ap = {"items": [{"id": "3", "source_version": policy.source_version(r), "approved_by": "o", "approved_at": "t"}]}
        self.assertEqual(policy.approved_ids({"3": r}, ap)[0], {"3"})
        r2 = dict(r, title="Changed"); self.assertEqual(policy.approved_ids({"3": r2}, ap)[0], set())


class Collector(unittest.TestCase):
    def pages(self, total, pages):
        def f(kind, p):
            return {"success": True, "total": total, "hasMore": p < len(pages), "movies": pages[p - 1] if p <= len(pages) else []}
        return f
    def test_total_gap_and_empty_page(self):
        pg = [[{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 3}], []]
        recs, s, w = crawl_kind("movie", self.pages(10, pg))
        self.assertEqual(set(recs), {"1", "2", "3"}); self.assertEqual(s["duplicates"], 1)
        self.assertTrue(any("gap 7" in x for x in w))
    def test_repeated_page_guard(self):
        pg = [[{"id": 1}], [{"id": 1}], [{"id": 1}]]
        recs, s, w = crawl_kind("movie", lambda k, p: {"success": True, "total": 1, "hasMore": True, "movies": [{"id": 1}]})
        self.assertTrue(any("repeats" in x for x in w))
    def test_same_title_different_dj_kept(self):
        r, _, _ = crawl_kind("series", self.pages(2, [[{"id": 1, "title": "A", "dj_dubber": "X"}, {"id": 2, "title": "A", "dj_dubber": "Y"}]]))
        self.assertEqual(len(r), 2)
    def test_duplicate_folder_conflict(self):
        u = "https://drive.google.com/drive/folders/AAA"
        recs = {"1": {"title": "A", "dj_dubber": "X", "video_url": u}, "2": {"title": "B", "dj_dubber": "Y", "video_url": u}}
        self.assertEqual(len(folder_conflicts(recs)), 1)
    def test_sources_allowlist_and_trailer(self):
        refs, uns = extract_sources({"video_url": "https://evil.example/x", "trailer_url": "https://drive.google.com/file/d/T/view",
                                     "download_url": "https://swamedia.co.tz/dl.php?url=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2FF1%2Fview%3Fresourcekey%3DRK"})
        self.assertEqual([(r.kind, r.id, r.resource_key) for r in refs], [("file", "F1", "RK")])
        self.assertEqual(uns[0]["host"], "evil.example")
    def test_drive_url_kinds(self):
        self.assertEqual(parse_drive_url("https://drive.google.com/open?id=ABC").kind, "file")
        self.assertEqual(parse_drive_url("https://drive.google.com/drive/folders/XYZ?resourcekey=k").resource_key, "k")


def fake_drive(tree):
    """tree: folder_id -> list of file dicts; pages of 1000 split by token."""
    calls = []
    def lp(fid, token, rk):
        calls.append((fid, token))
        if fid not in tree:
            raise DriveError("permission", "403", 403)
        items = tree[fid]; start = int(token or 0); end = start + 120
        return {"files": items[start:end], "nextPageToken": str(end) if end < len(items) else None}
    lp.calls = calls
    return lp

def vid(i, name): return {"id": i, "name": name, "mimeType": "video/mp4", "size": "10", "modifiedTime": "t"}
def folder(i, name): return {"id": i, "name": name, "mimeType": FOLDER_MIME}


class Drive(unittest.TestCase):
    def test_pagination_over_50(self):
        tree = {"R": [vid(f"f{i}", f"EP {i}.mp4") for i in range(1, 301)]}
        files, issues = walk_folder(DriveRef("folder", "R"), fake_drive(tree))
        self.assertEqual(len(files), 300)
    def test_nested_cycle_and_frozen_subfolders(self):
        tree = {"ROOT": [folder("A", "FROZEN 1-10"), folder("B", "FROZEN 11-100 FINAL"), folder("ROOT", "loop")],
                "A": [vid(f"a{i}", f"FROZEN EP {i}.mp4") for i in range(1, 11)],
                "B": [vid(f"b{i}", f"FROZEN EP {i}.mp4") for i in range(11, 101)] + [{"id": "z", "name": "x.zip", "mimeType": "application/zip"}]}
        files, issues = walk_folder(DriveRef("folder", "ROOT"), fake_drive(tree))
        self.assertEqual(len(files), 100)
        self.assertTrue(any(i["type"] == "cycle_or_repeat" for i in issues))
        self.assertTrue(any(i["type"] == "not_media" for i in issues))
    def test_permission_denied_reported(self):
        files, issues = walk_folder(DriveRef("folder", "NOPE"), fake_drive({}))
        self.assertEqual(issues[0]["type"], "permission")
    def test_adult_filename_excluded(self):
        files, _ = walk_folder(DriveRef("folder", "R"), fake_drive({"R": [vid("1", "xxx clip.mp4")]}))
        self.assertEqual(files[0].status, "excluded")


class Naming(unittest.TestCase):
    def test_order_ep2_before_ep10(self):
        names = ["EP 10.mp4", "EP 2.mp4", "EP 1.mp4", "Kipindi 11.mp4"]
        got = sorted(names, key=lambda n: sort_key(parse_episode(n), n))
        self.assertEqual(got, ["EP 1.mp4", "EP 2.mp4", "EP 10.mp4", "Kipindi 11.mp4"])
    def test_patterns(self):
        e = parse_episode("Show S02E03 1080p 2024.mkv"); self.assertEqual((e.season, e.episode), (2, 3))
        self.assertEqual(parse_episode("Episode 3.mp4").episode, 3)
        p = parse_episode("Movie PART 2.mp4"); self.assertEqual((p.part, p.episode), (2, None))
    def test_year_and_range_not_episode(self):
        self.assertEqual(parse_episode("FROZEN FLOWER 1-100 FINAL 2024.mp4").status, "needs_review")
        self.assertEqual(parse_episode("movie 2024.mp4").status, "needs_review")
        self.assertEqual(parse_episode("EP 3 EP 4.mp4").status, "needs_review")
    def test_same_episode_two_ids_conflict(self):
        c = find_conflicts([{"file_id": "a", "episode": parse_episode("EP 1.mp4")}, {"file_id": "b", "episode": parse_episode("Episode 1.mp4")},
                            {"file_id": "a", "episode": parse_episode("EP 1.mp4")}])
        self.assertEqual(len(c), 1)
    def test_dj(self):
        for r in ("DJ Blaki", "Blaki", "DJ Black", "DJ Blacki", "dj blacky"): self.assertEqual(normalize_dj(r), "DJ Black")
        self.assertEqual(normalize_dj("DJ Mack Boy"), "DJ Mack")


class FakeInfo:  # HF repo_info
    def __init__(self, private): self.private = private

class FakeApi:
    def __init__(self, private=True): self.private, self.commits, self.fail = private, [], False
    def repo_info(self, repo, repo_type=None): return FakeInfo(self.private)

class AutoApproveTests(unittest.TestCase):
    def test_auto_approve_respects_policy_and_blocklist(self):
        from swamedia import cli
        recs = {"1": rec(id="1", title="Luca", type="movie"), "2": rec(id="2", title="Spicy Night Show", type="movie"),
                "3": rec(id="3", title="Safe", type="movie", adult="1")}
        ap, skipped = cli.auto_approvals(recs, ["spicy"])
        ids = {i["id"] for i in ap["items"]}
        self.assertEqual(ids, {"1"}); self.assertEqual(skipped, 1)
        ok, problems = policy.approved_ids(recs, ap)
        self.assertEqual(ok, {"1"}, problems)

class StateTests(unittest.TestCase):
    def test_public_repo_blocks_writes(self):
        s = HFStore("u/r", "tok", api=FakeApi(private=False))
        with self.assertRaises(PublicRepoError): s.ensure_private()
        with self.assertRaises(PublicRepoError): s.commit([], {"a": {}}, "m")
        with self.assertRaises(ValueError): HFStore("", "t", api=FakeApi())
    def test_source_change_not_skipped(self):
        k1, k2 = st.resume_key(1, "f", "md5a"), st.resume_key(1, "f", "md5b")
        self.assertNotEqual(k1, k2)
        old = {"resume_key": k1, "status": "uploaded", "ready": True, "attempts": 0, "next_retry_at": None, "blocked": False, "reason": None}
        new = {"resume_key": k2, "status": "approved"}
        self.assertEqual(st.merge_task(old, new)["status"], "approved")
        self.assertEqual(st.merge_task(old, {"resume_key": k1, "status": "approved"})["status"], "uploaded")
    def test_attempt5_returns_after_cooldown(self):
        t = {"attempts": 0, "status": "approved", "blocked": False, "next_retry_at": None}
        now = st.now_utc()
        for _ in range(6): st.mark_failed(t, "x", "other", now)
        self.assertFalse(st.eligible(t, now))
        self.assertTrue(st.eligible(t, now + timedelta(days=2)))
        st.mark_failed(t, "perm", "permission", now); self.assertFalse(st.eligible(t, now + timedelta(days=30)))
        st.manual_retry(t); self.assertTrue(st.eligible(t, now))


class FakeStore:
    def __init__(self, fail_commit=False):
        self.files, self.json, self.fail = {}, {}, fail_commit
    def ensure_private(self): pass
    def load_json(self, p, d): return json.loads(json.dumps(self.json[p])) if p in self.json else d
    def commit(self, videos, jsons, msg):
        if self.fail: raise RuntimeError("boom")
        for src, dst in videos: self.files[dst] = os.path.getsize(src)
        for k, v in jsons.items(): self.json[k] = json.loads(json.dumps(v))
    def verify_remote(self, path, size): return self.files.get(path) == size

class FakeDrive:
    def __init__(self, src): self.src = src
    def list_page(self, fid, tok, rk): return {"files": [vid("F1", "EP 1.mp4")]}
    def get_meta(self, fid, rk=None): return {"size": str(os.path.getsize(self.src)), "id": fid, "name": "EP 1.mp4", "mimeType": "video/mp4"}
    def download(self, f, dest):
        import shutil; shutil.copy(self.src, dest)


def make_sample(path, audio=True, codec="mpeg4", pix="yuv420p"):
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=321x241:rate=10:duration=2"]
    if audio: cmd += ["-f", "lavfi", "-i", "sine=duration=2"]
    cmd += ["-c:v", codec, "-pix_fmt", pix] + (["-c:a", "mp3"] if audio else []) + [path]
    subprocess.run(cmd, check=True)

HAVE_FF = subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0


@unittest.skipUnless(HAVE_FF, "ffmpeg missing")
class Conversion(unittest.TestCase):
    def test_sample_conversion_h264_yuv420p_aac_faststart(self):
        d = tempfile.mkdtemp(); s, o = os.path.join(d, "s.avi"), os.path.join(d, "o.mp4")
        make_sample(s)
        rep = converter.convert(s, o)
        self.assertEqual((rep["video"], rep["audio"], rep["mode"]), ("h264", "aac", "transcode"))
        self.assertEqual(rep["width"] % 2, 0); self.assertTrue(converter.moov_before_mdat(o))
    def test_no_audio_source_allowed(self):
        d = tempfile.mkdtemp(); s, o = os.path.join(d, "s.avi"), os.path.join(d, "o.mp4")
        make_sample(s, audio=False)
        self.assertIsNone(converter.convert(s, o)["audio"])
    def test_html_is_not_video(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "x.mp4"); open(p, "w").write("<html>quota</html>")
        with self.assertRaises(converter.ConvertError): converter.convert(p, os.path.join(d, "o.mp4"))

    def _approved(self, r):
        return {"items": [{"id": str(r["id"]), "source_version": policy.source_version(r), "approved_by": "owner", "approved_at": "2026-11-20"}]}

    def test_upload_failure_not_ready_and_success_ready(self):
        d = tempfile.mkdtemp(); s = os.path.join(d, "s.avi"); make_sample(s)
        r = rec(id=5, video_url="https://drive.google.com/drive/folders/ROOT")
        store = FakeStore(fail_commit=True)
        w = os.path.join(d, "w")
        rep = pipeline.run_convert(store, FakeDrive(s), {"5": r}, self._approved(r), w, enabled=True)
        self.assertEqual((rep["ready"], rep["uploaded"], rep["failed"]), (0, 0, 1))
        self.assertTrue(os.path.exists(os.path.join(w, "F1.mp4")))  # local backup kept, not cleaned before upload
    def test_end_to_end_ready_and_exclusion_removes_from_serving(self):
        d = tempfile.mkdtemp(); s = os.path.join(d, "s.avi"); make_sample(s)
        r = rec(id=5, video_url="https://drive.google.com/drive/folders/ROOT")
        store = FakeStore()
        rep = pipeline.run_convert(store, FakeDrive(s), {"5": r}, self._approved(r), os.path.join(d, "w"), enabled=True)
        self.assertEqual((rep["uploaded"], rep["ready"]), (1, 1))
        self.assertIn("videos/swamedia/5/F1.mp4", store.files)
        self.assertEqual(len(store.json[pipeline.SERVING_PATH]["items"]["5"]["episodes"]), 1)
        self.assertNotIn("drive.google", json.dumps(store.json[pipeline.SERVING_PATH]))
        # item becomes excluded -> gone from serving, purge report asks separate approval, nothing deleted
        r2 = dict(r, category="Adult")
        rep2 = pipeline.run_convert(store, FakeDrive(s), {"5": r2}, self._approved(r), os.path.join(d, "w"), enabled=True)
        self.assertEqual(store.json[pipeline.SERVING_PATH]["items"], {})
        self.assertIn("videos/swamedia/5/F1.mp4", store.files)
    def test_disabled_does_nothing(self):
        rep = pipeline.run_convert(FakeStore(), None, {}, {"items": []}, tempfile.mkdtemp(), enabled=False)
        self.assertEqual(rep["uploaded"], 0)


class D1Tests(unittest.TestCase):
    def test_d1_rows(self):
        from swamedia import d1
        good = rec(id=1, title="O'Neil | DJ X", poster_url="https://p/x.jpg", video_url="https://drive.google.com/drive/folders/ROOT")
        bad = rec(id=2, category="Adult", poster_url="https://p/bad.jpg", video_url="https://drive.google.com/drive/folders/BAD")
        recs = {"1": good, "2": bad}
        dec = {k: policy.classify(v) for k, v in recs.items()}
        task = {"key": "1:F1", "item_id": "1", "file_id": "F1", "original_name": "EP 1.mp4", "season": None, "episode": 1,
                "part": None, "path": "videos/swamedia/1/F1.mp4", "status": "uploaded", "ready": True, "info": {"duration": 2.0, "size": 9}}
        sql = "\n".join(d1.build_statements(recs, dec, {"1"}, {"1:F1": task}, "jaynesmaxtv/movie-file"))
        self.assertIn("O''Neil", sql); self.assertIn("jaynesmaxtv/movie-file", sql)
        self.assertIn("videos/swamedia/1/", sql); self.assertIn("ROOT", sql)
        self.assertNotIn("BAD", sql); self.assertNotIn("bad.jpg", sql)  # excluded: no links/posters
    def test_client_batches(self):
        from swamedia import d1
        calls = []
        class C:
            def run(self, sql): calls.append(sql)
        d1.sync(C(), ["SELECT 1;"] * 250, chunk=100)
        self.assertEqual(len(calls), 4)
    def test_bad_row_isolated(self):
        from swamedia import d1
        done = []
        class C:
            def run(self, sql):
                if "BAD" in sql: raise d1.D1Error(400, "SQLITE_ERROR: x")
                done.append(sql)
        stmts = ["INSERT OR REPLACE INTO items (id,title) VALUES ('1','ok');", "INSERT OR REPLACE INTO items (id,title) VALUES ('2','BAD');",
                 "INSERT OR REPLACE INTO media (media_id,item_id) VALUES ('m9','3');"]
        fl = []
        d1.sync(C(), stmts, chunk=100, failures=fl)
        self.assertEqual(len(fl), 1); self.assertEqual(fl[0][0], "items:2")
        self.assertEqual(len([d for d in done if d.startswith("INSERT")]), 2)


if __name__ == "__main__":
    unittest.main()
